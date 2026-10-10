import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from wsgiref.simple_server import make_server, WSGIRequestHandler
from flask import Flask
from channel_service.app import create_app
from channel_service.demo import AGENT_ID, DemoMultica
from sdk.channel_client import ChannelClient, ChannelError, compact_context
from sdk.flask_bridge import register_channel
from PIL import Image

class Quiet(WSGIRequestHandler):
    def log_message(self,*args):pass

class SdkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(Path(self.tmp.name)/'server',DemoMultica())
        self.store=self.app.extensions['channel'];key=self.store.source('workspace')['token']
        self.store.save_route(dict(name='test',agent_id=AGENT_ID,batch_seconds=0))
        self.server=make_server('127.0.0.1',0,self.app,handler_class=Quiet);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.path=Path(self.tmp.name)/'channel.json';self.path.write_text(json.dumps(dict(source_id='workspace',token=key,base_url='http://127.0.0.1:'+str(self.server.server_port))))
        self.client=ChannelClient(self.path)
    def tearDown(self):self.server.shutdown();self.thread.join();self.server.server_close();self.tmp.cleanup()
    def test_send_dedup_result_and_ack(self):
        a=self.client.send('owner','msg-12345','你好');b=self.client.send('owner','msg-12345','你好');self.assertEqual(a['id'],b['id']);self.assertTrue(b['duplicate'])
        worker=self.app.extensions['worker'];worker.process(self.store.claim())
        with self.store.db(True) as db:db.execute('UPDATE batches SET next_poll=0,lease_until=0')
        worker.process(self.store.claim());self.assertEqual(self.client.status(a['id'],'owner')['state'],'succeeded')
        result=self.client.results('owner')['items'][0];self.client.acknowledge(result['id']);self.assertTrue(self.client.results('owner')['items'][0]['acknowledged'])
        with self.assertRaises(ChannelError):self.client.status(a['id'],'other')
    def test_multipart_upload_is_idempotent(self):
        blob=io.BytesIO();Image.new('RGB',(5,5),'blue').save(blob,'PNG')
        a=self.client.upload('owner',blob.getvalue());b=self.client.upload('owner',blob.getvalue());self.assertEqual(a['id'],b['id'])
    def test_workspace_uses_captured_backend_account_and_trusted_record(self):
        class Platform:
            def identity(self):return {'id':'real-owner'}
        class Service:platform=Platform()
        app=Flask('bridge');seen=[]
        def record(item_id,owner):seen.append((item_id,owner));return {'id':item_id,'title':'当前账号内容'}
        register_channel(app,Service(),self.path,record)
        response=app.test_client().post('/api/channel/messages',json={'message_id':'workbench-12345','item_id':'owned-item','account_id':'forged-owner','text':'分析'})
        self.assertEqual(response.status_code,202);self.assertEqual(response.json['account'],'real-owner');self.assertEqual(seen,[('owned-item','real-owner')])
        self.assertEqual(response.json['payload']['surface'],'content')
        self.assertEqual(response.json['payload']['event_type'],'content.analysis_requested')
    def test_compaction_keeps_recent_messages_and_marks_truncation(self):
        value=compact_context({'messages':[{'text':'中'*1800,'id':str(i)} for i in range(20)],'item':{'description':'文'*4000}})
        self.assertTrue(value['context_truncated']);self.assertEqual(value['messages'][-1]['id'],'19');self.assertLess(len(json.dumps(value,ensure_ascii=False).encode()),24000)

if __name__=='__main__':unittest.main()
