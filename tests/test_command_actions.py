import json
import tempfile
import time
import unittest
from channel_service.automation import Automation
from channel_service.command_actions import queue, read
from channel_service.demo import AGENT_ID, DemoMultica
from channel_service.app import create_app
from channel_service.domain import Problem
from channel_service.store import Store


class CommandActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.source_key = self.store.source('synthetic')['token']
        self.auto = Automation(self.store)
        self.config = dict(name='synthetic goal', source_id='synthetic', account_id='fake-a',
            agent_id=AGENT_ID, objective='synthetic integration', enabled=True, mode='automatic',
            event_types=['content.requested'], allowed_actions=['dm.reply', 'comment.reply', 'content.publish'],
            hour_start=0, hour_end=24, max_followups=0)
        self.authority = dict(owner='hq', revision=1, mission_id='mission', assignment_id='assignment', lease_seconds=90,
            context={'execution_grant': dict(source_id='synthetic', account_id='fake-a',
                operations=['dm.reply', 'comment.reply', 'content.publish'],
                targets=['dm:c1', 'comment:r1', 'content:r1'], expires_at=time.time() + 60)})
        self.auto.save_goal(self.config, 'goal', control=self.authority)
        self.auto.heartbeat('synthetic', dict(account_id='fake-a', executor_id='fake-adapter',
                                            capabilities=['dm.reply', 'comment.reply', 'content.publish']))
        self.body = dict(request_id='action-1', revision=1, source_id='synthetic', account_id='fake-a',
                         operation='dm.reply', target='dm:c1', text='synthetic response')

    def incoming(self, account='fake-a', conversation='c1', event_id='in-1', **extra):
        return self.auto.ingest('synthetic', dict(account_id=account, event_id=event_id,
            event_type='dm.received', conversation_id=conversation, content={'text': 'synthetic inquiry'}, **extra))[0]

    def test_dispatch_receipt_and_idempotent_restart(self):
        self.incoming()
        queued = queue(self.auto, 'hq', 'goal', self.body)
        restarted = Automation(self.store)
        self.assertEqual(queue(restarted, 'hq', 'goal', self.body)['id'], queued['id'])
        claimed = self.auto.claim('synthetic', dict(account_id='fake-a', executor_id='fake-adapter'))['action']
        self.assertEqual(claimed['target'], 'dm:c1')
        self.assertEqual(claimed['account'], 'fake-a')
        self.auto.receipt('synthetic', claimed['id'], dict(account_id='fake-a', claim_token=claimed['claim_token'], status='succeeded', external_id='fake-platform-message'))
        result = read(self.auto, 'hq', 'goal', 'action-1')
        self.assertEqual(result['receipt']['external_id'], 'fake-platform-message')
        self.assertEqual(result['state'], 'succeeded')
        with self.store.db() as db:
            payload = json.loads(db.execute("SELECT payload FROM business_events WHERE state='command_requested'").fetchone()[0])
        self.assertEqual(payload['direction'], 'system')
        self.assertEqual(payload['event_type'], 'command.requested')
        changed = dict(self.body, text='changed')
        with self.assertRaises(Problem):
            queue(self.auto, 'hq', 'goal', changed)

    def test_no_fabricated_inbound_or_cross_account(self):
        self.incoming(account='fake-b')
        with self.assertRaises(Problem):
            queue(self.auto, 'hq', 'goal', self.body)
        self.incoming(event_id='outgoing', direction='outgoing')
        with self.assertRaises(Problem):
            queue(self.auto, 'hq', 'goal', self.body)
        self.incoming(event_id='real')
        for body in (dict(self.body, account_id='fake-b'), dict(self.body, target='dm:outside'), dict(self.body, revision=2)):
            with self.assertRaises(Problem):
                queue(self.auto, 'hq', 'goal', body)
        with self.assertRaises(Problem):
            queue(self.auto, 'other-owner', 'goal', self.body)

    def test_latest_context_and_expired_scope_cancel_at_claim(self):
        self.incoming()
        row = queue(self.auto, 'hq', 'goal', self.body)
        self.incoming(event_id='new-inbound')
        self.assertIsNone(self.auto.claim('synthetic', dict(account_id='fake-a', executor_id='fake-adapter'))['action'])
        self.assertEqual(read(self.auto, 'hq', 'goal', 'action-1')['state'], 'cancelled')
        queue(self.auto, 'hq', 'goal', dict(self.body, request_id='action-2'))
        with self.store.db(True) as db:
            context = self.authority['context']
            context['execution_grant']['expires_at'] = time.time() - 1
            db.execute('UPDATE control_goals SET context=? WHERE goal_id=?', (json.dumps(context), 'goal'))
        self.assertIsNone(self.auto.claim('synthetic', dict(account_id='fake-a', executor_id='fake-adapter'))['action'])
        self.assertEqual(read(self.auto, 'hq', 'goal', 'action-2')['state'], 'cancelled')

    def test_publication_command_and_pause(self):
        row = queue(self.auto, 'hq', 'goal', dict(self.body, operation='content.publish', target='content:r1', title='synthetic title'))
        self.assertEqual(row['type'], 'content.publish')
        self.authority['revision'] = 2
        self.config['enabled'] = False
        self.auto.save_goal(self.config, 'goal', control=self.authority)
        self.assertEqual(read(self.auto, 'hq', 'goal', 'action-1')['state'], 'cancelled')
        self.assertIsNone(self.auto.claim('synthetic', dict(account_id='fake-a', executor_id='fake-adapter'))['action'])

    def test_machine_endpoints_reject_source_and_browser_credentials(self):
        app = create_app(self.tmp.name, DemoMultica(), auth=False)
        client = app.test_client()
        path = '/control/v1/deployments/goal/actions'
        source_key = self.source_key
        self.assertEqual(client.post(path,json=self.body,headers={'Authorization':'Bearer '+source_key}).status_code,401)
        command_key = (self.store.directory / 'command-key').read_text().strip()
        self.assertEqual(client.post(path,json=self.body,headers={'Authorization':'Bearer '+command_key,'Origin':'http://localhost'}).status_code,401)
        self.assertEqual(client.get('/control/v1/catalog',headers={'Authorization':'Bearer '+command_key}).json['direct_goal_actions'],True)
