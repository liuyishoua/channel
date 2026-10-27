import json
import tempfile
import time
import unittest
from channel_service.demo import DemoMultica, AGENT_ID
from channel_service.domain import Problem
from channel_service.multica import RemoteError
from channel_service.sessions import iso
from channel_service.store import Store
from channel_service.worker import Worker


class Remote(DemoMultica):
    def __init__(self):
        super().__init__();self.chats=[];self.inputs=[];self.writes=0;self.lose_create=False;self.lose_send=False
        self.output={'decision':'reply','reply_messages':[{'type':'text','text':'你好','delay_seconds':0}], 'requires_human':False}
    def request(self, method, path, data=None):
        if method=='POST' and path=='/api/chat/sessions':
            self.chats.append({'id':'chat-1',**data})
            if self.lose_create:self.lose_create=False;raise RemoteError(0,True)
            return self.chats[-1]
        if method=='GET' and path.startswith('/api/chat/sessions?'):return self.chats
        if method=='POST' and path.endswith('/messages'):
            self.writes+=1;self.inputs.append({'id':'input-1','task_id':'task-1','role':'user','content':data['content']})
            if self.lose_send:self.lose_send=False;raise RemoteError(0,True)
            return {'message_id':'input-1','task_id':'task-1','attachment_ids':data['attachment_ids']}
        if method=='GET' and path.endswith('/messages'):
            return self.inputs+[{'role':'assistant','task_id':'task-1','content':json.dumps(self.output)}] if self.inputs else []
        if method=='PATCH':return {}
        raise AssertionError((method,path))


class FastReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.store.source('xhs');self.remote=Remote();self.worker=Worker(self.store,self.remote)
        self.route=self.store.save_route(dict(name='即时私信',agent_id=AGENT_ID,sources=['xhs'],account_ids=['a'],
            categories=['reply_draft'],surfaces=['dm'],event_types=['dm.received'],batch_seconds=0,execution_mode='frozen_reply'))
        s=self.store.sessions.open('xhs',dict(account_id='a',surface='dm',conversation_id='peer'))
        self.sid=s['id'];self.store.sessions.sync('xhs',self.sid,dict(account_id='a',messages=[dict(
            message_id='m',text='你好',direction='inbound',sent_at=iso(time.time()-1))]))
        self.snap=self.store.sessions.snapshot('xhs',self.sid,dict(account_id='a',request_id='r',trigger_message_ids=['m']))
        self.event=self.store.ingest('xhs',dict(account_id='a',message_id='job',category='reply_draft',surface='dm',
            event_type='dm.received',conversation_id='peer',route_id=self.route['id'],session_snapshot_id=self.snap['id'],
            content={'text':'当前请求','images':[]},context={'control_mode':'full_access'}))[0]
    def tick(self):
        with self.store.db(True) as db:db.execute('UPDATE batches SET next_poll=0,lease_until=0')
        batch=self.store.claim()
        if batch:self.worker.process(batch)
    def complete(self):
        for _ in range(6):
            self.tick();r=self.store.message(self.event['id'])
            if r['state'] in ('succeeded','failed','failed_preflight','cancelled'):return r
        self.fail('did not complete')
    def test_complete_frozen_context_runs_once_without_issue_reads(self):
        r=self.complete();self.assertEqual(r['state'],'succeeded');self.assertEqual(self.remote.writes,1)
        self.assertEqual(r['execution']['chat_session_id'],'chat-1')
        body=json.loads(self.remote.inputs[0]['content'].split('\n',1)[1])
        self.assertEqual(body['requests'][0]['context']['current_messages'][0]['text'],'你好')
        self.assertEqual(body['requests'][0]['account_id'],'a')
        self.tick();self.assertEqual(self.remote.writes,1)
    def test_restart_trigger_is_separate_from_historical_stop_and_old_decision(self):
        messages=[{'context':{'messages':[{'message_id':'old','text':'停止吧'},
            {'message_id':'new','text':'继续聊聊呗'}],'trigger_message_ids':['new'],
            'previous_analysis':{'summary':'本轮不回复，尊重停止要求'}}}]
        from channel_service.fast_reply import FastReply
        prompt=FastReply(self.worker).prompt({'id':'probe'},messages)
        context=json.loads(prompt.split('\n',1)[1])['requests'][0]['context']
        self.assertEqual(context['current_messages'][0]['text'],'继续聊聊呗')
        self.assertEqual(context['messages'][0]['text'],'停止吧')
        self.assertNotIn('previous_analysis',context)
        self.assertEqual(len(messages[0]['context']['messages']),2)
    def test_lost_create_receipt_is_reconciled_without_duplicate_creation(self):
        self.remote.lose_create=True;r=self.complete()
        self.assertEqual(r['state'],'succeeded');self.assertEqual(len(self.remote.chats),1);self.assertEqual(self.remote.writes,1)
    def test_lost_send_receipt_is_reconciled_without_duplicate_model_task(self):
        self.remote.lose_send=True;r=self.complete()
        self.assertEqual(r['state'],'succeeded');self.assertEqual(self.remote.writes,1)
    def test_snapshot_superseded_before_execution_never_starts_model(self):
        self.store.sessions.sync('xhs',self.sid,dict(account_id='a',messages=[dict(message_id='new',text='停止吧',
            direction='inbound',sent_at=iso(time.time()))]))
        self.assertEqual(self.complete()['state'],'cancelled');self.assertEqual(self.remote.writes,0)
    def test_invalid_model_contract_is_rejected(self):
        self.remote.output={'title':'not a reply'}
        self.assertEqual(self.complete()['state'],'failed')
    def test_execution_mode_rejects_other_surfaces(self):
        with self.assertRaises(Problem):
            self.store.save_route({**self.route,'surfaces':['comment']})
    def test_editing_ordinary_route_fields_preserves_reply_execution_mode(self):
        data={k:v for k,v in self.route.items() if k!='execution_mode'}
        data['name']='更新名称'
        saved=self.store.save_route(data,self.route['id'])
        self.assertEqual(saved['execution_mode'],'frozen_reply')
