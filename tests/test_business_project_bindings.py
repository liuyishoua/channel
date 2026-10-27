import tempfile
import unittest
from channel_service.store import Store
from channel_service.project_bindings import bind
from channel_service.domain import Problem
from channel_service.demo import AGENT_ID
from channel_service.multica import Multica


class Remote:
    def request(self,method,path,data=None):
        return {'description':'Synthetic HQGoal-goal-project','id':'project'}
    def issue(self,key):
        return {'description':'HQGoal-goal-main','project_id':'project'}


class BusinessProjectBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.store.source('synthetic')
        self.route=self.store.save_route({'name':'owned agent','agent_id':AGENT_ID,'sources':['synthetic'],'account_ids':['a','b'],'batch_seconds':0})
        self.fields={'source_id':'synthetic','account_id':'a','message_id':'business-action','action_id':'action','goal_id':'goal','project_id':'project','parent_issue_id':'main'}

    def message(self,account='a',context=None):
        return self.store.ingest('synthetic',{'account_id':account,'message_id':'business-action','content':{'text':'Synthetic task'},'context':context or {}})[0]

    def test_owner_binding_applies_native_project_parent_and_survives_restart(self):
        bind(self.store,Remote(),'hq',self.fields)
        self.store=Store(self.tmp.name)
        m=self.message(context={'project_id':'attacker','parent_issue_id':'foreign'})
        self.assertEqual(m['route']['project_id'],'project');self.assertEqual(m['route']['parent_issue_id'],'main')
        self.assertEqual(self.message(context={'project_id':'attacker','parent_issue_id':'foreign'})['id'],m['id'])
        class Capture(Multica):
            def __init__(self):self.sent=None
            def request(self,method,path,data=None):self.sent=data;return {'id':'new'}
        adapter=Capture();adapter.create_issue('task','body',m['route'],[])
        self.assertEqual(adapter.sent['project_id'],'project');self.assertEqual(adapter.sent['parent_issue_id'],'main')

    def test_context_cannot_bind_and_other_account_cannot_borrow_binding(self):
        bind(self.store,Remote(),'hq',self.fields)
        m=self.message('b',{'project_id':'project'})
        self.assertFalse(m['route']['project_id'])

    def test_mismatched_project_and_binding_owner_rejected(self):
        bad=dict(self.fields,project_id='foreign')
        with self.assertRaises(Problem):bind(self.store,Remote(),'hq',bad)
        bind(self.store,Remote(),'hq',self.fields)
        with self.assertRaises(Problem):bind(self.store,Remote(),'other',self.fields)


if __name__=='__main__':unittest.main()
