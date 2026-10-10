import concurrent.futures
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from PIL import Image
from channel_service.app import create_app
from channel_service.demo import AGENT_ID, DemoMultica
from channel_service.domain import Problem, group_key, normalize_message
from channel_service.multica import extract_output
from channel_service.store import Store
from channel_service.worker import Worker


def png():
    out=io.BytesIO();Image.new('RGB',(40,30),'green').save(out,'PNG');return out.getvalue()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.remote=DemoMultica();self.worker=Worker(self.store,self.remote)
        self.key=self.store.source('xianyu','闲鱼')['token'];self.store.source('zhihu','知乎')
        self.route=self.store.save_route(dict(name='客服起草',agent_id=AGENT_ID,batch_seconds=0,grouping='conversation',max_batch=1))
        self.counter=0
    def tearDown(self):self.tmp.cleanup()
    def incoming(self,**kwargs):
        self.counter+=1
        d=dict(account_id='shop-1',message_id='m-'+str(self.counter),category='reply_draft',conversation_id='customer-1',content={'text':'商品可以包邮吗？'},context={})
        d.update(kwargs);return self.store.ingest('xianyu',d)[0]
    def tick(self):
        with self.store.db(True) as db:db.execute('UPDATE batches SET next_poll=0,lease_until=0')
        b=self.store.claim()
        if b:self.worker.process(b)
        return b
    def complete(self,m):
        for _ in range(10):
            self.tick();d=self.store.message(m['id'])
            if d['state'] in ('succeeded','failed','failed_preflight','uncertain'):return d
        self.fail('did not complete')
    def test_duplicate_is_same_event_and_changed_content_conflicts(self):
        m=self.incoming(message_id='same');again=self.incoming(message_id='same');self.assertEqual(m['id'],again['id'])
        with self.assertRaises(Problem) as e:self.incoming(message_id='same',content={'text':'changed'})
        self.assertEqual(e.exception.status,409)
    def test_lifestyle_editor_has_one_direct_contract_without_generic_wrapper(self):
        message = {'source_id': 'xhs', 'category': 'lifestyle_editor', 'content': {'text': 'mode=review'}}
        body = self.worker.body({'id': 'review-batch'}, self.route, [message], {})
        self.assertIn('mode=review 返回 decision=approve|hold', body)
        self.assertIn('不要把结果包装进 reply/summary/labels 字段', body)
        self.assertNotIn('"reply":"可采用的正文或结论"', body)
        ordinary = self.worker.body({'id': 'ordinary'}, self.route,
            [{**message, 'category': 'content_analysis'}], {})
        self.assertIn('"reply":"可采用的正文或结论"', ordinary)
    def test_parallel_duplicate_insert_is_atomic(self):
        data=dict(account_id='shop',message_id='same',content={'text':'一次'})
        with concurrent.futures.ThreadPoolExecutor(8) as pool:rows=list(pool.map(lambda _:self.store.ingest('xianyu',data)[0],range(12)))
        self.assertEqual(len({x['id'] for x in rows}),1)
    def test_source_and_account_boundaries(self):
        a=self.incoming();b=self.incoming(account_id='shop-2')
        self.assertNotEqual(a['group_key'],b['group_key'])
        with self.assertRaises(LookupError):self.store.message(a['id'],'zhihu')
        with self.assertRaises(LookupError):self.store.message(a['id'],'xianyu','shop-2')
    def test_context_cannot_contain_credentials(self):
        for context in [{'cookie':'secret'},{'nested':[{'api_key':'secret'}]},{'text':'mul_'+'a'*32}]:
            with self.assertRaises(Problem):self.incoming(context=context)
    def test_source_claim_cannot_be_forged(self):
        with self.assertRaises(Problem) as e:self.incoming(source_id='zhihu')
        self.assertEqual(e.exception.status,403)
    def test_missing_scope_defaults_to_independent_tasks(self):
        a=self.incoming(conversation_id='');b=self.incoming(conversation_id='');self.assertNotEqual(a['group_key'],b['group_key'])
    def test_topic_grouping(self):
        r=self.store.save_route(dict(name='主题',agent_id=AGENT_ID,batch_seconds=0,grouping='topic',priority=90))
        a=self.incoming(context={'topic_id':'item-1'});b=self.incoming(context={'topic_id':'item-1'},conversation_id='different')
        self.assertEqual(a['group_key'],b['group_key']);self.assertEqual(a['route']['id'],r['id'])
    def test_priority_and_media_match(self):
        image_route=self.store.save_route(dict(name='图片',agent_id=AGENT_ID,kinds=['image','mixed'],priority=99,batch_seconds=0))
        asset=self.store.put_asset('xianyu','shop-1',png())
        a=self.incoming(content={'text':'','images':[asset['id']]});self.assertEqual(a['route']['id'],image_route['id'])
        self.assertEqual(self.incoming()['route']['id'],self.route['id'])
    def test_assets_are_validated_and_account_scoped(self):
        with self.assertRaises(Problem):self.store.put_asset('xianyu','shop-1',b'<svg></svg>')
        asset=self.store.put_asset('xianyu','shop-1',png())
        with self.assertRaises(Problem):self.incoming(account_id='shop-2',content={'text':'看图','images':[asset['id']]})
        self.assertEqual(Path(self.store.asset(asset['id'])['path']).stat().st_mode&0o777,0o600)
    def test_repeated_image_upload_keeps_same_asset_for_safe_message_retry(self):
        a=self.store.put_asset('xianyu','shop-1',png());b=self.store.put_asset('xianyu','shop-1',png());c=self.store.put_asset('xianyu','shop-2',png())
        self.assertEqual(a['id'],b['id']);self.assertNotEqual(a['id'],c['id'])
    def test_image_upload_reaches_task_as_attachment(self):
        asset=self.store.put_asset('xianyu','shop-1',png());m=self.incoming(content={'text':'解释图片','images':[asset['id']]})
        final=self.complete(m);self.assertEqual(final['state'],'succeeded');self.assertEqual(self.remote.calls[0][0],'upload');self.assertTrue(self.remote.calls[1][2])
    def test_append_image_upload_is_bound_to_existing_issue(self):
        first=self.complete(self.incoming());asset=self.store.put_asset('xianyu','shop-1',png())
        final=self.complete(self.incoming(content={'text':'图文追加','images':[asset['id']]}))
        uploads=[x for x in self.remote.calls if x[0]=='upload']
        self.assertEqual(uploads[-1][2],first['execution']['issue_id']);self.assertEqual(final['execution']['issue_id'],first['execution']['issue_id'])
    def test_same_conversation_appends_after_first_result(self):
        a=self.incoming();first=self.complete(a);b=self.incoming(reply_to=a['message_id']);second=self.complete(b)
        self.assertEqual(first['thread_id'],second['thread_id']);self.assertEqual(first['execution']['issue_id'],second['execution']['issue_id'])
        self.assertNotEqual(first['execution']['task_id'],second['execution']['task_id']);self.assertEqual([x[0] for x in self.remote.calls],['create','append'])
    def test_no_overlapping_turns_in_one_conversation(self):
        self.remote.auto_complete=False;a=self.incoming();b=self.incoming();self.tick()
        self.assertIsNone(self.store.claim());self.tick();self.assertEqual(self.store.message(b['id'])['state'],'queued')
        run=self.remote.task_rows[self.store.message(a['id'])['execution']['issue_id']][0];run['status']='completed';self.tick();self.tick();self.assertEqual([x[0] for x in self.remote.calls],['create','append'])
    def test_other_conversations_can_dispatch(self):
        self.remote.auto_complete=False;self.incoming();self.incoming(conversation_id='customer-2');self.tick();self.worker.process(self.store.claim())
        self.assertEqual(len(self.remote.issues),2)
    def test_short_messages_batch_together(self):
        self.store.save_route({**self.route,'max_batch':10},self.route['id']);a=self.incoming();b=self.incoming();fa=self.complete(a);fb=self.store.message(b['id'])
        self.assertEqual(fa['batch_id'],fb['batch_id']);self.assertEqual(len(self.remote.issues),1);self.assertEqual(len(self.store.results('xianyu')),2)
    def test_route_edit_does_not_move_queued_messages(self):
        a=self.incoming();updated=self.store.save_route({**self.route,'grouping':'message'},self.route['id']);b=self.incoming()
        self.assertEqual(self.store.message(a['id'])['route']['version'],1);self.assertEqual(b['route']['version'],updated['version']);self.assertNotEqual(a['group_key'],b['group_key'])
    def test_same_customer_different_business_items_do_not_mix(self):
        a=self.incoming(context={'business_key':'order-1'});b=self.incoming(context={'business_key':'order-2'})
        self.assertNotEqual(a['group_key'],b['group_key'])
        with self.assertRaises(Problem):self.incoming(reply_to=a['message_id'],context={'business_key':'order-2'})
    def test_explicit_new_task_forks_context(self):
        a=self.incoming();b=self.incoming(new_task=True);self.assertNotEqual(a['group_key'],b['group_key'])
    def test_reply_cannot_cross_conversations(self):
        a=self.incoming()
        with self.assertRaises(Problem):self.incoming(reply_to=a['message_id'],conversation_id='other')
    def test_reply_cannot_cross_route_versions(self):
        a=self.incoming();self.store.save_route({**self.route,'instruction':'新要求'},self.route['id'])
        with self.assertRaises(Problem):self.incoming(reply_to=a['message_id'])
    def test_out_of_order_reply_is_retained_then_unblocked(self):
        a=self.incoming(message_id='later',reply_to='first');self.assertEqual(a['state'],'blocked_reply')
        self.incoming(message_id='first');self.store.unblock_replies();self.assertEqual(self.store.message(a['id'])['state'],'queued')
    def test_out_of_order_reply_dispatches_parent_first(self):
        child=self.incoming(message_id='child',reply_to='parent');parent=self.incoming(message_id='parent');self.store.unblock_replies()
        self.tick();self.assertEqual(self.store.message(parent['id'])['state'],'waiting');self.assertEqual(self.store.message(child['id'])['state'],'queued')
    def test_reply_unblock_keeps_original_route_snapshot(self):
        child=self.incoming(message_id='child',reply_to='parent');self.incoming(message_id='parent')
        self.store.save_route({**self.route,'instruction':'new'},self.route['id']);self.store.unblock_replies()
        self.assertEqual(self.store.message(child['id'])['route']['version'],1);self.assertEqual(self.store.message(child['id'])['state'],'queued')
    def test_unresolved_reply_backlog_does_not_starve_resolvable_reply(self):
        for i in range(101):self.incoming(reply_to='absent-'+str(i),conversation_id='wait-'+str(i))
        child=self.incoming(reply_to='arrived');self.incoming(message_id='arrived');self.store.unblock_replies()
        self.assertEqual(self.store.message(child['id'])['state'],'queued')
    def test_self_reply_is_rejected(self):
        with self.assertRaises(Problem):self.incoming(message_id='self',reply_to='self')
    def test_cancel_before_dispatch_returns_terminal_result(self):
        m=self.incoming();self.store.cancel(m['id']);self.assertIsNone(self.store.claim());self.assertEqual(self.store.results('xianyu')[0]['state'],'cancelled')
    def test_reply_to_closed_old_turn_uses_current_generation(self):
        a=self.complete(self.incoming());self.store.close_thread(a['thread_id']);b=self.complete(self.incoming());c=self.complete(self.incoming(reply_to=a['message_id']))
        self.assertEqual(b['thread_id'],c['thread_id'])
    def test_explicit_thread_cannot_cross_account(self):
        a=self.complete(self.incoming())
        with self.assertRaises(Problem):self.incoming(account_id='shop-2',thread_id=a['thread_id'])
    def test_closed_thread_creates_new_generation(self):
        a=self.complete(self.incoming());self.store.close_thread(a['thread_id']);b=self.complete(self.incoming());self.assertNotEqual(a['thread_id'],b['thread_id'])
    def test_finished_multica_issue_creates_new_task(self):
        a=self.complete(self.incoming());self.remote.issues[a['execution']['issue_id']]['status']='done';b=self.complete(self.incoming());self.assertNotEqual(a['thread_id'],b['thread_id'])
    def test_expired_session_creates_new_task(self):
        a=self.complete(self.incoming())
        with self.store.db(True) as db:db.execute('UPDATE threads SET last_at=0 WHERE id=?',(a['thread_id'],))
        b=self.complete(self.incoming());self.assertNotEqual(a['thread_id'],b['thread_id'])
    def test_max_messages_rotates_and_limits_batch(self):
        self.store.save_route({**self.route,'max_messages':1,'max_batch':10},self.route['id']);a=self.incoming();b=self.incoming();fa=self.complete(a);fb=self.complete(b)
        self.assertNotEqual(fa['thread_id'],fb['thread_id']);self.assertTrue(all(t['message_count']==1 for t in self.store.threads()))
    def test_context_budget_rotates_session(self):
        a=self.complete(self.incoming())
        with self.store.db(True) as db:db.execute('UPDATE threads SET context_bytes=262144 WHERE id=?',(a['thread_id'],))
        b=self.complete(self.incoming());self.assertNotEqual(a['thread_id'],b['thread_id'])
    def test_lost_create_response_reconciles_without_duplicate(self):
        self.remote.drop_create_response=True;m=self.incoming();self.tick();self.assertEqual(self.store.batch(self.store.message(m['id'])['batch_id'])['status'],'reconciling')
        final=self.complete(m);self.assertEqual(final['state'],'succeeded');self.assertEqual(len(self.remote.issues),1)
    def test_lost_append_response_reconciles_without_duplicate(self):
        a=self.complete(self.incoming());self.remote.drop_comment_response=True;b=self.incoming();final=self.complete(b)
        self.assertEqual(final['state'],'succeeded');self.assertEqual(len(self.remote.comment_rows[a['execution']['issue_id']]),1)
    def test_crash_after_write_does_not_repost(self):
        m=self.incoming();b=self.store.claim();route=json.loads(b['route']);marker='<!-- channel-batch:'+b['id']+' -->'
        self.store.update_batch(b['id'],write_started=1,stage='create',lease_until=0,next_poll=0)
        self.remote.create_issue('Channel-'+b['id'],marker,route,[])
        restarted=Worker(Store(self.tmp.name),self.remote);restarted.process(self.store.claim());self.tick();self.assertEqual(self.store.message(m['id'])['state'],'succeeded');self.assertEqual(len(self.remote.issues),1)
    def test_unknown_write_stays_uncertain_and_blocks_followup(self):
        m=self.incoming();b=self.store.claim();self.store.update_batch(b['id'],write_started=1,stage='create',attempts=12,status='reconciling',lease_until=0,next_poll=0)
        self.worker.process(self.store.claim());self.assertEqual(self.store.message(m['id'])['state'],'uncertain');self.incoming();self.assertIsNone(self.store.claim())
    def test_timeout_does_not_release_context_or_claim_success(self):
        self.remote.auto_complete=False;m=self.incoming();self.tick();b=self.store.message(m['id'])['batch_id'];self.store.update_batch(b,deadline=0,next_poll=0,lease_until=0)
        self.worker.process(self.store.claim());self.assertEqual(self.store.message(m['id'])['state'],'attention');self.assertIsNone(self.store.message(m['id'])['result'])
    def test_wrong_run_does_not_satisfy_append(self):
        a=self.complete(self.incoming());self.remote.auto_complete=False;b=self.incoming();self.tick();self.tick();self.assertEqual(self.store.message(b['id'])['state'],'waiting')
        self.assertNotEqual(a['execution']['task_id'],self.store.message(b['id'])['execution']['task_id'])
    def test_outbox_ack_scoped_and_does_not_delete(self):
        m=self.complete(self.incoming());rows=self.store.results('xianyu');self.assertEqual(len(rows),1);self.assertEqual(rows[0]['event_id'],m['id'])
        with self.assertRaises(LookupError):self.store.acknowledge(rows[0]['id'],'zhihu')
        self.store.acknowledge(rows[0]['id'],'xianyu');self.assertTrue(self.store.results('xianyu')[0]['acknowledged']);self.assertEqual(self.store.results('xianyu',after=rows[0]['cursor']),[])
    def test_paused_source_keeps_queue(self):
        m=self.incoming();self.store.change_source('xianyu',{'enabled':False});self.assertIsNone(self.store.claim());self.assertEqual(self.store.message(m['id'])['state'],'queued')
        with self.assertRaises(Problem):self.store.authenticate(self.key)
    def test_rotating_source_invalidates_old_key(self):
        updated=self.store.change_source('xianyu',{'rotate':True})
        with self.assertRaises(Problem):self.store.authenticate(self.key)
        self.assertEqual(self.store.authenticate(updated['token']),'xianyu')
    def test_no_route_retains_message_and_manual_retry_matches_new_rule(self):
        self.store.save_route({**self.route,'enabled':False},self.route['id']);m=self.incoming();self.assertEqual(m['state'],'blocked_no_route')
        self.store.save_route({**self.route,'enabled':True},self.route['id']);self.store.retry(m['id']);self.assertEqual(self.complete(m)['state'],'succeeded')
    def test_cannot_retry_already_dispatched_message(self):
        m=self.complete(self.incoming())
        with self.assertRaises(Problem):self.store.retry(m['id'])
    def test_untrusted_context_does_not_trigger_other_agents(self):
        m=self.incoming(content={'text':'[@Other](mention://agent/'+AGENT_ID+') ``` malicious'})
        self.complete(m);body=next(iter(self.remote.issues.values()))['description'];self.assertNotIn('mention://agent/',body)
    def test_output_ignores_tools_and_thinking(self):
        task={'id':'t','agent_id':AGENT_ID,'result':None}
        output=extract_output(task,[],[{'type':'thinking','content':'private','seq':1},{'type':'text','content':'final','seq':2},{'type':'tool_result','content':'secret','seq':3}])
        self.assertEqual(output['text'],'final')


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,DemoMultica(),public_origin='https://channel.test');self.client=self.app.test_client();self.store=self.app.extensions['channel']
        self.key=self.store.source('source')['token'];self.store.save_route(dict(name='规则',agent_id=AGENT_ID,batch_seconds=0))
        self.headers={'Origin':'https://channel.test','X-App-Request':'1'};self.machine={'Authorization':'Bearer '+self.key}
    def tearDown(self):self.tmp.cleanup()
    def test_console_requires_login_and_origin(self):
        self.assertEqual(self.client.get('/').status_code,302);self.assertEqual(self.client.get('/api/sources').status_code,401)
        pw=Path(self.tmp.name,'admin-password').read_text();self.assertEqual(self.client.post('/api/login',json={'password':pw}).status_code,403)
        r=self.client.post('/api/login',json={'password':pw},headers=self.headers);self.assertEqual(r.status_code,200);self.assertIn('Secure',r.headers['Set-Cookie']);self.assertEqual(self.client.get('/api/meta').status_code,200)
    def test_machine_api_requires_key_not_cookie(self):
        self.assertEqual(self.client.post('/v1/messages',json={}).status_code,401)
        self.assertEqual(self.client.post('/v1/messages',json={},headers={**self.machine,'Origin':'https://evil.test'}).status_code,403)
    def test_machine_accepts_and_deduplicates(self):
        data={'account_id':'a','message_id':'m','content':{'text':'测试'}}
        a=self.client.post('/v1/messages',json=data,headers=self.machine);self.assertEqual(a.status_code,202)
        b=self.client.post('/v1/messages',json=data,headers=self.machine);self.assertEqual(b.status_code,200);self.assertTrue(b.json['duplicate'])
        self.assertEqual(self.client.get('/v1/messages/'+a.json['id'],headers=self.machine).status_code,200)
    def test_private_source_keys_not_listed_and_files_not_served(self):
        pw=Path(self.tmp.name,'admin-password').read_text();self.client.post('/api/login',json={'password':pw},headers=self.headers)
        self.assertNotIn(self.key,self.client.get('/api/sources').text);self.assertEqual(self.client.get('/.local/multica.json').status_code,404)
        self.assertIn("frame-ancestors 'none'",self.client.get('/').headers['Content-Security-Policy'])
    def test_upload_validates_file_and_scope(self):
        r=self.client.post('/v1/assets',data={'account_id':'a','file':(io.BytesIO(png()),'test.png')},headers=self.machine)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json['media_type'],'image/png')
    def test_invalid_json_and_unknown_cursor_rejected(self):
        self.assertEqual(self.client.post('/v1/messages',json=[],headers=self.machine).status_code,400)
        self.assertEqual(self.client.get('/v1/results?after=bad',headers=self.machine).status_code,400)

if __name__=='__main__':unittest.main()
