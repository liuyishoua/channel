"""Narrow machine interface for an independent goal command service.

Source keys and browser sessions never grant these privileges. The command
service receives no platform secrets and cannot acknowledge platform receipts.
"""
import json
import time
from flask import Blueprint, g, request
from .domain import Problem, identifier, integer, inspect_context


def register_control(app, automation):
    bp = Blueprint('control', __name__)
    store = automation.store

    @bp.get('/control/v1/catalog')
    def catalog():
        adapter = app.extensions['multica']
        return dict(protocol=1, sources=store.sources(), connectors=automation.connectors(),
                    agents=adapter.agents(), multica_url=adapter.public_url)

    @bp.put('/control/v1/deployments/<goal_id>')
    def deploy(goal_id):
        identifier(goal_id, '执行规则标识', 80)
        data = request.json
        context = data.get('context', {})
        if not isinstance(context, dict) or len(json.dumps(context, ensure_ascii=False)) > 10000:
            raise Problem('指挥上下文必须是最多 10000 字的对象')
        inspect_context(context)
        authority = dict(owner=g.command_owner, revision=integer(data.get('revision'), '指令版本', 1, 2147483647),
            mission_id=identifier(data.get('mission_id'), '业务目标标识', 80),
            assignment_id=identifier(data.get('assignment_id'), '分工标识', 80), context=context,
            lease_seconds=integer(data.get('lease_seconds', 90), '指挥授权秒数', 30, 120))
        with store.db() as db:
            old = db.execute('SELECT * FROM control_goals WHERE goal_id=?', (goal_id,)).fetchone()
            if old and any(old[k] != authority[k] for k in ('owner', 'mission_id', 'assignment_id')):
                raise Problem('不能改变执行规则归属', 409)
        return automation.save_goal(data.get('goal', {}), goal_id, control=authority)

    @bp.post('/control/v1/renewals')
    def renewals():
        items = request.json.get('items')
        if not isinstance(items, list) or len(items) > 500 or any(not isinstance(x, dict) for x in items):
            raise Problem('每次续约最多 500 条')
        rows = [(identifier(x.get('goal_id'), '执行规则', 80), integer(x.get('revision'), '指令版本', 1, 2147483647)) for x in items]
        renewed = []
        with store.db(True) as db:
            for goal_id, revision in rows:
                updated = db.execute("UPDATE control_goals SET valid_until=? WHERE goal_id=? AND revision=? AND owner=? AND EXISTS(SELECT 1 FROM goals WHERE id=? AND json_extract(config,'$.enabled')=1)", (time.time() + 90, goal_id, revision, g.command_owner, goal_id))
                if updated.rowcount:
                    renewed.append(goal_id)
        return dict(renewed=renewed)

    @bp.get('/control/v1/state')
    def state():
        try:
            after = int(request.args.get('after', '0'))
            if after < 0:
                raise ValueError()
        except ValueError:
            raise Problem('游标必须是非负整数')
        with store.db() as db:
            db.execute('BEGIN')
            controlled = [dict(x) for x in db.execute('SELECT * FROM control_goals WHERE owner=?', (g.command_owner,))]
            changes = db.execute('SELECT c.* FROM control_changes c JOIN control_goals g ON g.goal_id=c.goal_id WHERE g.owner=? AND c.seq>? ORDER BY c.seq LIMIT 500', (g.command_owner, after)).fetchall()
            actions = []
            for action_id in dict.fromkeys(x['object_id'] for x in changes):
                row = db.execute('SELECT * FROM account_actions WHERE id=?', (action_id,)).fetchone()
                value = automation._action(row)
                job = db.execute('SELECT event_id,channel_event,config FROM goal_jobs WHERE id=?', (row['job_id'],)).fetchone()
                value.update(event_id=job['event_id'], channel_event=job['channel_event'])
                actions.append(value)
            stats = [dict(x) for x in db.execute('SELECT a.goal_id,a.state,count(*) AS count FROM account_actions a JOIN control_goals g ON g.goal_id=a.goal_id WHERE g.owner=? GROUP BY a.goal_id,a.state', (g.command_owner,))]
            cases = [dict(x) for x in db.execute('SELECT c.* FROM goal_cases c JOIN control_goals g ON g.goal_id=c.goal_id WHERE g.owner=? ORDER BY c.updated DESC LIMIT 200', (g.command_owner,))]
            # Expose conflicts, never credentials. The command service owns only its projections.
            unmanaged = [automation._goal(x) for x in db.execute('SELECT * FROM goals WHERE id NOT IN (SELECT goal_id FROM control_goals)')]
        return dict(actions=actions, stats=stats, cases=cases,
            deployments=[{k: v for k, v in x.items() if k not in ('fingerprint', 'context')} for x in controlled],
            unmanaged_goals=unmanaged, next_cursor=changes[-1]['seq'] if changes else after, has_more=len(changes) == 500)

    @bp.post('/control/v1/actions/<action_id>/decision')
    def decision(action_id):
        with store.db() as db:
            row = db.execute('SELECT 1 FROM account_actions a JOIN control_goals g ON g.goal_id=a.goal_id WHERE a.id=? AND g.owner=?', (action_id, g.command_owner)).fetchone()
        if not row:
            raise LookupError()
        if not isinstance(request.json.get('approve'), bool):
            raise Problem('approve 必须是布尔值')
        return automation.decide_action(action_id, request.json['approve'])

    app.register_blueprint(bp)
