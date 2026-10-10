"""Real XHS agent bootstrap regression with an isolated synthetic Channel store."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from channel_service.multica import Multica
from channel_service.store import Store
from channel_service.worker import Worker
from channel_service.sessions import iso


def main():
    p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true');p.add_argument('--question',action='store_true');args=p.parse_args()
    if not args.execute:p.error('--execute is required for a real model call')
    with sqlite3.connect(ROOT/'.local/channel.sqlite3') as db:
        route=json.loads(db.execute('SELECT route FROM batches WHERE id=?',('a4bedb62-9c90-4f89-88ce-2c1d42237cee',)).fetchone()[0])
    remote=Multica(ROOT/'.local/multica.json')
    with tempfile.TemporaryDirectory(prefix='dm-bootstrap-') as directory:
        store=Store(directory);store.source('xhs');route=store.save_route({**route,'batch_seconds':0})
        worker=Worker(store,remote)
        account='synthetic-bootstrap-check';session=store.sessions.open('xhs',dict(account_id=account,surface='dm',conversation_id='synthetic-peer'))
        now=time.time()
        messages=[dict(message_id='self-1',direction='outbound',text='哈哈，刚刚那个猫猫表情也太逗了',sent_at=iso(now-10)),dict(message_id='peer-1',direction='inbound',text='你说的猫猫表情是好笑还是可爱呀？' if args.question else '嘻嘻嘻',sent_at=iso(now-1))]
        store.sessions.sync('xhs',session['id'],dict(account_id=account,messages=messages,coverage=dict(since=iso(now-172800),until=iso(now),complete=True)))
        snapshot=store.sessions.snapshot('xhs',session['id'],dict(account_id=account,request_id='bootstrap-test',trigger_message_ids=['peer-1']))
        event=store.ingest('xhs',dict(account_id=account,message_id='bootstrap-test',conversation_id='synthetic-peer',category='reply_draft',surface='dm',event_type='dm.received',route_id=route['id'],session_snapshot_id=snapshot['id'],content={'text':'纯虚构聊天验收，只根据本轮快照决定是否接话，最终按JSON契约返回。不向任何平台发送。','images':[]},context={'control_mode':'full_access','media_catalog':[]}))[0]
        deadline=time.monotonic()+240
        while time.monotonic()<deadline:
            with store.db(True) as db:db.execute('UPDATE batches SET next_poll=0,lease_until=0')
            batch=store.claim()
            if batch:worker.process(batch)
            state=store.message(event['id'])
            if state['state'] in ('succeeded','failed','failed_preflight','cancelled','uncertain','attention'):break
            time.sleep(2)
        result=state.get('result') or {}
        record={k:state.get(k) for k in ('id','state','execution','error')};record['result']=result
        execution=state.get('execution') or {}
        transcript=remote.transcript(execution['task_id']) if execution.get('task_id') else []
        # Keep only non-secret run metadata and synthetic data in the private report.
        record['transcript']=transcript
        path=ROOT/('.local/dm-task-reading-question-verification.json' if args.question else '.local/dm-task-reading-verification.json');path.write_text(json.dumps(record,ensure_ascii=False,indent=2));path.chmod(0o600)
        output=(result.get('output') or {}).get('structured') or {}
        print(json.dumps({'state':state['state'],'execution':execution,'output':output},ensure_ascii=False),flush=True)
        assert state['state']=='succeeded',state['error']
        assert output.get('decision') in ('reply','no_reply') and output.get('requires_human') is False,output
        if args.question:
            assert output['decision']=='reply' and output.get('reply_messages'),output
        assert 'issue 查询' not in output.get('summary','') and '没有提供私信上下文' not in output.get('summary',''),output
        tools=[x for x in transcript if x.get('type')=='tool_use']
        assert any('multica issue get' in json.dumps(x,ensure_ascii=False) for x in tools),'Agent did not read assigned task'
        print('PASS: real agent read assigned issue and decided from synthetic chat; no platform send',flush=True)

if __name__=='__main__':main()
