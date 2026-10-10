import concurrent.futures
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from channel_service.app import create_app
from channel_service.automation import Automation
from channel_service.demo import AGENT_ID, DemoMultica
from channel_service.domain import Problem, match_route, normalize_message
from channel_service.store import Store
from channel_service.worker import Worker


class GoalModel(DemoMultica):
    def __init__(self):
        super().__init__()
        self.decision = {'summary': '已确认示例商品支持包邮', 'requires_human': False,
                         'action': {'type': 'dm.reply', 'text': '可以包邮。您需要哪种规格？'}, 'followup_after_seconds': 300}

    def _run(self, issue_id, route, comment_id=None):
        super()._run(issue_id, route, comment_id)
        self.task_rows[issue_id][-1]['result'] = {'content': json.dumps(self.decision, ensure_ascii=False)}


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.key = self.store.source('xianyu')['token']
        self.store.source('other')
        self.auto = Automation(self.store)
        self.model = GoalModel()
        self.worker = Worker(self.store, self.model)
        self.goal = self.auto.save_goal(dict(name='包邮咨询', source_id='xianyu', account_id='shop',
            agent_id=AGENT_ID, objective='依据商品事实回答咨询，必要时跟进。', enabled=True,
            mode='automatic', hour_start=0, hour_end=24, min_interval=10))
        self.counter = 0

    def event(self, **changes):
        self.counter += 1
        data = dict(account_id='shop', event_id='incoming-' + str(self.counter), event_type='dm.received',
                    conversation_id='customer', content={'text': '这个支持包邮吗？'})
        data.update(changes)
        return self.auto.ingest('xianyu', data)[0]

    def process(self):
        for _ in range(6):
            self.auto.tick()
            with self.store.db(True) as db:
                db.execute('UPDATE batches SET next_poll=0,lease_until=0')
            batch = self.store.claim()
            if batch:
                self.worker.process(batch)
        self.auto.tick()

    def claim(self):
        self.auto.heartbeat('xianyu', dict(account_id='shop', executor_id='executor', capabilities=['dm.reply']))
        return self.auto.claim('xianyu', dict(account_id='shop', executor_id='executor'))['action']

    def receipt(self, action, **changes):
        data = dict(account_id='shop', claim_token=action['claim_token'], status='succeeded', external_id='platform-message-1')
        data.update(changes)
        return self.auto.receipt('xianyu', action['id'], data)

    def test_legacy_normalization_keeps_existing_message_fingerprints(self):
        payload = normalize_message(dict(account_id='a', message_id='m', content={'text': 'x'}), 'xianyu')
        self.assertNotIn('surface', payload)
        self.assertEqual(match_route(payload, self.store.routes()), None)

    def test_routing_can_separate_private_messages_and_comments(self):
        rule = self.store.save_route(dict(name='仅私信', agent_id=AGENT_ID, surfaces=['dm'], event_types=['dm.received']))
        message = normalize_message(dict(account_id='a', message_id='m', surface='dm', event_type='dm.received', content={'text': 'x'}), 'xianyu')
        self.assertEqual(match_route(message, [rule])['id'], rule['id'])
        message['surface'] = 'comment'
        self.assertIsNone(match_route(message, [rule]))

    def test_event_deduplicates_across_transport_and_conflicting_body_rejected(self):
        first = self.event(event_id='canonical')
        second = self.event(event_id='canonical', transport='webhook')
        self.assertEqual(first['id'], second['id'])
        with self.assertRaises(Problem):
            self.event(event_id='canonical', content={'text': '不同内容'})

    def test_event_cannot_forge_source_or_timer(self):
        for changes in ({'source_id': 'other'}, {'event_type': 'dm.followup_due'}, {'surface': 'notification'}):
            with self.assertRaises(Problem):
                self.event(**changes)

    def test_only_matching_execution_account_creates_a_job(self):
        event = self.event(account_id='another-shop')
        self.assertEqual(event['state'], 'unmatched')
        self.assertEqual(event['jobs'], [])

    def test_own_messages_do_not_trigger_reply_loops(self):
        event = self.event(direction='outgoing')
        self.process()
        self.assertEqual(event['state'], 'ignored_outgoing')
        self.assertEqual(self.auto.actions(), [])

    def test_agent_plan_becomes_account_bound_action_not_a_platform_call(self):
        self.event()
        self.process()
        action = self.auto.actions()[0]
        self.assertEqual((action['state'], action['account'], action['target']), ('ready', 'shop', 'dm:customer'))
        self.assertEqual(action['payload']['text'], self.model.decision['action']['text'])
        self.assertEqual(self.auto.cases()[0]['turns'], 0)
        self.assertEqual([x[0] for x in self.model.calls], ['create'])

    def test_review_mode_requires_adoption_before_claim(self):
        self.goal = self.auto.save_goal({**self.goal, 'mode': 'review'}, self.goal['id'])
        self.event()
        self.process()
        action = self.auto.actions()[0]
        self.assertEqual(action['state'], 'review')
        self.assertIsNone(self.claim())
        self.auto.decide_action(action['id'], True)
        self.assertEqual(self.claim()['id'], action['id'])

    def test_model_human_flag_and_missing_media_force_review(self):
        self.event(context={'media_unavailable': True})
        self.process()
        self.assertEqual(self.auto.actions()[0]['state'], 'review')

    def test_model_cannot_change_recipient(self):
        self.model.decision['action']['target'] = 'different-customer'
        self.event()
        self.process()
        self.assertEqual(self.auto.actions(), [])
        self.assertEqual(self.auto.cases()[0]['state'], 'needs_human')

    def test_disallowed_publish_action_is_rejected(self):
        self.model.decision['action'] = {'type': 'content.publish', 'title': 'x', 'text': 'x'}
        self.event()
        self.process()
        self.assertEqual(self.auto.actions(), [])

    def test_new_message_cancels_old_proposal_and_preserves_burst_context(self):
        first = self.event(content={'text': '我需要大号'})
        self.process()
        second = self.event(content={'text': '可以包邮吗？'})
        self.assertEqual(self.auto.actions()[0]['state'], 'cancelled')
        self.process()
        self.assertEqual(len(self.auto.actions()), 2)
        newest = self.store.messages()[0]['payload']
        self.assertEqual([x['text'] for x in newest['context']['messages']], ['我需要大号', '可以包邮吗？'])
        self.assertEqual(len(self.model.issues), 1)

    def test_stop_contact_cancels_proposals_without_model_call(self):
        self.event()
        self.process()
        self.event(content={'text': '不要再联系我了'})
        self.process()
        self.assertIsNone(self.claim())
        self.assertEqual(self.auto.cases()[0]['state'], 'stopped')
        self.assertEqual(len(self.model.calls), 1)

    def test_pausing_goal_cancels_unclaimed_actions(self):
        self.event()
        self.process()
        self.auto.save_goal({**self.goal, 'enabled': False}, self.goal['id'])
        self.assertEqual(self.auto.actions()[0]['state'], 'cancelled')
        self.assertIsNone(self.claim())

    def test_goal_route_can_only_be_changed_through_goal_configuration(self):
        route = self.store.routes()[0]
        with self.assertRaises(Problem):
            self.store.save_route(route, route['id'])

    def test_concurrent_executors_claim_at_most_once(self):
        self.event()
        self.process()
        self.auto.heartbeat('xianyu', dict(account_id='shop', executor_id='executor', capabilities=['dm.reply']))
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            values = list(pool.map(lambda _: self.auto.claim('xianyu', dict(account_id='shop', executor_id='executor'))['action'], range(6)))
        self.assertEqual(sum(x is not None for x in values), 1)

    def test_lease_expiry_blocks_account_and_late_receipt_resolves_original_action(self):
        self.event()
        self.process()
        action = self.claim()
        with self.store.db(True) as db:
            db.execute('UPDATE account_actions SET lease_until=0 WHERE id=?', (action['id'],))
        self.auto.tick()
        self.assertEqual(self.auto.actions()[0]['state'], 'uncertain')
        self.assertIsNone(self.claim())
        self.receipt(action)
        self.assertEqual(self.auto.actions()[0]['state'], 'succeeded')
        self.assertEqual(self.auto.cases()[0]['turns'], 1)

    def test_receipt_is_scoped_and_idempotent(self):
        self.event()
        self.process()
        action = self.claim()
        with self.assertRaises(LookupError):
            self.receipt(action, account_id='another-shop')
        with self.assertRaises(Problem):
            self.receipt(action, claim_token='incorrect')
        with self.assertRaises(Problem):
            self.receipt(action, external_id='')
        self.assertFalse(self.receipt(action)['duplicate'])
        self.assertTrue(self.receipt(action)['duplicate'])
        with self.assertRaises(Problem):
            self.receipt(action, external_id='different')

    def test_followup_clock_starts_after_receipt_and_fires_once_across_restart(self):
        self.event()
        self.process()
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM followup_timers').fetchone()[0], 0)
        action = self.claim()
        self.receipt(action)
        with self.store.db(True) as db:
            db.execute('UPDATE followup_timers SET due=0')
        self.auto.tick()
        Automation(self.store).tick()
        timers = [x for x in self.auto.events() if x['payload']['event_type'] == 'dm.followup_due']
        self.assertEqual(len(timers), 1)

    def test_customer_reply_cancels_waiting_followup(self):
        self.event()
        self.process()
        self.receipt(self.claim())
        self.event(content={'text': '好的，我看看'})
        with self.store.db(True) as db:
            db.execute('UPDATE followup_timers SET due=0')
        self.auto.tick()
        self.assertFalse(any(x['payload']['event_type'] == 'dm.followup_due' for x in self.auto.events()))

    def test_old_followup_cannot_override_a_newer_reply(self):
        original = self.event()
        self.process()
        self.event(content={'text': '我已经买好了'})
        value, duplicate = self.auto.ingest('xianyu', dict(account_id='shop', event_id='late-timer',
            event_type='dm.followup_due', conversation_id='customer', content={'text': '过期跟进'}),
            internal=True, only_goal=self.goal['id'], expected_latest=original['id'])
        self.assertIsNone(value)

    def test_source_pause_keeps_pending_jobs_and_due_followups_for_resume(self):
        self.event()
        self.store.change_source('xianyu', {'enabled': False})
        self.auto.tick()
        self.assertEqual(self.auto.events()[0]['jobs'][0]['state'], 'pending')
        self.store.change_source('xianyu', {'enabled': True})
        self.process()
        self.receipt(self.claim())
        self.store.change_source('xianyu', {'enabled': False})
        with self.store.db(True) as db:
            db.execute('UPDATE followup_timers SET due=0')
        self.auto.tick()
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT state FROM followup_timers').fetchone()[0], 'waiting')

    def test_case_limit_stops_further_actions(self):
        self.goal = self.auto.save_goal({**self.goal, 'case_limit': 1}, self.goal['id'])
        self.event()
        self.process()
        self.receipt(self.claim())
        self.event(content={'text': '还有别的规格吗？'})
        self.process()
        self.assertIsNone(self.claim())
        self.assertIn('上限', self.auto.actions()[0]['reason'])

    def test_same_account_daily_budget_is_shared_across_customers(self):
        self.goal = self.auto.save_goal({**self.goal, 'daily_limit': 1}, self.goal['id'])
        self.event()
        self.process()
        self.receipt(self.claim())
        self.event(conversation_id='other-customer')
        self.process()
        self.assertIsNone(self.claim())
        self.assertIn('额度', self.auto.actions()[0]['reason'])

    def test_business_endpoints_keep_source_and_browser_auth_boundaries(self):
        app = create_app(self.temp.name, self.model, auth=False)
        client = app.test_client()
        payload = dict(account_id='shop', event_id='api-event', event_type='dm.received', conversation_id='customer', content={'text': 'x'})
        self.assertEqual(client.post('/v1/events', json=payload).status_code, 401)
        headers = {'Authorization': 'Bearer ' + self.key}
        self.assertEqual(client.post('/v1/events', json=payload, headers=headers).status_code, 202)
        self.assertEqual(client.post('/v1/events', json=payload, headers={**headers, 'Origin': 'http://localhost'}).status_code, 403)
        self.assertEqual(client.get('/automation').status_code, 200)
        self.assertEqual(client.get('/api/automation').status_code, 200)


if __name__ == '__main__':
    unittest.main()
