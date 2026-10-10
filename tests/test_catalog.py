"""Source-scoped model routing catalog and explicit route selection."""
import tempfile
import unittest
from channel_service.app import create_app
from channel_service.demo import AGENT_ID, DemoMultica
from channel_service.domain import Problem
from channel_service.store import Store


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.token = self.store.source('xhs', '小红书')['token']
        self.store.source('other', 'other')
        self.shared = self.store.save_route(dict(name='shared', agent_id=AGENT_ID, priority=100))
        self.chosen = self.store.save_route(dict(name='chosen', agent_id=AGENT_ID, sources=['xhs'], categories=['reply_draft'], priority=10))
        self.private = self.store.save_route(dict(name='other only', agent_id=AGENT_ID, sources=['other']))
        self.app = create_app(self.tmp.name, DemoMultica())
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + self.token}
    def tearDown(self):
        self.tmp.cleanup()
    def payload(self, **kwargs):
        result = dict(account_id='own', message_id='stable', category='reply_draft', content={'text':'synthetic'})
        result.update(kwargs)
        return result
    def test_catalog_only_exposes_available_source_routes_and_safe_fields(self):
        response = self.client.get('/v1/catalog', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual({r['id'] for r in body['routes']}, {self.shared['id'], self.chosen['id']})
        self.assertEqual(body['routes'][0]['agent']['model'], 'demo')
        self.assertNotIn('instruction', body['routes'][0])
        self.assertEqual(self.client.get('/v1/catalog').status_code, 401)
        self.assertEqual(self.client.get('/v1/catalog', headers={**self.headers,'Origin':'https://example.test'}).status_code, 403)
    def test_explicit_route_keeps_snapshot_and_idempotency(self):
        payload = self.payload(route_id=self.chosen['id'])
        first, duplicate = self.store.ingest('xhs', payload)
        self.assertFalse(duplicate)
        self.assertEqual(first['route']['id'], self.chosen['id'])
        self.assertEqual(self.store.ingest('xhs', payload)[0]['id'], first['id'])
        with self.assertRaises(Problem) as context:
            self.store.ingest('xhs', self.payload(route_id=self.shared['id']))
        self.assertEqual(context.exception.status, 409)
    def test_cannot_choose_foreign_or_incompatible_route(self):
        for payload in [self.payload(route_id=self.private['id']), self.payload(route_id=self.chosen['id'], category='content_analysis'), self.payload(route_id='unknown')]:
            with self.assertRaises(Problem) as context:
                self.store.ingest('xhs', payload)
            self.assertEqual(context.exception.status, 409)
    def test_legacy_messages_keep_priority_routing(self):
        message, _ = self.store.ingest('xhs', self.payload())
        self.assertEqual(message['route']['id'], self.shared['id'])

    def test_legacy_route_without_new_dimensions_is_listed(self):
        import json
        with self.store.db(True) as db:
            row = db.execute('SELECT config FROM routes WHERE id=?', (self.shared['id'],)).fetchone()
            config = json.loads(row[0]); config.pop('surfaces'); config.pop('event_types')
            db.execute('UPDATE routes SET config=? WHERE id=?', (json.dumps(config), self.shared['id']))
        response = self.client.get('/v1/catalog', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        route = next(r for r in response.get_json()['routes'] if r['id'] == self.shared['id'])
        self.assertEqual(route['surfaces'], ['*'])
        self.assertEqual(route['event_types'], ['*'])

    def test_dm_contract_and_legacy_contract_are_distinct(self):
        from channel_service.worker import Worker
        worker = Worker(self.store, DemoMultica())
        route = {'instruction':'draft only'}
        body = worker.body({'id':'synthetic'},route,[{'source_id':'xhs','surface':'dm','category':'reply_draft'}],{})
        self.assertIn('reply_text',body)
        self.assertIn('requires_human=false',body)
        self.assertIn('no_reply', body)
        self.assertIn('context.messages', body)
        self.assertIn('context.control_mode', body)
        self.assertIn('request_approval', body)
        self.assertIn('auto_approve', body)
        self.assertIn('full_access', body)
        self.assertIn('reply_messages', body)
        self.assertIn('1至4条', body)
        self.assertIn('image_number', body)
        manual = worker.body({'id':'synthetic'},route,[{'source_id':'xhs','category':'reply_draft'}],{})
        self.assertIn('"reply":',manual)
        self.assertNotIn('"reply_text":',manual)
