"""Retire obsolete account helpers after both workbenches have been migrated.

Preserve historical issues, threads and immutable batches. Remote mutation failures
are never retried automatically. Backups and the execution journal stay private.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'channel'))
from channel_service.multica import Multica
from channel_service.store import Store

RETIRED_AGENTS = {'户外运动号内容与评论', '户外运动号自然私信', '户外·场景技能验收'}
RETIRED_SKILLS = {'xhs-lifestyle-publisher', 'xhs-natural-expression',
                  'conversation-personality', 'xhs-outdoor-fitness', 'lady-s-man'}
ACTIVE_TASKS = {'pending', 'queued', 'running', 'in_progress', 'claimed', 'waiting'}


def save(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    remote = Multica(ROOT / 'channel/.local/multica.json')
    store = Store(ROOT / 'channel/.local')
    agents = remote.request('GET', '/api/agents')
    selected = [agent for agent in agents if agent['name'] in RETIRED_AGENTS]
    retired_ids = {agent['id'] for agent in selected}
    skills = remote.request('GET', '/api/skills')
    autopilots = remote.request('GET', '/api/autopilots')['autopilots']
    details = [remote.request('GET', '/api/agents/' + agent['id']) for agent in agents]
    removed_ids = {skill['id'] for skill in skills if skill['name'] in RETIRED_SKILLS}
    for agent in details:
        if agent['id'] not in retired_ids and any(skill['id'] in removed_ids for skill in agent.get('skills', [])):
            raise ValueError('Retired capability still mounted on a retained agent: ' + agent['name'])
    for agent in selected:
        tasks = remote.request('GET', '/api/agents/' + agent['id'] + '/tasks?limit=100')
        rows = tasks if isinstance(tasks, list) else tasks.get('tasks', tasks.get('items', []))
        if any(row.get('status') in ACTIVE_TASKS for row in rows):
            raise ValueError('Old agent still has active tasks: ' + agent['name'])
    with store.db() as db:
        batches = db.execute("SELECT route FROM batches WHERE status NOT IN ('succeeded','cancelled','failed_preflight')").fetchall()
        if any(json.loads(row['route']).get('agent_id') in retired_ids for row in batches):
            raise ValueError('Old agent still has an unsettled Channel batch')
    plan = {'archive_agents': sorted(agent['name'] for agent in selected),
            'delete_skills': sorted(skill['name'] for skill in skills if skill['name'] in RETIRED_SKILLS),
            'preserve': 'All historical issues and execution records; generic helper for other platforms'}
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    backup = ROOT / 'channel/.local/backups' / ('retire-account-agents-' + str(time.time_ns()))
    backup.mkdir(parents=True, mode=0o700)
    safe_agent_fields = ('id', 'name', 'description', 'instructions', 'skills', 'model', 'runtime_id')
    save(backup / 'agents.json', [{key: agent.get(key) for key in safe_agent_fields} for agent in details])
    save(backup / 'routes.json', store.routes())
    save(backup / 'skills.json', [remote.request('GET', '/api/skills/' + skill['id'])
                                for skill in skills if skill['name'] in RETIRED_SKILLS])
    save(backup / 'autopilots.json', [{key: item.get(key) for key in ('id', 'title', 'status', 'description', 'assignee_id')}
                                    for item in autopilots])
    journal = {**plan, 'changes': [], 'pending': None}

    def mutation(label, method, path, data=None):
        journal['pending'] = label
        save(backup / 'journal.json', journal)
        result = remote.request(method, path, data)
        journal['changes'].append(label)
        journal['pending'] = None
        save(backup / 'journal.json', journal)
        return result

    # Cross-platform helpers remain useful outside the two migrated workbenches.
    for route in store.routes():
        if route.get('goal_ids', ['*']) != ['*']:
            # Goal-owned snapshots are managed through the goal API, not routes.
            continue
        if route['agent_id'] in retired_ids:
            route['enabled'] = False
            store.save_route(route, route['id'])
        elif next((agent['name'] for agent in agents if agent['id'] == route['agent_id']), '') == '智能回复助手':
            sources = [source for source in route['sources'] if source not in {'xhs', 'xianyu'}]
            if len(sources) != len(route['sources']):
                route['sources'] = sources or route['sources']
                route['enabled'] = bool(sources)
                store.save_route(route, route['id'])
    for autopilot in autopilots:
        if autopilot.get('assignee_id') in retired_ids:
            mutation('archive automation: ' + autopilot['title'], 'DELETE', '/api/autopilots/' + autopilot['id'])
        elif 'xhs-lifestyle-publisher' in (autopilot.get('description') or ''):
            description = autopilot['description'].replace('xhs-lifestyle-publisher', 'social-content-kit 与 platform-xhs')
            mutation('replace publishing skill reference: ' + autopilot['title'], 'PATCH',
                     '/api/autopilots/' + autopilot['id'], {'description': description})
    for agent in selected:
        mutation('archive agent: ' + agent['name'], 'POST', '/api/agents/' + agent['id'] + '/archive', {})
    # Detach retired capabilities only; leave shared business skills attached.
    for agent in details:
        mounted = agent.get('skills', [])
        if not any(skill['id'] in removed_ids for skill in mounted):
            continue
        if agent['id'] not in retired_ids:
            raise ValueError('Retired capability still mounted on a retained agent: ' + agent['name'])
        mutation('detach retired skills: ' + agent['name'], 'PUT', '/api/agents/' + agent['id'] + '/skills',
                 {'skill_ids': [skill['id'] for skill in mounted if skill['id'] not in removed_ids and skill.get('enabled')]})
    for skill in skills:
        if skill['id'] in removed_ids:
            mutation('delete skill: ' + skill['name'], 'DELETE', '/api/skills/' + skill['id'])
    remaining_agents = remote.request('GET', '/api/agents')
    remaining_skills = remote.request('GET', '/api/skills')
    if any(agent['name'] in RETIRED_AGENTS for agent in remaining_agents):
        raise ValueError('Archived agent still appears in active list')
    if any(skill['name'] in RETIRED_SKILLS for skill in remaining_skills):
        raise ValueError('Retired skill still appears in skill list')
    journal['verified'] = True
    save(backup / 'journal.json', journal)
    print(json.dumps({'verified': True, **plan}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
