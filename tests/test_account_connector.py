import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk'))
from xianyu_connector import XianyuConnector


class Workbench:
    def __init__(self):
        self.account = 'shop'
        self.messages = [{'id': 'old-message', 'text': '历史咨询', 'outgoing': False}]
        self.sends = []
        self.drop_response = False
        self.operation_visible = False

    def identity(self):
        return self.account

    def request(self, path, data=None):
        if path == '/api/conversations':
            return {'items': [{'id': 'customer', 'peer_id': 'peer'}]}
        if path.endswith('/messages'):
            return {'items': self.messages}
        if path.endswith('/send'):
            self.sends.append(data)
            if self.drop_response:
                raise TimeoutError()
            return {'id': 'local-job'}
        if path == '/api/jobs/local-job':
            return {'status': 'completed', 'result': {'message_id': 'platform-ack'}}
        if path == '/api/operations/channel-action-1' and self.operation_visible:
            return {'id': 'local-job', 'status': 'completed', 'result': {'message_id': 'platform-ack'}}
        raise AssertionError(path)


class Channel:
    def __init__(self):
        self.events = []
        self.receipts = []
        self.fail_event = False
        self.fail_receipt = False
        self.action = None

    def heartbeat(self, *args):
        pass

    def event(self, **data):
        if self.fail_event:
            raise ValueError('temporary')
        self.events.append(data)

    def claim_action(self, *args):
        action, self.action = self.action, None
        return action

    def action_receipt(self, account, action, **receipt):
        if self.fail_receipt:
            raise ValueError('temporary')
        self.receipts.append(receipt)


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.web = Workbench()
        self.channel = Channel()
        self.config = {'account_id': 'shop', 'journal': str(Path(self.temp.name) / 'journal.sqlite3'), 'execute': True}
        self.connector = XianyuConnector(self.config, self.web, self.channel)
        self.addCleanup(self.connector.db.close)

    def action(self, **changes):
        value = dict(id='action-1', claim_token='synthetic-lease', source='xianyu', account='shop', type='dm.reply', target='dm:customer', payload={'text': '合成回复', 'images': []})
        value.update(changes)
        return value

    def test_first_scan_baselines_history_and_only_new_incoming_is_forwarded(self):
        self.connector.observe()
        self.assertEqual(self.channel.events, [])
        self.web.messages += [{'id': 'new-message', 'text': '现在咨询', 'outgoing': False}, {'id': 'sent', 'text': '自己发送', 'outgoing': True}]
        self.connector.observe()
        self.connector.observe()
        self.assertEqual(len(self.channel.events), 1)
        self.assertEqual(self.channel.events[0]['text'], '现在咨询')

    def test_failed_event_delivery_stays_in_local_outbox_until_success(self):
        self.connector.observe()
        self.web.messages += [{'id': 'new', 'text': '咨询', 'outgoing': False}]
        self.channel.fail_event = True
        with self.assertRaises(ValueError):
            self.connector.observe()
        self.channel.fail_event = False
        self.connector.observe()
        self.assertEqual(len(self.channel.events), 1)

    def test_changed_account_stops_observation_and_execution(self):
        self.web.account = 'different'
        with self.assertRaises(ValueError):
            self.connector.step()
        self.assertEqual(self.channel.events, [])
        self.assertEqual(self.web.sends, [])

    def test_success_uses_existing_request_id_and_platform_receipt(self):
        self.channel.action = self.action()
        self.connector.step()
        self.connector.step()
        self.assertEqual(len(self.web.sends), 1)
        self.assertEqual(self.web.sends[0]['request_id'], 'channel-action-1')
        self.assertEqual(self.web.sends[0]['expected_account_id'], 'shop')
        self.assertEqual(self.channel.receipts[0]['external_id'], 'platform-ack')

    def test_lost_send_response_is_uncertain_and_never_resent(self):
        self.channel.action = self.action()
        self.web.drop_response = True
        self.connector.step()
        self.connector.step()
        self.connector.step()
        self.assertEqual(len(self.web.sends), 1)
        self.assertEqual(self.channel.receipts[0]['status'], 'uncertain')

    def test_restart_after_write_intent_reports_uncertain_without_sending(self):
        self.connector.db.execute('INSERT INTO pending VALUES(?,?,?,?,?)', ('action-1', json.dumps(self.action()), None, 'intent', None))
        self.connector.db.commit()
        self.connector.recover()
        self.assertEqual(self.web.sends, [])
        self.assertEqual(self.channel.receipts[0]['status'], 'uncertain')

    def test_lost_response_keeps_original_lease_and_late_ack_resolves_without_resending(self):
        self.channel.action = self.action()
        self.web.drop_response = True
        self.connector.step()
        self.connector.step()
        row = self.connector.db.execute('SELECT * FROM pending').fetchone()
        self.assertEqual(row['state'], 'uncertain')
        self.assertEqual(json.loads(row['action'])['claim_token'], 'synthetic-lease')
        self.web.operation_visible = True
        self.connector.step()
        self.assertEqual([x['status'] for x in self.channel.receipts], ['uncertain', 'succeeded'])
        self.assertEqual(len(self.web.sends), 1)
        self.assertIsNone(self.connector.db.execute('SELECT 1 FROM pending').fetchone())

    def test_receipt_retry_does_not_repeat_platform_send(self):
        self.channel.action = self.action()
        self.connector.step()
        self.channel.fail_receipt = True
        with self.assertRaises(ValueError):
            self.connector.step()
        self.channel.fail_receipt = False
        self.connector.step()
        self.assertEqual(len(self.web.sends), 1)
        self.assertEqual(len(self.channel.receipts), 1)

    def test_image_messages_are_marked_for_human_handling(self):
        self.connector.observe()
        self.web.messages += [{'id': 'image', 'text': '', 'kind': 'image', 'images': ['https://example.invalid/image'], 'outgoing': False}]
        self.connector.observe()
        self.assertTrue(self.channel.events[0]['context']['media_unavailable'])

    def test_observation_only_mode_never_claims_action(self):
        self.connector.config['execute'] = False
        self.channel.action = self.action()
        self.connector.step()
        self.assertEqual(self.web.sends, [])
        self.assertIsNotNone(self.channel.action)


if __name__ == '__main__':
    unittest.main()
