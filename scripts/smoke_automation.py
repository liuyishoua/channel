"""Opt-in real model plan test. Synthetic account; no account executor or send."""
import argparse
import json
from pathlib import Path
import time
import uuid

import requests

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        parser.error('Use --execute only for the user-requested model integration acceptance')
    base = 'https://d.58nft.net:9456'
    session = requests.Session()
    session.trust_env = False
    session.headers.update({'Origin': base, 'X-App-Request': '1'})
    def api(path, data=None, method='POST'):
        response = session.request(method if data is not None else 'GET', base + path, json=data, timeout=25)
        if not response.ok:
            raise RuntimeError('Acceptance API failed HTTP ' + str(response.status_code))
        return response.json()
    api('/api/login', {'password': (ROOT / '.local/admin-password').read_text().strip()})
    agents = api('/api/meta')['agents']
    agent = next((a for a in agents if '回复' in a['name']), agents[0])
    goal = api('/api/goals', dict(name='合成验收 · 客服与跟进', source_id='xianyu', account_id='channel-goal-acceptance',
        agent_id=agent['id'], objective='仅用虚构商品资料回答合成客户问题，形成私信回复方案；不执行发送，不虚构承诺。',
        persona='用一句简洁自然的中文回答，一次只推进一个问题。', knowledge='示例商品99元，支持包邮，有蓝色和白色。具体发货时间未知，需要人工确认。',
        enabled=True, mode='review', hour_start=0, hour_end=24, max_followups=2))
    results = []
    try:
        for body in ['示例商品99元，可以包邮吗？', '那我想选蓝色，能选吗？']:
            event = api('/api/business-events', dict(source_id='xianyu', account_id=goal['account_id'], event_id='acceptance-' + str(uuid.uuid4()),
                event_type='dm.received', conversation_id='synthetic-customer', transport='manual', content={'text': body, 'images': []}))
            deadline = time.time() + 240
            while time.time() < deadline:
                state = api('/api/automation')
                record = next(x for x in state['events'] if x['id'] == event['id'])
                job = record['jobs'][0]
                if job['state'] == 'needs_human':
                    raise RuntimeError('Model proposal failed validation: ' + job['error'])
                if job['state'] == 'decided':
                    message = api('/api/messages/' + job['channel_event'])
                    action = next((a for a in state['actions'] if a['job_id'] == job['id']), None)
                    assert message['state'] == 'succeeded'
                    assert action and action['state'] == 'review' and action['type'] == 'dm.reply'
                    results.append(dict(event_id=event['id'], goal_id=goal['id'], case_id=job['case_id'],
                        channel_event=job['channel_event'], issue_id=message['execution']['issue_id'], task_id=message['execution']['task_id'],
                        proposed_action=action['payload'], execution_state=action['state'], decision=message['result']['output']['structured']))
                    break
                time.sleep(2)
            else:
                raise RuntimeError('Model proposal timed out')
        assert len({x['issue_id'] for x in results}) == 1
        assert len({x['task_id'] for x in results}) == 2
        report = dict(passed=True, synthetic_account=True, social_actions_executed=0, goal_paused_after_test=True, turns=results)
        (ROOT / 'docs/automation-model-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        api('/api/goals/' + goal['id'], {**goal, 'enabled': False}, 'PUT')


if __name__ == '__main__':
    main()
