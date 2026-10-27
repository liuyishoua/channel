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
        self.assertEqual(body['routes'][0]['execution_mode'],'issue')
        self.assertEqual(body['routes'][0]['priority'],100)
        self.assertNotIn('instruction', body['routes'][0])
        self.assertEqual(len(body['agents']), 1)
        self.assertEqual(body['agents'][0]['id'], AGENT_ID)
        self.assertNotIn('runtime_config', body['agents'][0])
        self.assertNotIn('instruction', body['agents'][0])
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

    def test_live_agent_catalog_includes_new_unrouted_agent_and_refreshes_name(self):
        remote = self.app.extensions['multica']
        second = {'id': '22222222-2222-4222-8222-222222222222', 'name': 'New unrouted Agent'}
        remote.agents = lambda: [dict(id=AGENT_ID, name='existing'), dict(second)]
        body = self.client.get('/v1/catalog', headers=self.headers).get_json()
        self.assertIn(second, body['agents'])
        self.assertNotIn(second['id'], {route['agent_id'] for route in body['routes']})
        second['name'] = 'Renamed Agent'
        self.assertIn(second, self.client.get('/v1/catalog', headers=self.headers).get_json()['agents'])
        route = self.client.post('/v1/bindings', headers=self.headers,
            json={'agent_id': second['id'], 'account_id': 'own', 'purpose': 'dm'}).get_json()['route']
        self.assertEqual(route['agent']['name'], second['name'])
        self.assertEqual(route['sources'], ['xhs'])
        self.assertEqual(route['account_ids'], ['own'])
        self.assertEqual(self.store.ingest('xhs', self.payload(route_id=route['id'], surface='dm', event_type='dm.received'))[0]['route']['agent_id'], second['id'])
        with self.assertRaises(Problem):
            self.store.ingest('other', self.payload(route_id=route['id'], surface='dm', event_type='dm.received'))
        with self.assertRaises(Problem):
            self.store.ingest('xhs', self.payload(account_id='other-account', route_id=route['id'], surface='dm', event_type='dm.received'))

    def test_binding_is_idempotent_and_rejects_missing_agent_or_bad_scope(self):
        payload = {'agent_id': AGENT_ID, 'account_id': 'own', 'purpose': 'dm'}
        first = self.client.post('/v1/bindings', headers=self.headers, json=payload)
        self.assertEqual(first.status_code, 200)
        repeated = self.client.post('/v1/bindings', headers=self.headers, json=payload)
        self.assertEqual(first.get_json()['route'], repeated.get_json()['route'])
        for bad, code in [({'agent_id': 'missing'}, 409), ({'account_id': ''}, 400), ({'purpose': 'goal_decision'}, 400)]:
            self.assertEqual(self.client.post('/v1/bindings', headers=self.headers, json={**payload, **bad}).status_code, code)
        self.assertEqual(self.client.post('/v1/bindings', json=payload).status_code, 401)
        self.assertEqual(self.client.post('/v1/bindings', headers={**self.headers, 'Origin': 'https://example.test'}, json=payload).status_code, 403)

    def test_catalog_failure_does_not_return_cached_agent_list(self):
        from channel_service.multica import RemoteError
        self.assertEqual(self.client.get('/v1/catalog', headers=self.headers).status_code, 200)
        def unavailable():
            raise RemoteError(503)
        self.app.extensions['multica'].agents = unavailable
        response = self.client.get('/v1/catalog', headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('agents', response.get_json())

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

    def test_xianyu_reply_contract_preserves_all_control_modes_without_affecting_other_sources(self):
        from channel_service.worker import Worker
        worker = Worker(self.store, DemoMultica())
        for surface in ('dm', 'comment'):
            body = worker.body({'id': 'synthetic'}, {'instruction': 'draft only'},
                [{'source_id': 'xianyu', 'surface': surface, 'category': 'reply_draft'}], {})
            self.assertIn('"reply_text":', body)
            for mode in ('request_approval', 'auto_approve', 'full_access'):
                self.assertIn(mode, body)
            self.assertIn('只有 handoff 才 requires_human=true', body)
        other = worker.body({'id': 'synthetic'}, {'instruction': 'draft only'},
            [{'source_id': 'other', 'surface': 'dm', 'category': 'reply_draft'}], {})
        self.assertIn('"requires_human":true', other)
        self.assertNotIn('本次为闲鱼', other)


class AdditionalPlatformBindingTests(unittest.TestCase):
    def test_platform_purpose_routes_execute_only_with_correct_account_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            for source in ('douyin','weibo','bilibili','kuaishou','jd','zhihu'):
                store.source(source,source)
                purposes=['dm'] if source=='douyin' else ['content','comment','research']
                for purpose in purposes:
                    route=store.bind_agent(source,'synthetic-own',purpose,{'id':AGENT_ID,'name':'unrouted'})
                    payload=dict(account_id='synthetic-own',message_id=source+'-'+purpose,category=route['categories'][0],surface=route['surfaces'][0],event_type=route['event_types'][0],route_id=route['id'],content={'text':'synthetic'})
                    event,_=store.ingest(source,payload)
                    self.assertEqual(event['route']['agent_id'],AGENT_ID)
                    with self.assertRaises(Problem):store.ingest(source,{**payload,'account_id':'foreign'})
                    with self.assertRaises(Problem):store.ingest('foreign-source',payload)
