"""Account-scoped routing uses only synthetic IDs and temporary databases."""
import json
import tempfile
import unittest

from channel_service.app import create_app
from channel_service.demo import AGENT_ID, DemoMultica
from channel_service.domain import Problem, match_route, normalize_message, normalize_route
from channel_service.store import Store


class AccountRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.token = self.store.source('synthetic', 'Synthetic source')['token']
        self.store.source('other-synthetic')
        self.client = create_app(self.tmp.name, DemoMultica()).test_client()

    def route(self, **changes):
        return self.store.save_route(dict(name='Account route', agent_id=AGENT_ID,
                                         sources=['synthetic'], **changes))

    def payload(self, **changes):
        return dict(account_id='account-a', message_id='same-id',
                    content={'text': 'Synthetic message'}, **changes)

    def test_default_and_legacy_config_remain_wildcard(self):
        route = self.route()
        self.assertEqual(route['account_ids'], ['*'])
        with self.store.db(True) as db:
            legacy = dict(route)
            legacy.pop('account_ids')
            db.execute('UPDATE routes SET config=? WHERE id=?', (json.dumps(legacy), route['id']))
        message = normalize_message(self.payload(), 'synthetic')
        self.assertEqual(match_route(message, self.store.routes())['id'], route['id'])
        response = self.client.get('/v1/catalog', headers={'Authorization': 'Bearer ' + self.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['routes'][0]['account_ids'], ['*'])

    def test_exact_account_and_source_are_both_required(self):
        route = self.route(account_ids=['account-a', 'account-b'])
        for account in ('account-a', 'account-b'):
            data = self.payload()
            data['account_id'] = account
            self.assertEqual(match_route(normalize_message(data, 'synthetic'), [route])['id'], route['id'])
        data = self.payload()
        data['account_id'] = 'account-a-extra'
        self.assertIsNone(match_route(normalize_message(data, 'synthetic'), [route]))
        self.assertIsNone(match_route(normalize_message(self.payload(), 'other-synthetic'), [route]))

    def test_explicit_selection_cannot_bypass_account_or_fall_back(self):
        private = self.route(account_ids=['account-b'], priority=100)
        fallback = self.route(priority=10)
        with self.assertRaises(Problem) as caught:
            self.store.ingest('synthetic', self.payload(route_id=private['id']))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.ingest('synthetic', self.payload())[0]['route']['id'], fallback['id'])

    def test_snapshot_and_source_account_idempotency_survive_route_change(self):
        route = self.route(account_ids=['account-a'])
        first, duplicate = self.store.ingest('synthetic', self.payload(route_id=route['id']))
        self.assertFalse(duplicate)
        changed = self.store.save_route({**route, 'account_ids': ['account-b']}, route['id'])
        repeated, duplicate = self.store.ingest('synthetic', self.payload(route_id=route['id']))
        self.assertTrue(duplicate)
        self.assertEqual(repeated['id'], first['id'])
        self.assertEqual(repeated['route']['account_ids'], ['account-a'])
        self.assertEqual(repeated['route']['version'], route['version'])
        self.assertEqual(changed['version'], route['version'] + 1)
        data = self.payload(route_id=route['id'])
        data['account_id'] = 'account-b'
        second, duplicate = self.store.ingest('synthetic', data)
        self.assertFalse(duplicate)
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual(second['route']['account_ids'], ['account-b'])
        other, duplicate = self.store.ingest('other-synthetic', self.payload())
        self.assertFalse(duplicate)
        self.assertNotEqual(other['id'], first['id'])
        self.assertEqual(other['state'], 'blocked_no_route')
        data['content'] = {'text': 'Changed synthetic message'}
        with self.assertRaises(Problem) as caught:
            self.store.ingest('synthetic', data)
        self.assertEqual(caught.exception.status, 409)

    def test_selector_matches_inbound_id_limits_and_rejects_invalid_lists(self):
        account = 'a' * 160
        normalized = normalize_route(dict(name='Long ID', agent_id=AGENT_ID, account_ids=[' ' + account[:-2] + ' ']))
        self.assertEqual(normalized['account_ids'], [account[:-2]])
        route = self.route(account_ids=[account])
        data = self.payload()
        data['account_id'] = account
        self.assertIsNotNone(match_route(normalize_message(data, 'synthetic'), [route]))
        for selector in ([], '*', None, [''], [42], ['a' * 161], ['a'] * 31):
            with self.subTest(selector=selector), self.assertRaises(Problem):
                self.route(account_ids=selector)


if __name__ == '__main__':
    unittest.main()
