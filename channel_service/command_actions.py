"""Authenticated command intents delivered through the existing account queue.

Replies reference observed inbound events, never synthesized inbound messages.
New publications are explicitly system command events. Connectors alone attest
platform effects; all old queue lease, contact-stop and receipt guards apply.
"""
import json
import time
from .domain import Problem, identifier
from .automation import ACTIONS, digest, dumps, uid

SCHEMA = '''CREATE TABLE IF NOT EXISTS command_intents(
 owner TEXT NOT NULL, request_id TEXT NOT NULL, goal_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, action_id TEXT NOT NULL UNIQUE,
 PRIMARY KEY(owner,request_id));'''


def initialize(automation):
    with automation.store.db() as db:
        db.executescript(SCHEMA)


def queue(automation, owner, goal_id, data):
    request_id = identifier(data.get('request_id'), '命令幂等标识', 100)
    kind = data.get('operation')
    target = identifier(data.get('target'), '目标资源', 200)
    if kind not in ACTIONS:
        raise Problem('命令操作没有实际执行适配')
    fingerprint = digest(dict(goal_id=goal_id, data=data))
    now = time.time()
    with automation.store.db(True) as db:
        authority = db.execute('SELECT * FROM control_goals WHERE goal_id=? AND owner=?', (goal_id, owner)).fetchone()
        if not authority:
            raise Problem('命令目标不属于当前指挥服务', 403)
        config = automation._goal(db.execute('SELECT * FROM goals WHERE id=?', (goal_id,)).fetchone())
        if not db.execute('SELECT 1 FROM sources WHERE id=? AND enabled=1', (config['source_id'],)).fetchone():
            raise Problem('执行来源已停用', 403)
        if not config['enabled'] or not automation.authorized(db, goal_id, now):
            raise Problem('目标未启用或授权已过期', 409)
        if data.get('revision') != authority['revision']:
            raise Problem('命令授权版本已变化', 409)
        context = json.loads(authority['context'])
        grant = context.get('execution_grant', {})
        if grant.get('expires_at', 0) <= now:
            raise Problem('目标操作授权已过期', 403)
        if kind not in grant.get('operations', []) or kind not in config['allowed_actions']:
            raise Problem('该目标未授权此操作', 403)
        if grant.get('source_id') != config['source_id'] or grant.get('account_id') != config['account_id']:
            raise Problem('目标授权账号不一致', 403)
        if data.get('source_id') != config['source_id'] or data.get('account_id') != config['account_id']:
            raise Problem('命令执行账号不一致', 403)
        if target not in grant.get('targets', []):
            raise Problem('目标资源不在明确授权范围内', 403)
        prior = db.execute('SELECT * FROM command_intents WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
        if prior:
            if prior['fingerprint'] != fingerprint:
                raise Problem('同一命令标识内容冲突', 409)
            return automation._action(db.execute('SELECT * FROM account_actions WHERE id=?', (prior['action_id'],)).fetchone())
        connector = db.execute('SELECT * FROM account_connectors WHERE source=? AND account=? AND last_seen>?',
                               (config['source_id'], config['account_id'], now - 120)).fetchone()
        if not connector or kind not in json.loads(connector['capabilities']):
            raise Problem('所选账号缺少在线操作执行器', 409)
        event_type = {'dm.reply': 'dm.received', 'comment.reply': 'comment.received'}.get(kind)
        if event_type:
            # Only original observed incoming contexts qualify for a reply.
            original = db.execute("SELECT * FROM business_events WHERE source=? AND account=? AND json_extract(payload,'$.target')=? AND json_extract(payload,'$.event_type')=? AND json_extract(payload,'$.direction')='incoming' ORDER BY seq DESC LIMIT 1",
                (config['source_id'], config['account_id'], target, event_type)).fetchone()
            if not original:
                raise Problem('没有该账号该目标的真实入站上下文', 403)
            observed = json.loads(original['payload'])
            if any(word in observed.get('content', {}).get('text', '') for word in config['stop_words']):
                raise Problem('入站上下文要求停止联系', 403)
            surface = observed['surface']
            context_ref = {'observed_event_id': original['id']}
        else:
            if not target.startswith('content:'):
                raise Problem('发布目标资源类型不一致')
            surface, context_ref = 'content', {}
        event_id = uid()
        event = dict(source_id=config['source_id'], account_id=config['account_id'],
                     target=target, event_type='command.requested', surface=surface, direction='system',
                     resource={'id': target.split(':', 1)[1]}, content={'text': '', 'images': []}, context=context_ref)
        db.execute('INSERT INTO business_events(id,source,account,external_id,fingerprint,payload,state,created) VALUES(?,?,?,?,?,?,?,?)',
            (event_id, config['source_id'], config['account_id'], 'command:' + request_id, digest(event), dumps(event), 'command_requested', now))
        action = {k: data[k] for k in ('text', 'title', 'images') if k in data}
        action['type'] = kind
        _, payload, _, _ = automation._validate_action(dict(action=action), config, event)
        case = db.execute('SELECT * FROM goal_cases WHERE goal_id=? AND target=?', (goal_id, target)).fetchone()
        if case and case['state'] in ('closed', 'stopped'):
            raise Problem('目标事项已结束或停止', 409)
        case_id = case['id'] if case else uid()
        db.execute('INSERT INTO goal_cases(id,goal_id,source,account,target,latest_event,updated) VALUES(?,?,?,?,?,?,?) ON CONFLICT(goal_id,target) DO UPDATE SET latest_event=excluded.latest_event,updated=excluded.updated',
            (case_id, goal_id, config['source_id'], config['account_id'], target, event_id, now))
        # Cancel only stale, unclaimed actions for this command goal/target.
        db.execute("UPDATE account_actions SET state='cancelled',reason='更新指挥行动',updated=? WHERE goal_id=? AND target=? AND state IN ('ready','review')", (now, goal_id, target))
        job_id, action_id = uid(), uid()
        db.execute('INSERT INTO goal_jobs(id,event_id,case_id,goal_id,config,state,created) VALUES(?,?,?,?,?,?,?)',
            (job_id, event_id, case_id, goal_id, dumps(config), 'decided', now))
        db.execute('INSERT INTO account_actions(id,job_id,goal_id,case_id,source,account,target,type,payload,state,created,updated,available) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (action_id, job_id, goal_id, case_id, config['source_id'], config['account_id'], target, kind, dumps(payload), 'ready', now, now, now))
        db.execute('INSERT INTO command_intents VALUES(?,?,?,?,?)', (owner, request_id, goal_id, fingerprint, action_id))
        automation.store.audit(db, 'command.intent', action_id, kind)
        return automation._action(db.execute('SELECT * FROM account_actions WHERE id=?', (action_id,)).fetchone())


def read(automation, owner, goal_id, request_id, cancel=False):
    with automation.store.db(True) as db:
        row = db.execute('SELECT a.* FROM command_intents c JOIN account_actions a ON a.id=c.action_id WHERE c.owner=? AND c.goal_id=? AND c.request_id=?', (owner, goal_id, request_id)).fetchone()
        if not row:
            raise LookupError()
        if cancel:
            db.execute("UPDATE account_actions SET state='cancelled',reason='指挥目标已撤销或暂停',updated=? WHERE id=? AND state IN ('ready','review')", (time.time(), row['id']))
            row = db.execute('SELECT * FROM account_actions WHERE id=?', (row['id'],)).fetchone()
        return automation._action(row)


def guard(automation, db, action, now):
    intent = db.execute('SELECT 1 FROM command_intents WHERE action_id=?', (action['id'],)).fetchone()
    if not intent:
        return None
    if not db.execute('SELECT 1 FROM sources WHERE id=? AND enabled=1', (action['source'],)).fetchone():
        return 'cancelled', '执行来源已停用'
    authority = db.execute('SELECT context FROM control_goals WHERE goal_id=?', (action['goal_id'],)).fetchone()
    grant = json.loads(authority['context']).get('execution_grant', {}) if authority else {}
    if (grant.get('expires_at', 0) <= now or grant.get('source_id') != action['source'] or
            grant.get('account_id') != action['account'] or action['type'] not in grant.get('operations', []) or
            action['target'] not in grant.get('targets', [])):
        return 'cancelled', '操作授权已过期或已撤销'
    job = db.execute('SELECT event_id FROM goal_jobs WHERE id=?', (action['job_id'],)).fetchone()
    event = db.execute('SELECT payload FROM business_events WHERE id=?', (job['event_id'],)).fetchone()
    original = json.loads(event['payload']).get('context', {}).get('observed_event_id')
    if original:
        event_type = {'dm.reply': 'dm.received', 'comment.reply': 'comment.received'}[action['type']]
        latest = db.execute("SELECT id FROM business_events WHERE source=? AND account=? AND json_extract(payload,'$.target')=? AND json_extract(payload,'$.event_type')=? AND json_extract(payload,'$.direction')='incoming' ORDER BY seq DESC LIMIT 1",
            (action['source'], action['account'], action['target'], event_type)).fetchone()
        if not latest or latest['id'] != original:
            return 'cancelled', '已有更新入站上下文，必须重新决策'
    return None
