import concurrent.futures
import json
import tempfile
import time
import unittest
from channel_service.app import create_app
from channel_service.demo import DemoMultica, AGENT_ID
from channel_service.domain import Problem
from channel_service.sessions import iso
from channel_service.store import Store
from channel_service.worker import Worker


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.token=self.store.source('xhs')['token'];self.other=self.store.source('other')['token']
        self.s=self.store.sessions;self.session=self.s.open('xhs',dict(account_id='a',surface='dm',conversation_id='peer'))
        self.sid=self.session['id'];self.remote=DemoMultica();self.worker=Worker(self.store,self.remote)
        self.route=self.store.save_route(dict(name='会话',agent_id=AGENT_ID,batch_seconds=0,max_batch=20))
        self.app=create_app(self.tmp.name,self.remote);self.client=self.app.test_client()
        self.headers={'Authorization':'Bearer '+self.token}
    def tearDown(self): self.tmp.cleanup()
    def msg(self,mid='m1',text='你好',age=1,**extra):
        return dict(message_id=mid,text=text,sent_at=iso(time.time()-age),direction='inbound',**extra)
    def sync(self,messages,**extra):
        return self.s.sync('xhs',self.sid,dict(account_id='a',messages=messages,**extra))
    def snap(self,mid='m1',request_id='r1',**extra):
        return self.s.snapshot('xhs',self.sid,dict(account_id='a',request_id=request_id,trigger_message_ids=[mid],**extra))
    def submit(self,snap,message_id='job1',**extra):
        p=dict(account_id='a',message_id=message_id,conversation_id='peer',category='reply_draft',surface='dm',event_type='dm.received',content={'text':'请决策本轮'},context={},session_snapshot_id=snap['id'])
        p.update(extra);return self.store.ingest('xhs',p)[0]
    def tick(self):
        with self.store.db(True) as db: db.execute('UPDATE batches SET next_poll=0,lease_until=0')
        batch=self.store.claim()
        if batch:self.worker.process(batch)
        return batch
    def complete(self,event):
        for _ in range(10):
            self.tick();state=self.store.message(event['id'])
            if state['state'] in ('succeeded','cancelled','uncertain','failed_preflight'):return state
        self.fail('no terminal result')
    def test_identity_is_independent_of_agent_route_and_source_account_surface(self):
        self.assertEqual(self.sid,self.s.open('xhs',dict(account_id='a',surface='dm',conversation_id='peer'))['id'])
        ids={self.sid}
        for source,account,surface in [('other','a','dm'),('xhs','b','dm'),('xhs','a','comment')]:
            ids.add(self.s.open(source,dict(account_id=account,surface=surface,conversation_id='peer'))['id'])
        self.assertEqual(len(ids),4)
    def test_sync_is_idempotent_atomic_and_never_enqueues_jobs(self):
        messages=[self.msg('x')];self.assertEqual(self.sync(messages)['changed'],1)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda _:self.sync(messages),range(8)))
        self.assertTrue(all(r['changed']==0 for r in results))
        with self.assertRaises(Problem): self.sync([self.msg('should-rollback'),{**messages[0],'text':'冲突'}])
        self.assertEqual(len(self.s.messages('xhs',self.sid,'a')['items']),1)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
    def test_source_account_and_browser_isolation_on_every_read_write(self):
        self.sync([self.msg()]);snap=self.snap()
        for path in [f'/v1/sessions/{self.sid}',f'/v1/sessions/{self.sid}/messages',f'/v1/sessions/{self.sid}/memories',f'/v1/session-snapshots/{snap["id"]}',f'/v1/session-snapshots/{snap["id"]}/validate']:
            self.assertEqual(self.client.get(path+'?account_id=a',headers={'Authorization':'Bearer '+self.other}).status_code,404)
            self.assertEqual(self.client.get(path+'?account_id=b',headers=self.headers).status_code,404)
            self.assertEqual(self.client.get(path,headers=self.headers).status_code,400)
            self.assertEqual(self.client.get(path+'?account_id=a',headers={**self.headers,'Origin':'https://evil.test'}).status_code,403)
        for suffix,payload in [('/messages',{'messages':[]}),('/memories',{'key':'x','kind':'fact','text':'x','evidence':['m1']}),('/snapshots',{'request_id':'x','trigger_message_ids':['m1']})]:
            self.assertEqual(self.client.post(f'/v1/sessions/{self.sid}'+suffix,headers={'Authorization':'Bearer '+self.other},json={'account_id':'a',**payload}).status_code,404)
    def test_query_pagination_handles_late_arrivals_with_event_time_order(self):
        self.sync([self.msg('new',age=1),self.msg('old',age=100),self.msg('middle',age=50)])
        first=self.s.messages('xhs',self.sid,'a',limit=2)
        self.assertEqual([m['message_id'] for m in first['items']],['new','middle'])
        second=self.s.messages('xhs',self.sid,'a',limit=2,before=first['next_cursor'])
        self.assertEqual([m['message_id'] for m in second['items']],['old'])
    def test_revision_correction_and_stale_transport_cannot_undo_withdrawal(self):
        msg=self.msg();self.sync([msg]);snap=self.snap()
        self.sync([{**msg,'revision':2,'revoked':True}]);self.sync([msg])
        self.assertTrue(self.s.messages('xhs',self.sid,'a')['items'][0]['revoked'])
        self.assertFalse(self.s.validate('xhs',snap['id'],'a')['current'])
        with self.assertRaises(Problem):self.sync([{**msg,'revision':3,'sent_at':iso(time.time()-10)}])
    def test_snapshot_retry_returns_exact_original_after_new_messages(self):
        self.sync([self.msg()]);original=self.snap();self.sync([self.msg('m2',age=0)])
        self.assertEqual(original,self.snap());self.assertFalse(self.s.validate('xhs',original['id'],'a')['current'])
        with self.assertRaises(Problem):self.snap(recent_limit=5)
    def test_freshness_echo_and_answered_messages_do_not_trigger(self):
        self.sync([self.msg('old',age=172801)]);self.assertFalse(self.snap('old')['eligible'])
        self.sync([self.msg('current')]);self.sync([{**self.msg('self',age=0),'direction':'outbound'}])
        self.assertFalse(self.snap('current',request_id='r2')['eligible'])
    def test_budget_and_related_recall_do_not_repeat_current_body(self):
        history=[self.msg('h'+str(i),'无关长对话'+('文'*80),age=1000-i) for i in range(100)]
        history[3]['text']='周六咖啡店见面要带相机'
        self.sync(history);self.sync([self.msg('current','周六咖啡店几点呀')]);snap=self.snap('current',max_context_bytes=6000,recent_limit=10)
        ctx=snap['context'];self.assertLessEqual(snap['context_bytes'],6000)
        self.assertEqual(sum(m['message_id']=='current' for m in ctx['messages']),1);self.assertNotIn('text',ctx['current_message'])
        self.assertTrue(any(m['message_id']=='h3' for m in ctx['messages']));self.assertLess(len(ctx['messages']),20)
    def test_trigger_is_never_silently_truncated(self):
        self.sync([self.msg(text='字'*3000)])
        with self.assertRaises(Problem):self.snap(max_context_bytes=4096)
    def test_memory_needs_evidence_cas_expiry_and_invalidates_on_correction(self):
        msg=self.msg('pref','喜欢安静的咖啡店');self.sync([msg])
        data=dict(account_id='a',key='coffee',kind='preference',text='喜欢安静咖啡店',evidence=['pref'],ttl_seconds=3600)
        result=self.s.memory('xhs',self.sid,data);self.assertEqual(result,self.s.memory('xhs',self.sid,data))
        self.assertEqual(len(self.snap('pref')['context']['memories']),1)
        with self.assertRaises(Problem):self.s.memory('xhs',self.sid,{**data,'text':'变化'})
        self.sync([{**msg,'revision':2,'text':'其实我不喜欢咖啡'}]);self.assertEqual(self.snap('pref',request_id='new')['context']['memories'],[])
        with self.assertRaises(Problem):self.s.memory('xhs',self.sid,{**data,'evidence':['missing']})
    def test_independent_runs_keep_same_serialization_key_but_new_issues(self):
        self.sync([self.msg()]);first=self.submit(self.snap());a=self.complete(first)
        self.sync([self.msg('m2',age=0)]);second=self.submit(self.snap('m2',request_id='r2'),message_id='job2');b=self.complete(second)
        self.assertEqual(first['group_key'],second['group_key']);self.assertNotEqual(a['execution']['issue_id'],b['execution']['issue_id'])
        self.assertEqual([c[0] for c in self.remote.calls],['create','create']);self.assertNotIn('messages',second['payload']['context'])
        self.assertTrue(all('独立会话决策' in i['description'] for i in self.remote.issues.values()))
        for issue in self.remote.issues.values():
            self.assertIn('内部任务读取', issue['description'])
            self.assertIn('你好', issue['description'])
    def test_parallel_turns_stay_serial_even_across_route_version_change(self):
        self.remote.auto_complete=False;self.sync([self.msg()]);first=self.submit(self.snap());self.tick()
        self.store.save_route({**self.route,'instruction':'新的要求'},self.route['id'])
        self.sync([self.msg('m2',age=0)]);second=self.submit(self.snap('m2',request_id='r2'),message_id='job2')
        self.assertEqual(first['group_key'],second['group_key']);self.assertIsNone(self.store.claim())
    def test_new_facts_cancel_queued_snapshot_before_remote_create(self):
        self.sync([self.msg()]);event=self.submit(self.snap());self.sync([self.msg('new',age=0)])
        self.assertEqual(self.complete(event)['state'],'cancelled');self.assertEqual(self.remote.calls,[])
    def test_job_retry_snapshot_dedupe_and_scope_override(self):
        self.sync([self.msg()]);snap=self.snap();event=self.submit(snap);self.assertEqual(event['id'],self.submit(snap)['id'])
        for extra in [dict(message_id='another'),dict(message_id='wrong',conversation_id='wrong'),dict(message_id='override',context={'messages':[]})]:
            with self.assertRaises(Problem):self.submit(snap,**extra)
        with self.assertRaises(Problem):self.submit(self.snap(request_id='other'),message_id='another-request')
    def test_lost_remote_create_response_reconciles_one_issue(self):
        self.sync([self.msg()]);event=self.submit(self.snap());self.remote.drop_create_response=True;self.tick()
        self.assertEqual(self.store.message(event['id'])['execution']['status'],'reconciling')
        self.assertEqual(self.complete(event)['state'],'succeeded');self.assertEqual(len(self.remote.issues),1)
    def test_expired_snapshot_cancels_without_model_or_send(self):
        self.sync([self.msg()]);snap=self.snap();event=self.submit(snap)
        with self.store.db(True) as db:
            body=json.loads(db.execute('SELECT body FROM session_snapshots WHERE id=?',(snap['id'],)).fetchone()[0]);body['expires_at']=iso(time.time()-1)
            db.execute('UPDATE session_snapshots SET body=? WHERE id=?',(json.dumps(body),snap['id']))
        self.assertEqual(self.complete(event)['state'],'cancelled');self.assertEqual(self.remote.calls,[])

    def test_long_session_has_bounded_turns_and_complete_queryable_facts(self):
        for turn in range(50):
            mid='turn-'+str(turn)
            self.sync([self.msg(mid,'周六一起喝咖啡 '+str(turn),age=0)])
            snap=self.snap(mid,request_id=mid,max_context_bytes=8000)
            self.assertLessEqual(snap['context_bytes'],8000)
            self.assertLessEqual(len(snap['context']['messages']),26)
            self.assertEqual(sum(m['message_id']==mid for m in snap['context']['messages']),1)
        records=self.s.messages('xhs',self.sid,'a',limit=100)['items']
        self.assertEqual(len(records),50)
        self.assertTrue(all('revision' in r for r in records))

    def test_memory_can_be_retired_with_cas_and_retry_without_resurrection(self):
        self.sync([self.msg()])
        self.s.memory('xhs',self.sid,dict(account_id='a',key='style',text='简短',kind='style',evidence=['m1']))
        data=dict(account_id='a',expected_version=1)
        retired=self.s.retire_memory('xhs',self.sid,'style',data)
        self.assertEqual(retired,self.s.retire_memory('xhs',self.sid,'style',data))
        self.assertEqual(self.snap()['context']['memories'],[])
        with self.assertRaises(Problem):self.s.memory('xhs',self.sid,dict(account_id='a',key='style',text='重新启用',kind='style',evidence=['m1'],expected_version=1))

    def test_group_actor_and_bad_message_lists_are_validated(self):
        self.sync([self.msg(actor_id='member-1')])
        self.assertEqual(self.s.messages('xhs',self.sid,'a')['items'][0]['actor_id'],'member-1')
        with self.assertRaises(Problem):self.s.snapshot('xhs',self.sid,dict(account_id='a',request_id='bad',trigger_message_ids=[{}]))

    def test_evidence_correction_also_invalidates_previous_model_observation(self):
        original=self.msg();self.sync([original]);self.complete(self.submit(self.snap()))
        self.sync([self.msg('new',age=0)])
        self.assertTrue(self.snap('new',request_id='next')['context']['previous_analysis'])
        self.sync([{**original,'revision':2,'revoked':True}])
        self.assertEqual(self.snap('new',request_id='corrected')['context']['previous_analysis'],{})

    def test_shared_media_contract_accepts_eight_and_rejects_nine(self):
        from channel_service.domain import normalize_message
        payload=dict(account_id='a',message_id='images',content={'text':'图像','images':['asset-'+str(i) for i in range(8)]})
        self.assertEqual(len(normalize_message(payload,'xhs')['content']['images']),8)
        payload['content']['images'].append('asset-9')
        with self.assertRaises(Problem):normalize_message(payload,'xhs')
