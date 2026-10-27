"""Durable business events, goals and account-scoped action delivery.

This module never calls a social platform. Trusted account connectors own those
side effects and report their exact receipts through the source API.
"""
import hashlib
import json
import secrets
import time
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .domain import Problem, digest, dumps, identifier, inspect_context, integer, normalize_message, normalize_route
from sdk.channel_client import ChannelError, compact_context

SCHEMA = '''
CREATE TABLE IF NOT EXISTS control_goals(goal_id TEXT PRIMARY KEY,owner TEXT NOT NULL,revision INTEGER NOT NULL,mission_id TEXT NOT NULL,assignment_id TEXT NOT NULL,context TEXT NOT NULL,fingerprint TEXT NOT NULL,valid_until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS control_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,goal_id TEXT NOT NULL,object_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY,version INTEGER NOT NULL,route_id TEXT NOT NULL,config TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS business_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,external_id TEXT NOT NULL,fingerprint TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,UNIQUE(source,account,external_id));
CREATE TABLE IF NOT EXISTS goal_cases(id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,target TEXT NOT NULL,latest_event TEXT,state TEXT NOT NULL DEFAULT 'active',summary TEXT NOT NULL DEFAULT '',turns INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL,UNIQUE(goal_id,target));
CREATE TABLE IF NOT EXISTS goal_jobs(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,case_id TEXT NOT NULL,goal_id TEXT NOT NULL,config TEXT NOT NULL,channel_event TEXT,state TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,UNIQUE(goal_id,event_id));
CREATE TABLE IF NOT EXISTS account_actions(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,job_id TEXT NOT NULL UNIQUE,goal_id TEXT NOT NULL,case_id TEXT NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,target TEXT NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL,available REAL NOT NULL,lease_until REAL NOT NULL DEFAULT 0,claim_hash TEXT,executor TEXT,receipt TEXT,followup_seconds INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER IF NOT EXISTS controlled_action_insert AFTER INSERT ON account_actions WHEN EXISTS(SELECT 1 FROM control_goals WHERE goal_id=NEW.goal_id) BEGIN INSERT INTO control_changes(goal_id,object_id) VALUES(NEW.goal_id,NEW.id); END;
CREATE TRIGGER IF NOT EXISTS controlled_action_update AFTER UPDATE ON account_actions WHEN EXISTS(SELECT 1 FROM control_goals WHERE goal_id=NEW.goal_id) BEGIN INSERT INTO control_changes(goal_id,object_id) VALUES(NEW.goal_id,NEW.id); END;
CREATE INDEX IF NOT EXISTS action_queue ON account_actions(source,account,state,available,seq);
CREATE TABLE IF NOT EXISTS account_connectors(source TEXT NOT NULL,account TEXT NOT NULL,executor TEXT NOT NULL,capabilities TEXT NOT NULL,last_seen REAL NOT NULL,PRIMARY KEY(source,account));
CREATE TABLE IF NOT EXISTS contact_blocks(source TEXT NOT NULL,account TEXT NOT NULL,target TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(source,account,target));
CREATE TABLE IF NOT EXISTS followup_timers(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,event_id TEXT NOT NULL,action_id TEXT UNIQUE NOT NULL,due REAL NOT NULL,state TEXT NOT NULL,created REAL NOT NULL);
'''

EVENTS = {
    'dm.received': 'dm', 'dm.followup_due': 'dm', 'dm.sent': 'dm',
    'comment.received': 'comment', 'notification.received': 'notification',
    'content.requested': 'content', 'content.published': 'content',
}
ACTIONS = {'dm.reply', 'comment.reply', 'content.publish'}


def uid():
    return str(uuid.uuid4())


def text(value, name, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        raise Problem(f'{name} 最多 {maximum} 字')
    inspect_context(value)
    return value.strip()


def goal_config(data, source_ids):
    source = identifier(data.get('source_id'), 'source_id', 64)
    if source not in source_ids:
        raise Problem('请选择已接入来源')
    events = data.get('event_types', ['dm.received', 'dm.followup_due'])
    actions = data.get('allowed_actions', ['dm.reply'])
    if not isinstance(events, list) or not events or not set(events) <= EVENTS.keys():
        raise Problem('目标事件类型无效')
    if not isinstance(actions, list) or not actions or not set(actions) <= ACTIONS:
        raise Problem('目标动作类型无效')
    mode = data.get('mode', 'review')
    if mode not in ('review', 'automatic'):
        raise Problem('执行模式无效')
    enabled = data.get('enabled', False)
    if not isinstance(enabled, bool):
        raise Problem('enabled 必须是布尔值')
    objective = text(data.get('objective', ''), '业务目标', 2000)
    if not objective:
        raise Problem('请填写业务目标')
    stop_words = data.get('stop_words', ['不要再联系', '退订', '别发了'])
    if not isinstance(stop_words, list) or not 1 <= len(stop_words) <= 20:
        raise Problem('请配置 1–20 条停止联系用语')
    stop_words = [identifier(x, '停止联系用语', 40) for x in stop_words]
    start = integer(data.get('hour_start', 9), '开始小时', 0, 23)
    end = integer(data.get('hour_end', 21), '结束小时', 1, 24)
    if end <= start:
        raise Problem('结束小时必须晚于开始小时（北京时间）')
    return dict(source_id=source, account_id=identifier(data.get('account_id'), '执行账号'),
                name=identifier(data.get('name'), '目标名称', 80), objective=objective,
                agent_id=identifier(data.get('agent_id'), 'agent_id', 80),
                persona=text(data.get('persona', ''), '沟通风格', 1000),
                knowledge=text(data.get('knowledge', ''), '业务知识', 6000),
                event_types=events, allowed_actions=actions, mode=mode, enabled=enabled,
                priority=integer(data.get('priority', 50), '目标优先级', 0, 100),
                daily_limit=integer(data.get('daily_limit', 50), '账号每日动作上限', 1, 1000),
                min_interval=integer(data.get('min_interval', 60), '账号最短动作间隔', 10, 86400),
                case_limit=integer(data.get('case_limit', 20), '单事项动作上限', 1, 100),
                max_followups=integer(data.get('max_followups', 2), '连续跟进上限', 0, 5),
                hour_start=start, hour_end=end, stop_words=stop_words)


class Automation:
    def __init__(self, store):
        self.store = store
        self.tick_lock = threading.Lock()
        with store.db() as db:
            db.executescript(SCHEMA)
        from .command_actions import initialize
        initialize(self)

    def goals(self):
        with self.store.db() as db:
            values = []
            for row in db.execute('SELECT * FROM goals ORDER BY updated DESC'):
                value = self._goal(row)
                control = db.execute('SELECT owner,mission_id,assignment_id,valid_until FROM control_goals WHERE goal_id=?', (row['id'],)).fetchone()
                if control:
                    value['command'] = dict(control)
                values.append(value)
            return values

    def _goal(self, row):
        return dict(json.loads(row['config']), id=row['id'], version=row['version'], route_id=row['route_id'])

    def authorized(self, db, goal_id, now=None):
        authority = db.execute('SELECT valid_until FROM control_goals WHERE goal_id=?', (goal_id,)).fetchone()
        return not authority or authority['valid_until'] > (time.time() if now is None else now)

    def dispatch_guard(self, batch_id):
        with self.store.db() as db:
            jobs = db.execute('SELECT j.* FROM goal_jobs j JOIN messages m ON m.id=j.channel_event JOIN control_goals c ON c.goal_id=j.goal_id WHERE m.batch_id=?', (batch_id,)).fetchall()
            for job in jobs:
                goal = self._goal(db.execute('SELECT * FROM goals WHERE id=?', (job['goal_id'],)).fetchone())
                case = db.execute('SELECT * FROM goal_cases WHERE id=?', (job['case_id'],)).fetchone()
                if not goal['enabled'] or goal['version'] != json.loads(job['config'])['version'] or case['state'] in ('closed', 'stopped') or case['latest_event'] != job['event_id']:
                    return 'cancelled'
                if not self.authorized(db, job['goal_id']):
                    return 'waiting_authority'
        return None

    def save_goal(self, data, goal_id=None, *, control=None):
        config = goal_config(data, {x['id'] for x in self.store.sources()})
        now = time.time()
        with self.store.db(True) as db:
            old = db.execute('SELECT * FROM goals WHERE id=?', (goal_id,)).fetchone() if goal_id else None
            authority = db.execute('SELECT * FROM control_goals WHERE goal_id=?', (goal_id,)).fetchone()
            if authority and control is None:
                raise Problem('该执行规则由指挥部管理，请在指挥部修改或暂停目标', 409)
            if control:
                fingerprint = digest(dict(config=config, context=control['context']))
                if old and not authority:
                    raise Problem('不能接管已有本地目标', 409)
                if authority and any(authority[k] != control[k] for k in ('owner', 'mission_id', 'assignment_id')):
                    raise Problem('不能改变执行规则归属', 409)
                if authority and authority['owner'] != control['owner']:
                    raise Problem('执行规则所属指挥部不一致', 403)
                if authority and (control['revision'] < authority['revision'] or (control['revision'] == authority['revision'] and fingerprint != authority['fingerprint'])):
                    raise Problem('过期指令或同一版本内容冲突', 409)
                if authority and control['revision'] == authority['revision']:
                    db.execute('UPDATE control_goals SET valid_until=? WHERE goal_id=?', (now + control['lease_seconds'] if config['enabled'] else 0, goal_id))
                    return self._goal(old)
                db.execute('INSERT INTO control_goals VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(goal_id) DO UPDATE SET revision=excluded.revision,context=excluded.context,fingerprint=excluded.fingerprint,valid_until=excluded.valid_until',
                    (goal_id, control['owner'], control['revision'], control['mission_id'], control['assignment_id'], dumps(control['context']), fingerprint, now + control['lease_seconds'] if config['enabled'] else 0))
            elif goal_id and not old:
                raise LookupError()
            if old and any(config[k] != json.loads(old['config'])[k] for k in ('source_id', 'account_id')):
                raise Problem('执行账号不能修改，请创建独立目标', 409)
            goal_id = goal_id or uid()
            route_id = old['route_id'] if old else uid()
            version = old['version'] + 1 if old else 1
            route = normalize_route(dict(name='目标 · ' + config['name'], agent_id=config['agent_id'],
                sources=[config['source_id']], categories=['goal_decision'], goal_ids=[goal_id],
                priority=100, grouping='conversation', batch_seconds=0, max_batch=1,
                instruction='围绕 context.goal 的业务目标提出动作方案。只返回结构化方案，不执行社交平台操作。'), route_id, version)
            db.execute('INSERT INTO routes VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,config=excluded.config,updated=excluded.updated',
                       (route_id, version, dumps(route), now))
            db.execute('INSERT INTO goals VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,config=excluded.config,updated=excluded.updated',
                       (goal_id, version, route_id, dumps(config), old['created'] if old else now, now))
            # Editing invalidates unclaimed proposals; in-flight effects remain visible.
            db.execute("UPDATE account_actions SET state='cancelled',reason='目标配置已修改',updated=? WHERE goal_id=? AND state IN ('review','ready')", (now, goal_id))
            db.execute("UPDATE followup_timers SET state='cancelled' WHERE case_id IN (SELECT id FROM goal_cases WHERE goal_id=?) AND state='waiting'", (goal_id,))
            self.store.audit(db, 'goal.saved', goal_id, f'version {version}; {config["mode"]}; enabled={config["enabled"]}')
        return dict(config, id=goal_id, version=version, route_id=route_id)

    def normalize_event(self, source, data, internal=False):
        if data.get('source_id', source) != source:
            raise Problem('凭据与事件来源不一致', 403)
        event_type = data.get('event_type')
        if event_type not in EVENTS or (event_type == 'dm.followup_due' and not internal):
            raise Problem('事件类型无效；跟进唤醒只能由服务生成')
        surface = EVENTS[event_type]
        if data.get('surface', surface) != surface:
            raise Problem('业务入口与事件类型不一致')
        direction = data.get('direction', 'incoming')
        if direction not in ('incoming', 'outgoing', 'system'):
            raise Problem('消息方向无效')
        account = identifier(data.get('account_id'), 'account_id')
        external_id = identifier(data.get('event_id'), 'event_id')
        resource = data.get('resource', {})
        if not isinstance(resource, dict):
            raise Problem('resource 必须是对象')
        resource = {k: identifier(resource[k], 'resource.' + k, optional=True) for k in ('id', 'parent_id', 'author_id') if k in resource}
        conversation = identifier(data.get('conversation_id'), 'conversation_id', optional=True)
        if surface == 'dm' and not conversation:
            raise Problem('私信事件必须有稳定会话标识')
        if surface != 'dm' and not resource.get('id'):
            raise Problem('评论、通知或内容事件必须有业务资源标识')
        target = surface + ':' + (conversation if surface == 'dm' else resource['id'])
        message = normalize_message(dict(account_id=account, message_id=external_id, content=data.get('content'), context=data.get('context', {})), source)
        transport = data.get('transport', 'manual')
        if transport not in ('manual', 'poll', 'webhook', 'socket', 'timer', 'import'):
            raise Problem('采集方式无效')
        return dict(source_id=source, account_id=account, event_id=external_id, event_type=event_type,
                    surface=surface, direction=direction, conversation_id=conversation, resource=resource,
                    content=message['content'], context=message['context'], target=target, transport=transport)

    def ingest(self, source, data, internal=False, only_goal=None, expected_latest=None):
        payload = self.normalize_event(source, data, internal)
        now = time.time()
        fingerprint = digest({k: v for k, v in payload.items() if k != 'transport'})
        with self.store.db(True) as db:
            if not db.execute('SELECT 1 FROM sources WHERE id=? AND enabled=1', (source,)).fetchone():
                raise Problem('来源已停用', 403)
            old = db.execute('SELECT * FROM business_events WHERE source=? AND account=? AND external_id=?', (source, payload['account_id'], payload['event_id'])).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Problem('同一事件 ID 的业务内容发生变化', 409)
                return self._event(db, old), True
            if expected_latest:
                current_case = db.execute('SELECT latest_event,state FROM goal_cases WHERE goal_id=? AND target=?', (only_goal, payload['target'])).fetchone()
                if not current_case or current_case['latest_event'] != expected_latest or current_case['state'] in ('closed', 'stopped'):
                    return None, False
            for asset in payload['content']['images']:
                if not db.execute('SELECT 1 FROM assets WHERE id=? AND source=? AND account=?', (asset, source, payload['account_id'])).fetchone():
                    raise Problem('事件图片不属于当前账号', 403)
            event_id = uid()
            if db.execute("SELECT count(*) FROM goal_jobs WHERE state IN ('pending','waiting')").fetchone()[0] >= 20000:
                raise Problem('目标待处理队列已满，请保留事件 ID 重试', 429)
            goals = sorted([self._goal(x) for x in db.execute('SELECT * FROM goals')], key=lambda x: (-x['priority'], x['id']))
            matched = next((x for x in goals if x['enabled'] and x['source_id'] == source and x['account_id'] == payload['account_id'] and payload['event_type'] in x['event_types'] and (not only_goal or x['id'] == only_goal)), None)
            ignored = payload['direction'] == 'outgoing' or payload['event_type'] in ('dm.sent', 'content.published')
            state = 'ignored_outgoing' if ignored else 'unmatched' if not matched else 'accepted' if self.authorized(db, matched['id'], now) else 'waiting_authority'
            db.execute('INSERT INTO business_events(id,source,account,external_id,fingerprint,payload,state,created) VALUES(?,?,?,?,?,?,?,?)',
                       (event_id, source, payload['account_id'], payload['event_id'], fingerprint, dumps(payload), state, now))
            if not ignored and payload['event_type'] == 'dm.received':
                db.execute('UPDATE goal_cases SET latest_event=?,updated=? WHERE source=? AND account=? AND target=?', (event_id, now, source, payload['account_id'], payload['target']))
                db.execute("UPDATE followup_timers SET state='cancelled' WHERE state='waiting' AND case_id IN (SELECT id FROM goal_cases WHERE source=? AND account=? AND target=?)", (source, payload['account_id'], payload['target']))
                db.execute("UPDATE account_actions SET state='cancelled',reason='收到更新消息',updated=? WHERE source=? AND account=? AND target=? AND state IN ('review','ready')", (now, source, payload['account_id'], payload['target']))
                # Stops persist across goal changes and apply before any model call.
                words = {w for goal in goals if goal['source_id'] == source and goal['account_id'] == payload['account_id'] for w in goal['stop_words']}
                if any(word in payload['content']['text'] for word in words):
                    db.execute('INSERT OR REPLACE INTO contact_blocks VALUES(?,?,?,?,?)', (source, payload['account_id'], payload['target'], '对方明确要求停止联系', now))
                    db.execute("UPDATE goal_cases SET state='stopped',updated=? WHERE source=? AND account=? AND target=?", (now, source, payload['account_id'], payload['target']))
            blocked = db.execute('SELECT 1 FROM contact_blocks WHERE source=? AND account=? AND target=?', (source, payload['account_id'], payload['target'])).fetchone()
            if blocked:
                db.execute("UPDATE business_events SET state='stopped' WHERE id=?", (event_id,))
            elif matched and not ignored:
                case = db.execute('SELECT * FROM goal_cases WHERE goal_id=? AND target=?', (matched['id'], payload['target'])).fetchone()
                case_id = case['id'] if case else uid()
                db.execute('INSERT INTO goal_cases(id,goal_id,source,account,target,latest_event,updated) VALUES(?,?,?,?,?,?,?) ON CONFLICT(goal_id,target) DO UPDATE SET latest_event=excluded.latest_event,updated=excluded.updated',
                           (case_id, matched['id'], source, payload['account_id'], payload['target'], event_id, now))
                if not case or case['state'] not in ('closed', 'stopped'):
                    db.execute('INSERT INTO goal_jobs(id,event_id,case_id,goal_id,config,state,created) VALUES(?,?,?,?,?,?,?)', (uid(), event_id, case_id, matched['id'], dumps(matched), 'pending', now))
                else:
                    db.execute("UPDATE business_events SET state='case_closed' WHERE id=?", (event_id,))
            self.store.audit(db, 'business_event.accepted', event_id, state)
            return self._event(db, db.execute('SELECT * FROM business_events WHERE id=?', (event_id,)).fetchone()), False

    def _event(self, db, row):
        return dict(row, payload=json.loads(row['payload']), jobs=[dict(x) for x in db.execute('SELECT id,goal_id,case_id,channel_event,state,error FROM goal_jobs WHERE event_id=?', (row['id'],))], fingerprint=None)

    def events(self):
        with self.store.db() as db:
            return [self._event(db, x) for x in db.execute('SELECT * FROM business_events ORDER BY seq DESC LIMIT 100')]

    def cases(self):
        with self.store.db() as db:
            return [dict(x) for x in db.execute('SELECT * FROM goal_cases ORDER BY updated DESC LIMIT 100')]

    def close_case(self, case_id):
        with self.store.db(True) as db:
            if not db.execute('SELECT 1 FROM goal_cases WHERE id=?', (case_id,)).fetchone():
                raise LookupError()
            db.execute("UPDATE goal_cases SET state='closed',updated=? WHERE id=?", (time.time(), case_id))
            db.execute("UPDATE account_actions SET state='cancelled',reason='事项已人工结束' WHERE case_id=? AND state IN ('review','ready')", (case_id,))
            db.execute("UPDATE followup_timers SET state='cancelled' WHERE case_id=? AND state='waiting'", (case_id,))
            self.store.audit(db, 'goal_case.closed', case_id)
        return {'ok': True}

    def actions(self, source=None, account=None):
        with self.store.db() as db:
            rows = db.execute('SELECT * FROM account_actions WHERE (? IS NULL OR source=?) AND (? IS NULL OR account=?) ORDER BY seq DESC LIMIT 100', (source, source, account, account))
            return [self._action(x) for x in rows]

    def _action(self, row):
        return {k: (json.loads(v) if k in ('payload', 'receipt') and v else v) for k, v in dict(row).items() if k != 'claim_hash'}

    def heartbeat(self, source, data):
        account = identifier(data.get('account_id'), '执行账号')
        executor = identifier(data.get('executor_id'), '执行器标识', 80)
        capabilities = data.get('capabilities', [])
        if not isinstance(capabilities, list) or not set(capabilities) <= ACTIONS:
            raise Problem('执行器能力无效')
        with self.store.db(True) as db:
            old = db.execute('SELECT * FROM account_connectors WHERE source=? AND account=?', (source, account)).fetchone()
            if old and old['executor'] != executor and old['last_seen'] > time.time() - 120:
                raise Problem('该账号已有在线执行器', 409)
            db.execute('INSERT OR REPLACE INTO account_connectors VALUES(?,?,?,?,?)', (source, account, executor, dumps(capabilities), time.time()))
        return {'ok': True}

    def connectors(self):
        with self.store.db() as db:
            return [dict(x, capabilities=json.loads(x['capabilities']), online=x['last_seen'] > time.time() - 120) for x in db.execute('SELECT * FROM account_connectors')]

    def _validate_action(self, decision, config, event):
        if not isinstance(decision, dict):
            raise Problem('模型未返回动作对象')
        inspect_context(decision)
        action = decision.get('action', {'type': 'none'})
        if not isinstance(action, dict):
            raise Problem('action 必须是对象')
        kind = action.get('type')
        summary = text(decision.get('summary', ''), '决策摘要', 2000)
        if kind in ('none', 'handoff'):
            return kind, {}, summary, 0
        if kind not in config['allowed_actions']:
            raise Problem('模型提出未授权动作')
        expected = {'dm.reply': 'dm', 'comment.reply': 'comment', 'content.publish': 'content'}
        if event['surface'] != expected[kind]:
            raise Problem('不能从通知或其他入口跨越业务边界执行')
        if set(action) - {'type', 'text', 'title', 'images'}:
            raise Problem('动作不能携带账号、收件人、链接或任意执行参数')
        body = text(action.get('text', ''), '动作正文', 3000)
        title = text(action.get('title', ''), '作品标题', 100)
        images = action.get('images', [])
        if not isinstance(images, list) or len(images) > 6 or not all(isinstance(x, str) for x in images):
            raise Problem('动作图片无效')
        if not body and not images:
            raise Problem('动作缺少内容')
        if kind == 'content.publish' and not title:
            raise Problem('发布作品必须有标题')
        with self.store.db() as db:
            for image in images:
                if not db.execute('SELECT 1 FROM assets WHERE id=? AND source=? AND account=?', (image, event['source_id'], event['account_id'])).fetchone():
                    raise Problem('模型引用了未授权图片')
        delay = decision.get('followup_after_seconds', 0)
        if delay != 0:
            integer(delay, '跟进等待秒数', 300, 604800)
            if kind != 'dm.reply' or config['max_followups'] == 0 or 'dm.followup_due' not in config['event_types']:
                raise Problem('此目标未启用跟进')
        return kind, {'text': body, 'title': title, 'images': images}, summary, delay

    def tick(self):
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            self._tick()
        finally:
            self.tick_lock.release()

    def _tick(self):
        now = time.time()
        with self.store.db(True) as db:
            db.execute("UPDATE account_actions SET state='uncertain',reason='执行租约到期，必须核对平台回执',updated=? WHERE state='claimed' AND lease_until<?", (now, now))
            jobs = [dict(x) for x in db.execute("SELECT * FROM goal_jobs WHERE state IN ('pending','waiting') ORDER BY created LIMIT 100")]
        for job in jobs:
            try:
                self._job(job)
            except Problem as exc:
                with self.store.db(True) as db:
                    db.execute("UPDATE goal_jobs SET state='needs_human',error=? WHERE id=?", (str(exc), job['id']))
                    db.execute("UPDATE goal_cases SET state='needs_human' WHERE id=?", (job['case_id'],))
        self._timers(now)

    def _job(self, job):
        with self.store.db() as db:
            case = db.execute('SELECT * FROM goal_cases WHERE id=?', (job['case_id'],)).fetchone()
            goal = db.execute('SELECT * FROM goals WHERE id=?', (job['goal_id'],)).fetchone()
            event = json.loads(db.execute('SELECT payload FROM business_events WHERE id=?', (job['event_id'],)).fetchone()[0])
            source_enabled = db.execute('SELECT enabled FROM sources WHERE id=?', (event['source_id'],)).fetchone()[0]
        if not source_enabled:
            return
        with self.store.db() as db:
            if not self.authorized(db, job['goal_id']):
                return
        config = json.loads(job['config'])
        current = self._goal(goal)
        invalid = not current['enabled'] or current['version'] != config['version'] or case['state'] in ('closed', 'stopped') or case['latest_event'] != job['event_id']
        if invalid:
            with self.store.db(True) as db:
                db.execute("UPDATE goal_jobs SET state='superseded',error='目标、事项或最新事件已变化' WHERE id=? AND state IN ('pending','waiting')", (job['id'],))
            return
        if job['state'] == 'pending':
            with self.store.db(True) as db:
                db.execute("UPDATE business_events SET state='accepted' WHERE id=? AND state='waiting_authority'", (job['event_id'],))
            with self.store.db() as db:
                history = [json.loads(x[0]) for x in db.execute('SELECT payload FROM business_events WHERE source=? AND account=? AND json_extract(payload,\'$.target\')=? ORDER BY seq DESC LIMIT 20', (event['source_id'], event['account_id'], event['target']))]
                sent = [dict(x) for x in db.execute("SELECT payload,receipt FROM account_actions WHERE source=? AND account=? AND target=? AND state='succeeded' ORDER BY seq DESC LIMIT 5", (event['source_id'], event['account_id'], event['target']))]
            context = dict(event['context'])
            context.update(goal_id=config['id'], business_key=case['id'],
                goal={k: config[k] for k in ('objective', 'persona', 'knowledge', 'allowed_actions', 'max_followups')},
                case={'id': case['id'], 'summary': case['summary'], 'turns': case['turns']},
                event={k: event[k] for k in ('event_type', 'surface', 'direction', 'resource', 'transport')},
                messages=[{'event_id': x['event_id'], 'direction': x['direction'], 'text': x['content']['text']} for x in reversed(history)],
                sent_actions=[{'text': json.loads(x['payload'])['text'], 'platform_receipt': json.loads(x['receipt'])['external_id']} for x in reversed(sent)])
            with self.store.db() as db:
                authority = db.execute('SELECT context FROM control_goals WHERE goal_id=?', (config['id'],)).fetchone()
            if authority:
                context['command'] = json.loads(authority['context'])
            try:
                context = compact_context(context)
            except ChannelError as exc:
                raise Problem(str(exc)) from None
            message, _ = self.store.ingest(event['source_id'], dict(account_id=event['account_id'],
                message_id='goal-' + job['id'], category='goal_decision', surface=event['surface'], event_type=event['event_type'],
                conversation_id=case['id'], content=event['content'], context=context))
            if not message['route'] or message['route']['id'] != config['route_id'] or message['route']['version'] != config['version'] or message['route']['agent_id'] != config['agent_id']:
                if message['state'] in ('queued', 'blocked_no_route'):
                    self.store.cancel(message['id'])
                raise Problem('目标专属路由被其他规则覆盖，请检查分流配置')
            with self.store.db(True) as db:
                db.execute("UPDATE goal_jobs SET channel_event=?,state='waiting' WHERE id=? AND state='pending'", (message['id'], job['id']))
            return
        message = self.store.message(job['channel_event'])
        if message['state'] in ('failed', 'failed_preflight', 'cancelled', 'uncertain'):
            raise Problem('模型请求未完成，请检查关联 Channel 消息')
        if message['state'] != 'succeeded':
            return
        decision = message['result']['output'].get('structured')
        kind, payload, summary, delay = self._validate_action(decision, config, event)
        now = time.time()
        with self.store.db(True) as db:
            fresh = db.execute('SELECT * FROM goal_cases WHERE id=?', (case['id'],)).fetchone()
            fresh_goal = self._goal(db.execute('SELECT * FROM goals WHERE id=?', (config['id'],)).fetchone())
            if fresh['latest_event'] != job['event_id'] or fresh['state'] in ('closed', 'stopped') or not fresh_goal['enabled'] or fresh_goal['version'] != config['version']:
                db.execute("UPDATE goal_jobs SET state='superseded' WHERE id=?", (job['id'],))
                return
            if not db.execute("SELECT 1 FROM goal_jobs WHERE id=? AND state='waiting'", (job['id'],)).fetchone():
                return
            human = decision.get('requires_human') is not False or bool(event['context'].get('media_unavailable'))
            state = 'needs_human' if kind == 'handoff' else 'waiting_input' if kind == 'none' else 'awaiting_execution'
            if kind in ACTIONS:
                action_state = 'review' if config['mode'] == 'review' or human else 'ready'
                db.execute('INSERT INTO account_actions(id,job_id,goal_id,case_id,source,account,target,type,payload,state,created,updated,available,followup_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (uid(), job['id'], config['id'], case['id'], event['source_id'], event['account_id'], event['target'], kind, dumps(payload), action_state, now, now, now, delay))
                self.store.audit(db, 'account_action.proposed', job['id'], action_state)
            db.execute('UPDATE goal_cases SET state=?,summary=?,updated=? WHERE id=?', (state, summary, now, case['id']))
            db.execute("UPDATE goal_jobs SET state='decided' WHERE id=?", (job['id'],))

    def decide_action(self, action_id, approve):
        with self.store.db(True) as db:
            action = db.execute('SELECT * FROM account_actions WHERE id=?', (action_id,)).fetchone()
            if not action or action['state'] != 'review':
                raise Problem('仅待审核动作可以采用或拒绝', 409)
            state = 'ready' if approve else 'cancelled'
            db.execute('UPDATE account_actions SET state=?,reason=?,updated=? WHERE id=?', (state, '人工采用' if approve else '人工拒绝', time.time(), action_id))
            self.store.audit(db, 'account_action.' + ('approved' if approve else 'rejected'), action_id)
        return {'ok': True}

    def _guard(self, db, action, now):
        goal = self._goal(db.execute('SELECT * FROM goals WHERE id=?', (action['goal_id'],)).fetchone())
        from .command_actions import guard
        command_guard = guard(self, db, action, now)
        if command_guard:
            return command_guard
        if not self.authorized(db, action['goal_id'], now):
            return 'ready', '等待指挥部续约或暂停指令'
        case = db.execute('SELECT * FROM goal_cases WHERE id=?', (action['case_id'],)).fetchone()
        job = db.execute('SELECT * FROM goal_jobs WHERE id=?', (action['job_id'],)).fetchone()
        if not goal['enabled'] or goal['version'] != json.loads(job['config'])['version'] or case['state'] in ('closed', 'stopped') or case['latest_event'] != job['event_id']:
            return 'cancelled', '目标已暂停、事项结束或已有更新消息'
        if db.execute('SELECT 1 FROM contact_blocks WHERE source=? AND account=? AND target=?', (action['source'], action['account'], action['target'])).fetchone():
            return 'cancelled', '对方要求停止联系'
        if case['turns'] >= goal['case_limit']:
            return 'cancelled', '已达到事项沟通上限'
        clock = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
        if not goal['hour_start'] <= clock.hour < goal['hour_end']:
            return 'ready', '等待工作时间'
        # The strictest enabled goal governs the shared execution account budget.
        peers = [self._goal(x) for x in db.execute('SELECT * FROM goals')]
        peers = [x for x in peers if x['enabled'] and x['source_id'] == action['source'] and x['account_id'] == action['account']]
        limit = min(x['daily_limit'] for x in peers)
        interval = max(x['min_interval'] for x in peers)
        midnight = clock.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        count = db.execute("SELECT count(*) FROM account_actions WHERE source=? AND account=? AND claim_hash IS NOT NULL AND lease_until>=?", (action['source'], action['account'], midnight + 90)).fetchone()[0]
        last = db.execute('SELECT max(created_at) FROM (SELECT lease_until-90 AS created_at FROM account_actions WHERE source=? AND account=? AND claim_hash IS NOT NULL)', (action['source'], action['account'])).fetchone()[0]
        if count >= limit:
            return 'ready', '等待账号每日额度'
        if last is not None and now - last < interval:
            return 'ready', '等待账号动作间隔'
        if db.execute("SELECT 1 FROM account_actions WHERE source=? AND account=? AND state IN ('claimed','uncertain')", (action['source'], action['account'])).fetchone():
            return 'ready', '账号有在途或待核对动作'
        return '', ''

    def claim(self, source, data):
        account = identifier(data.get('account_id'), 'account_id')
        executor = identifier(data.get('executor_id'), 'executor_id', 80)
        now = time.time()
        with self.store.db(True) as db:
            connector = db.execute('SELECT * FROM account_connectors WHERE source=? AND account=? AND executor=? AND last_seen>?', (source, account, executor, now - 120)).fetchone()
            if not connector:
                raise Problem('请先为当前账号登记在线执行器', 409)
            for action in db.execute("SELECT * FROM account_actions WHERE source=? AND account=? AND state='ready' AND available<=? ORDER BY seq LIMIT 100", (source, account, now)).fetchall():
                state, reason = self._guard(db, action, now)
                if not state and action['type'] not in json.loads(connector['capabilities']):
                    state, reason = 'ready', '执行器未声明此动作能力'
                if state:
                    db.execute('UPDATE account_actions SET state=?,reason=? WHERE id=?', (state, reason, action['id']))
                    continue
                claim_token = secrets.token_urlsafe(32)
                db.execute("UPDATE account_actions SET state='claimed',claim_hash=?,executor=?,lease_until=?,updated=?,reason='' WHERE id=?", (hashlib.sha256(claim_token.encode()).hexdigest(), executor, now + 90, now, action['id']))
                self.store.audit(db, 'account_action.claimed', action['id'], executor)
                result = self._action(db.execute('SELECT * FROM account_actions WHERE id=?', (action['id'],)).fetchone())
                result['claim_token'] = claim_token
                return {'action': result}
        return {'action': None}

    def receipt(self, source, action_id, data):
        account = identifier(data.get('account_id'), 'account_id')
        token = identifier(data.get('claim_token'), 'claim_token')
        state = data.get('status')
        if state not in ('succeeded', 'failed', 'uncertain'):
            raise Problem('回执状态无效')
        external_id = identifier(data.get('external_id'), 'external_id', optional=True)
        if state == 'succeeded' and not external_id:
            raise Problem('成功回执必须携带平台消息或作品标识')
        receipt = dict(status=state, external_id=external_id, detail=text(data.get('detail', ''), '回执说明', 500))
        now = time.time()
        with self.store.db(True) as db:
            action = db.execute('SELECT * FROM account_actions WHERE id=? AND source=? AND account=?', (action_id, source, account)).fetchone()
            if not action:
                raise LookupError()
            if not secrets.compare_digest(action['claim_hash'] or '', hashlib.sha256(token.encode()).hexdigest()):
                raise Problem('动作领取凭据不匹配', 403)
            if action['state'] in ('succeeded', 'failed'):
                if action['receipt'] == dumps(receipt):
                    return {'ok': True, 'duplicate': True}
                raise Problem('终态回执不能覆盖', 409)
            if action['state'] not in ('claimed', 'uncertain'):
                raise Problem('动作尚未领取', 409)
            db.execute('UPDATE account_actions SET state=?,receipt=?,reason=?,updated=? WHERE id=?', (state, dumps(receipt), receipt['detail'], now, action_id))
            if state == 'succeeded':
                db.execute("UPDATE goal_cases SET turns=turns+1,state=CASE WHEN state IN ('closed','stopped') THEN state ELSE 'waiting_input' END,updated=? WHERE id=?", (now, action['case_id']))
                case = db.execute('SELECT * FROM goal_cases WHERE id=?', (action['case_id'],)).fetchone()
                goal = self._goal(db.execute('SELECT * FROM goals WHERE id=?', (action['goal_id'],)).fetchone())
                job = db.execute('SELECT * FROM goal_jobs WHERE id=?', (action['job_id'],)).fetchone()
                n = db.execute("SELECT count(*) FROM followup_timers WHERE case_id=? AND state IN ('fired','waiting')", (action['case_id'],)).fetchone()[0]
                if action['followup_seconds'] and goal['enabled'] and goal['version'] == json.loads(job['config'])['version'] and case['state'] not in ('closed', 'stopped') and case['latest_event'] == job['event_id'] and n < goal['max_followups']:
                    db.execute('INSERT OR IGNORE INTO followup_timers VALUES(?,?,?,?,?,?,?)', (uid(), action['case_id'], job['event_id'], action_id, now + action['followup_seconds'], 'waiting', now))
            else:
                db.execute("UPDATE goal_cases SET state=CASE WHEN state IN ('closed','stopped') THEN state ELSE 'needs_human' END,updated=? WHERE id=?", (now, action['case_id']))
            self.store.audit(db, 'account_action.receipt', action_id, state)
        return {'ok': True, 'duplicate': False}

    def _timers(self, now):
        with self.store.db() as db:
            timers = [dict(x) for x in db.execute("SELECT * FROM followup_timers WHERE state='waiting' AND due<=? ORDER BY due LIMIT 50", (now,))]
        for timer in timers:
            with self.store.db() as db:
                case = db.execute('SELECT * FROM goal_cases WHERE id=?', (timer['case_id'],)).fetchone()
                goal = self._goal(db.execute('SELECT * FROM goals WHERE id=?', (case['goal_id'],)).fetchone())
                event = json.loads(db.execute('SELECT payload FROM business_events WHERE id=?', (timer['event_id'],)).fetchone()[0])
                already_fired = db.execute('SELECT 1 FROM business_events WHERE source=? AND account=? AND external_id=?', (event['source_id'], event['account_id'], 'followup-' + timer['id'])).fetchone()
                source_enabled = db.execute('SELECT enabled FROM sources WHERE id=?', (event['source_id'],)).fetchone()[0]
            with self.store.db() as db:
                authority_active = self.authorized(db, goal['id'], now)
            if not source_enabled or not authority_active:
                continue
            valid = goal['enabled'] and case['state'] not in ('closed', 'stopped') and case['latest_event'] == timer['event_id']
            if valid and not already_fired:
                # Stable IDs close the crash window between ingestion and timer update.
                data = dict(event, event_id='followup-' + timer['id'], event_type='dm.followup_due', transport='timer', direction='system',
                            content={'text': '等待对方回复的期限已到。结合原沟通事项决定是否需要跟进；无需跟进时返回 none。', 'images': []})
                fired, _ = self.ingest(event['source_id'], data, internal=True, only_goal=goal['id'], expected_latest=timer['event_id'])
                valid = fired is not None
            with self.store.db(True) as db:
                db.execute('UPDATE followup_timers SET state=? WHERE id=? AND state=\'waiting\'', ('fired' if valid or already_fired else 'cancelled', timer['id']))


def register_automation(app, automation):
    from flask import Blueprint, g, request

    bp = Blueprint('automation', __name__)

    @bp.get('/automation')
    def automation_page():
        return app.send_static_file('automation.html')

    @bp.get('/api/automation')
    def overview():
        return dict(goals=automation.goals(), events=automation.events(), cases=automation.cases(), actions=automation.actions(), connectors=automation.connectors())

    @bp.post('/api/goals')
    def create_goal():
        return automation.save_goal(request.json)

    @bp.put('/api/goals/<goal_id>')
    def update_goal(goal_id):
        return automation.save_goal(request.json, goal_id)

    @bp.post('/api/business-events')
    def submit_event():
        value, duplicate = automation.ingest(request.json.get('source_id'), request.json)
        return dict(value, duplicate=duplicate), 200 if duplicate else 202

    @bp.post('/api/goal-cases/<case_id>/close')
    def close_case(case_id):
        return automation.close_case(case_id)

    @bp.post('/api/actions/<action_id>/approve')
    def approve(action_id):
        return automation.decide_action(action_id, True)

    @bp.post('/api/actions/<action_id>/reject')
    def reject(action_id):
        return automation.decide_action(action_id, False)

    @bp.post('/v1/events')
    def inbound():
        value, duplicate = automation.ingest(g.source, request.json)
        return dict(value, duplicate=duplicate), 200 if duplicate else 202

    @bp.post('/v1/connectors/heartbeat')
    def heartbeat():
        return automation.heartbeat(g.source, request.json)

    @bp.post('/v1/actions/claim')
    def claim():
        return automation.claim(g.source, request.json)

    @bp.post('/v1/actions/<action_id>/receipt')
    def receipt(action_id):
        return automation.receipt(g.source, action_id, request.json)

    app.register_blueprint(bp)
