import tempfile
import unittest
from flask import Flask
from werkzeug.test import Client
from werkzeug.wrappers import Response
from channel_service.workspace_identity import WorkspaceIdentity

class SharedAccessTests(unittest.TestCase):
 def test_explicit_application_grant_uses_shared_workspace_and_revokes_immediately(self):
  with tempfile.TemporaryDirectory() as root:
   app=Flask(__name__)
   @app.get('/api/shared')
   def shared():return {'workspace':'original-shared-data'}
   identities={'2':{'id':2,'username':'colleague','app_access':{'channel':True}},'3':{'id':3,'username':'ordinary','app_access':{'channel':False}}}
   def factory(path):raise AssertionError('Shared grants must not create an empty private tenant')
   gate=WorkspaceIdentity(app,root,'channel',factory,1,verify=lambda token:identities.get(token),owner_only=True)
   app.wsgi_app=gate;client=Client(app,Response)
   self.assertEqual(client.get('/api/shared').status_code,401)
   client.set_cookie('ops_workspace_session','2')
   self.assertEqual(client.get('/api/shared').json,{'workspace':'original-shared-data'})
   self.assertFalse(gate.apps)
   identities['2']['app_access']['channel']=False
   self.assertEqual(client.get('/api/shared').status_code,403)
   self.assertIn('https://d.58nft.net/permissions?app=channel',client.get('/').headers['Location'])
   client.set_cookie('ops_workspace_session','3')
   self.assertEqual(client.get('/api/shared',headers={'X-User-ID':'1','X-Role':'admin'}).status_code,403)
