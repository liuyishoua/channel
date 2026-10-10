import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid
import warnings
from PIL import Image
from .domain import Problem, digest, dumps, group_key, identifier, match_route, normalize_message, normalize_route

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,name TEXT NOT NULL,key_hash TEXT UNIQUE NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS routes(id TEXT PRIMARY KEY,version INTEGER NOT NULL,config TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,message_id TEXT NOT NULL,digest TEXT NOT NULL,payload TEXT NOT NULL,route TEXT,group_key TEXT NOT NULL,thread_id TEXT,batch_id TEXT,state TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL,available REAL NOT NULL,queue_order INTEGER NOT NULL DEFAULT 0,UNIQUE(source,account,message_id));
CREATE INDEX IF NOT EXISTS message_queue ON messages(state,available,seq);
CREATE INDEX IF NOT EXISTS message_group ON messages(group_key,state,seq);
CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,group_key TEXT NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,route_id TEXT NOT NULL,route_version INTEGER NOT NULL,agent_id TEXT NOT NULL,issue_id TEXT,status TEXT NOT NULL DEFAULT 'open',reason TEXT NOT NULL,created REAL NOT NULL,last_at REAL NOT NULL,message_count INTEGER NOT NULL DEFAULT 0,context_bytes INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS thread_group ON threads(group_key,status,last_at);
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,group_key TEXT NOT NULL,thread_id TEXT NOT NULL,route TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL DEFAULT 'prepare',issue_id TEXT,comment_id TEXT,task_id TEXT,error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL,next_poll REAL NOT NULL,deadline REAL NOT NULL,lease_until REAL NOT NULL DEFAULT 0,write_started INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS batch_work ON batches(status,next_poll,lease_until);
CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,source TEXT NOT NULL,account TEXT NOT NULL,path TEXT NOT NULL,media_type TEXT NOT NULL,bytes INTEGER NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS batch_assets(batch_id TEXT NOT NULL,asset_id TEXT NOT NULL,attachment_id TEXT NOT NULL,PRIMARY KEY(batch_id,asset_id));
CREATE TABLE IF NOT EXISTS results(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,event_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,acknowledged INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS result_source ON results(source,account,seq);
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,entity_id TEXT NOT NULL,detail TEXT NOT NULL,created REAL NOT NULL);
'''
ACTIVE = ('dispatching', 'waiting', 'reconciling', 'attention', 'uncertain')


def new_id():
    return str(uuid.uuid4())


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / 'channel.sqlite3'
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            if 'context_bytes' not in {x[1] for x in db.execute('PRAGMA table_info(threads)')}:
                db.execute('ALTER TABLE threads ADD COLUMN context_bytes INTEGER NOT NULL DEFAULT 0')
            if 'queue_order' not in {x[1] for x in db.execute('PRAGMA table_info(messages)')}:
                db.execute('ALTER TABLE messages ADD COLUMN queue_order INTEGER NOT NULL DEFAULT 0')
                db.execute('UPDATE messages SET queue_order=seq')
        os.chmod(self.path, 0o600)
        from .sessions import Sessions
        self.sessions = Sessions(self)

    @contextlib.contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def audit(self, db, action, entity, detail=''):
        db.execute('INSERT INTO audit(action,entity_id,detail,created) VALUES(?,?,?,?)', (action, entity, detail, time.time()))

    def source(self, source_id, name=None):
        if not isinstance(source_id, str) or not re.fullmatch(r'[a-z0-9_-]{1,64}', source_id):
            raise Problem('来源标识只允许小写字母、数字、短横线和下划线')
        token = 'chn_' + secrets.token_urlsafe(32)
        now = time.time()
        with self.db(True) as db:
            if db.execute('SELECT 1 FROM sources WHERE id=?', (source_id,)).fetchone():
                raise Problem('来源已存在', 409)
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?)', (source_id, identifier(name or source_id, '来源名称', 80), hashlib.sha256(token.encode()).hexdigest(), 1, now))
            self.audit(db, 'source.created', source_id)
        return {'id': source_id, 'token': token}

    def authenticate(self, token):
        if not isinstance(token, str) or not token.startswith('chn_') or len(token) > 160:
            raise Problem('来源凭据无效', 401)
        with self.db() as db:
            row = db.execute('SELECT id FROM sources WHERE key_hash=? AND enabled=1', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row:
            raise Problem('来源凭据无效或已停用', 401)
        return row['id']

    def sources(self):
        with self.db() as db:
            return [dict(x) for x in db.execute('SELECT id,name,enabled,created FROM sources ORDER BY created')]

    def change_source(self, source_id, data):
        with self.db(True) as db:
            if not db.execute('SELECT 1 FROM sources WHERE id=?', (source_id,)).fetchone():
                raise LookupError()
            if 'enabled' in data:
                if not isinstance(data['enabled'], bool):
                    raise Problem('enabled 必须是布尔值')
                db.execute('UPDATE sources SET enabled=? WHERE id=?', (int(data['enabled']), source_id))
            token = None
            if data.get('rotate') is True:
                token = 'chn_' + secrets.token_urlsafe(32)
                db.execute('UPDATE sources SET key_hash=? WHERE id=?', (hashlib.sha256(token.encode()).hexdigest(), source_id))
            self.audit(db, 'source.updated', source_id, 'rotated' if token else 'enabled changed')
        return {'ok': True, **({'token': token} if token else {})}

    def routes(self):
        with self.db() as db:
            return [json.loads(x['config']) for x in db.execute('SELECT config FROM routes ORDER BY updated')]

    def save_route(self, data, route_id=None):
        with self.db(True) as db:
            prior = db.execute('SELECT version FROM routes WHERE id=?', (route_id,)).fetchone() if route_id else None
            if route_id and not prior:
                raise LookupError()
            if prior:
                owned = json.loads(db.execute('SELECT config FROM routes WHERE id=?', (route_id,)).fetchone()[0])
                if owned.get('goal_ids', ['*']) != ['*']:
                    raise Problem('请在账号目标页修改目标专属规则', 409)
            route = normalize_route(data, route_id, prior['version'] + 1 if prior else 1)
            db.execute('INSERT INTO routes VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,config=excluded.config,updated=excluded.updated',
                       (route['id'], route['version'], dumps(route), time.time()))
            self.audit(db, 'route.saved', route['id'], 'version ' + str(route['version']))
        return route

    def _route_group(self, db, payload, route):
        key = group_key(payload, route)
        thread = None
        if payload['reply_to']:
            previous = db.execute('SELECT * FROM messages WHERE source=? AND account=? AND message_id=?',
                                  (payload['source_id'], payload['account_id'], payload['reply_to'])).fetchone()
            if not previous:
                return key, None, 'blocked_reply', '等待被回复消息到达'
            if previous['state'] in ('blocked_reply', 'blocked_no_route'):
                return key, None, 'blocked_reply', '等待被回复消息完成分流'
            previous_route = json.loads(previous['route']) if previous['route'] else None
            if not previous_route or previous_route['id'] != route['id'] or previous_route['version'] != route['version'] or json.loads(previous['payload'])['category'] != payload['category']:
                raise Problem('被回复消息的业务类别或路由版本不同，请新建任务', 409)
            if payload['conversation_id'] != json.loads(previous['payload'])['conversation_id']:
                raise Problem('不能跨会话追加回复', 409)
            if payload['context'].get('business_key', '') != json.loads(previous['payload'])['context'].get('business_key', ''):
                raise Problem('不能跨业务事项追加回复，请新建任务', 409)
            key, thread = previous['group_key'], previous['thread_id']
        if payload['thread_id']:
            row = db.execute('SELECT * FROM threads WHERE id=?', (payload['thread_id'],)).fetchone()
            if not row or row['source'] != payload['source_id'] or row['account'] != payload['account_id']:
                raise Problem('任务会话不存在或不属于当前账号', 404)
            if row['group_key'] != key or row['status'] != 'open':
                raise Problem('任务会话已关闭，或业务范围、路由版本不一致', 409)
            thread = row['id']
        return key, thread, 'queued', ''

    def ingest(self, source, data):
        payload = normalize_message(data, source)
        fingerprint = digest(payload)
        now = time.time()
        with self.db(True) as db:
            existing = db.execute('SELECT * FROM messages WHERE source=? AND account=? AND message_id=?', (source, payload['account_id'], payload['message_id'])).fetchone()
            if existing:
                if existing['digest'] != fingerprint:
                    raise Problem('同一 message_id 的内容发生变化，请使用新的消息 ID', 409)
                return self.project_message(existing), True
            if not db.execute('SELECT 1 FROM sources WHERE id=? AND enabled=1', (source,)).fetchone():
                raise Problem('来源已停用', 403)
            depth = db.execute("SELECT count(*) FROM messages WHERE state IN ('queued','processing','waiting','blocked_reply','blocked_no_route','attention','uncertain')").fetchone()[0]
            source_depth = db.execute("SELECT count(*) FROM messages WHERE source=? AND state IN ('queued','processing','waiting','blocked_reply','blocked_no_route','attention','uncertain')", (source,)).fetchone()[0]
            if depth >= 20000 or source_depth >= 5000:
                raise Problem('队列已满，请稍后使用原消息 ID 重试', 429)
            for asset_id in payload['content']['images']:
                if not db.execute('SELECT 1 FROM assets WHERE id=? AND source=? AND account=?', (asset_id, source, payload['account_id'])).fetchone():
                    raise Problem('图片不存在或不属于当前来源账号', 403)
            snapshot_id = payload.get('session_snapshot_id')
            if snapshot_id:
                snapshot_row, snapshot = self.sessions.resolve(db, snapshot_id, source, payload['account_id'])
                if (snapshot_row['surface'], snapshot_row['conversation']) != (payload.get('surface', 'manual'), payload['conversation_id']):
                    raise Problem('快照与请求会话不匹配', 409)
                if not snapshot['eligible'] or snapshot_row['revision'] != snapshot_row['current_revision'] or snapshot_row['memory_revision'] != snapshot_row['current_memory_revision']:
                    raise Problem('快照已失效，请为最新消息创建新快照', 409)
                if {'messages','history','current_message','previous_analysis','memories','session','trigger_message_ids'} & payload['context'].keys():
                    raise Problem('会话快照字段由 Channel 构造，不能覆盖')
                if payload['new_task'] or payload['reply_to'] or payload['thread_id']:
                    raise Problem('会话快照使用独立执行，不接受任务续接参数')
            route = match_route(payload, [json.loads(x[0]) for x in db.execute('SELECT config FROM routes')])
            if payload.get('route_id') and not route:
                raise Problem('所选路由不可用或不匹配当前来源、类别及入口', 409)
            event_id = new_id()
            if snapshot_id:
                turn_key = digest([snapshot['session_id'], snapshot['revision'], snapshot['memory_revision'], sorted(snapshot['trigger_message_ids']), payload.get('route_id'), payload['category'], payload['context'].get('control_mode'), snapshot_id if snapshot.get('reanalysis') else ''])
                prior = db.execute('SELECT event_id FROM session_dispatches WHERE snapshot_id=? OR turn_key=?', (snapshot_id, turn_key)).fetchone()
                if prior:
                    raise Problem('此轮快照已有请求，请查询原事件，勿重复提交', 409)
                db.execute('INSERT INTO session_dispatches VALUES(?,?,?)', (snapshot_id, event_id, turn_key))
            if route:
                if len(dumps(payload).encode()) > route.get('max_context_bytes', 262144):
                    raise Problem('单条消息上下文超过此规则允许的任务上限')
                key, thread, state, error = self._route_group(db, payload, route)
            else:
                key, thread, state, error = event_id, None, 'blocked_no_route', '没有匹配路由，消息已保留'
            db.execute('INSERT INTO messages(id,source,account,message_id,digest,payload,route,group_key,thread_id,state,error,created,updated,available) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (event_id, source, payload['account_id'], payload['message_id'], fingerprint, dumps(payload), dumps(route) if route else None, key, thread, state, error, now, now, now + (route['batch_seconds'] if route else 0)))
            db.execute('UPDATE messages SET queue_order=seq WHERE id=?', (event_id,))
            self.audit(db, 'message.accepted', event_id, state)
            row = db.execute('SELECT * FROM messages WHERE id=?', (event_id,)).fetchone()
        return self.project_message(row), False

    def project_message(self, row):
        value = dict(row)
        value['payload'] = json.loads(value['payload'])
        value['route'] = json.loads(value['route']) if value['route'] else None
        value.pop('digest', None)
        with self.db() as db:
            result = db.execute('SELECT payload,seq,acknowledged FROM results WHERE event_id=?', (row['id'],)).fetchone()
            batch = db.execute('SELECT issue_id,comment_id,task_id,status,stage FROM batches WHERE id=?', (row['batch_id'],)).fetchone() if row['batch_id'] else None
        value['result'] = json.loads(result['payload']) if result else None
        value['result_cursor'] = result['seq'] if result else None
        value['execution'] = dict(batch) if batch else None
        return value

    def message(self, event_id, source=None, account=None):
        with self.db() as db:
            row = db.execute('SELECT * FROM messages WHERE id=?', (event_id,)).fetchone()
        if not row or (source and row['source'] != source) or (account and row['account'] != account):
            raise LookupError()
        return self.project_message(row)

    def messages(self, state='', source='', limit=100):
        with self.db() as db:
            rows = db.execute('SELECT * FROM messages WHERE (?="" OR state=?) AND (?="" OR source=?) ORDER BY seq DESC LIMIT ?', (state, state, source, source, limit)).fetchall()
        return [self.project_message(x) for x in rows]

    def put_asset(self, source, account, blob):
        account = identifier(account, 'account_id')
        if not blob or len(blob) > 8 * 1024 * 1024:
            raise Problem('图片大小需在 1 字节至 8MB 之间')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(blob)) as image:
                    if image.format not in ('PNG', 'JPEG', 'WEBP', 'GIF') or image.width * image.height > 20000000:
                        raise Problem('只支持 2000 万像素以内的 PNG、JPEG、WebP、GIF 图片')
                    form = image.format
                    image.verify()
        except Problem:
            raise
        except Exception:
            raise Problem('图片文件无法识别或已损坏') from None
        asset_id = hashlib.sha256((source + '\0' + account + '\0').encode() + blob).hexdigest()
        with self.db() as db:
            existing = db.execute('SELECT id,media_type,bytes FROM assets WHERE id=? AND source=? AND account=?', (asset_id,source,account)).fetchone()
        if existing:
            return dict(existing)
        folder = self.directory / 'assets'
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / asset_id
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, 'wb') as f:
                f.write(blob)
        media_type = Image.MIME[form]
        with self.db(True) as db:
            db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?,?)', (asset_id, source, account, str(path), media_type, len(blob), time.time()))
        return {'id': asset_id, 'media_type': media_type, 'bytes': len(blob)}

    def asset(self, asset_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
        if not row:
            raise LookupError()
        return dict(row)

    def thread(self, thread_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM threads WHERE id=?', (thread_id,)).fetchone()
        if not row:
            raise LookupError()
        return dict(row)

    def threads(self):
        with self.db() as db:
            return [dict(x) for x in db.execute('SELECT * FROM threads ORDER BY last_at DESC LIMIT 100')]

    def close_thread(self, thread_id):
        with self.db(True) as db:
            row = db.execute('SELECT * FROM threads WHERE id=?', (thread_id,)).fetchone()
            if not row:
                raise LookupError()
            active = db.execute("SELECT 1 FROM batches WHERE thread_id=? AND status IN ('dispatching','waiting','reconciling','attention','uncertain')", (thread_id,)).fetchone()
            if active:
                raise Problem('任务仍有运行中或待确认批次，请先处理这些批次', 409)
            db.execute("UPDATE threads SET status='closed',reason='人工结束' WHERE id=?", (thread_id,))
            self.audit(db, 'thread.closed', thread_id)
        return {'ok': True}

    def cancel(self, event_id):
        now = time.time()
        with self.db(True) as db:
            row = db.execute('SELECT * FROM messages WHERE id=?', (event_id,)).fetchone()
            if not row:
                raise LookupError()
            if row['state'] not in ('queued','blocked_no_route','blocked_reply'):
                raise Problem('只能取消尚未投递的消息；已投递任务请在 Multica 中处理',409)
            error = '请求已在投递前取消'
            db.execute("UPDATE messages SET state='cancelled',error=?,updated=? WHERE id=?", (error,now,event_id))
            result = dict(event_id=event_id,message_id=row['message_id'],source_id=row['source'],account_id=row['account'],
                          thread_id=row['thread_id'],batch_id=None,issue_id=None,task_id=None,state='cancelled',output=None,error=error,created=now)
            db.execute('INSERT INTO results(id,source,account,event_id,payload,created) VALUES(?,?,?,?,?,?)',
                       (new_id(),row['source'],row['account'],event_id,dumps(result),now))
            self.audit(db,'message.cancelled',event_id)
        return self.message(event_id)

    def retry(self, event_id):
        with self.db(True) as db:
            row = db.execute('SELECT * FROM messages WHERE id=?', (event_id,)).fetchone()
            if not row:
                raise LookupError()
            if row['state'] not in ('blocked_no_route', 'blocked_reply', 'failed_preflight'):
                raise Problem('只有未投递的阻塞消息或前置检查失败可以重新排队', 409)
            payload = json.loads(row['payload'])
            route = match_route(payload, [json.loads(x[0]) for x in db.execute('SELECT config FROM routes')])
            if not route:
                raise Problem('仍然没有匹配路由')
            key, thread, state, error = self._route_group(db, payload, route)
            db.execute('UPDATE messages SET route=?,group_key=?,thread_id=?,batch_id=NULL,state=?,error=?,available=?,updated=? WHERE id=?',
                       (dumps(route), key, thread, state, error, time.time() + route['batch_seconds'], time.time(), event_id))
            self.audit(db, 'message.requeued', event_id)
        return self.message(event_id)

    def unblock_replies(self):
        with self.db(True) as db:
            rows = db.execute("""SELECT m.* FROM messages m JOIN messages p ON p.source=m.source AND p.account=m.account
                AND p.message_id=json_extract(m.payload,'$.reply_to') WHERE m.state='blocked_reply'
                AND p.state NOT IN ('blocked_reply','blocked_no_route')
                AND json_extract(m.route,'$.id')=json_extract(p.route,'$.id')
                AND json_extract(m.route,'$.version')=json_extract(p.route,'$.version')
                ORDER BY m.seq LIMIT 100""").fetchall()
            for row in rows:
                try:
                    payload, route = json.loads(row['payload']), json.loads(row['route'])
                    key, thread, state, error = self._route_group(db, payload, route)
                    if state == 'queued':
                        ordinal = db.execute('SELECT coalesce(max(queue_order),0)+1 FROM messages').fetchone()[0]
                        db.execute('UPDATE messages SET group_key=?,thread_id=?,state=?,error=?,queue_order=?,updated=? WHERE id=?',
                                   (key, thread, state, error, ordinal, time.time(), row['id']))
                        self.audit(db, 'message.reply_unblocked', row['id'])
                except (Problem, LookupError):
                    pass

    def claim(self, lease_seconds=90):
        now = time.time()
        with self.db(True) as db:
            existing = db.execute("SELECT * FROM batches WHERE status IN ('dispatching','waiting','reconciling','attention') AND next_poll<=? AND lease_until<? ORDER BY next_poll LIMIT 1", (now, now)).fetchone()
            if existing:
                db.execute('UPDATE batches SET lease_until=? WHERE id=?', (now + lease_seconds, existing['id']))
                return dict(existing)
            active = db.execute("SELECT count(*) FROM batches WHERE status IN ('dispatching','waiting','reconciling','attention','uncertain')").fetchone()[0]
            if active >= 8:
                return None
            candidates = db.execute("""SELECT m.* FROM messages m JOIN sources s ON s.id=m.source AND s.enabled=1
                WHERE m.state='queued' AND m.available<=? AND NOT EXISTS(SELECT 1 FROM batches b WHERE b.group_key=m.group_key AND b.status IN ('dispatching','waiting','reconciling','attention','uncertain'))
                AND NOT EXISTS(SELECT 1 FROM messages prior WHERE prior.group_key=m.group_key AND prior.state='queued' AND prior.queue_order<m.queue_order)
                ORDER BY m.queue_order LIMIT 200""", (now,)).fetchall()
            if not candidates:
                return None
            row = max(candidates, key=lambda x: json.loads(x['route'])['priority'] + min((now - x['created']) / 60, 100))
            route = json.loads(row['route'])
            batch_id = new_id()
            events = db.execute("SELECT * FROM messages WHERE group_key=? AND state='queued' ORDER BY queue_order LIMIT ?", (row['group_key'], min(route['max_batch'], route['max_messages']))).fetchall()
            independent = bool(json.loads(row['payload']).get('session_snapshot_id'))
            if independent:
                events = events[:1]
            bounded = []
            context_bytes = 0
            for event in events:
                size = len(event['payload'].encode())
                if bounded and context_bytes + size > min(131072, route.get('max_context_bytes',262144)):
                    break
                bounded.append(event)
                context_bytes += size
            events = bounded
            preferred = row['thread_id']
            thread = db.execute("SELECT * FROM threads WHERE id=? AND status='open'", (preferred,)).fetchone() if preferred else db.execute("SELECT * FROM threads WHERE group_key=? AND status='open' ORDER BY last_at DESC LIMIT 1", (row['group_key'],)).fetchone()
            if preferred and not thread:
                thread = db.execute("SELECT * FROM threads WHERE group_key=? AND status='open' ORDER BY last_at DESC LIMIT 1", (row['group_key'],)).fetchone()
            reason = '回复消息指定原任务' if preferred else '同一业务会话继续追加'
            if independent and thread:
                db.execute("UPDATE threads SET status='closed',reason='本轮独立上下文执行已结束' WHERE id=?", (thread['id'],))
                thread = None
                reason = '同一业务会话的新一轮独立上下文'
            if thread and (now - thread['last_at'] > route['idle_seconds'] or thread['message_count'] + len(events) > route['max_messages'] or thread['context_bytes'] + context_bytes > route.get('max_context_bytes',262144)):
                db.execute("UPDATE threads SET status='closed',reason='超出时间窗口、消息数或上下文上限' WHERE id=?", (thread['id'],))
                thread = None
                reason = '超出时间窗口、消息数或上下文上限，新建任务'
            if not thread:
                thread_id = new_id()
                if reason == '同一业务会话继续追加':
                    reason = '首条消息、缺少归并标识或主动新建任务'
                db.execute('INSERT INTO threads(id,group_key,source,account,route_id,route_version,agent_id,reason,created,last_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (thread_id, row['group_key'], row['source'], row['account'], route['id'], route['version'], route['agent_id'], reason, now, now))
            else:
                thread_id = thread['id']
            db.execute('INSERT INTO batches(id,group_key,thread_id,route,status,created,updated,next_poll,deadline,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (batch_id, row['group_key'], thread_id, dumps(route), 'dispatching', now, now, now, now + route['timeout_seconds'], now + lease_seconds))
            for event in events:
                db.execute("UPDATE messages SET state='processing',batch_id=?,thread_id=?,updated=? WHERE id=?", (batch_id, thread_id, now, event['id']))
            self.audit(db, 'batch.claimed', batch_id, str(len(events)) + ' messages; ' + reason)
            return dict(db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone())

    def batch(self, batch_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise LookupError()
        return dict(row)

    def batch_messages(self, batch_id):
        with self.db() as db:
            result = []
            for row in db.execute('SELECT payload FROM messages WHERE batch_id=? ORDER BY queue_order', (batch_id,)):
                payload = json.loads(row['payload'])
                if payload.get('session_snapshot_id'):
                    _, snapshot = self.sessions.resolve(db, payload['session_snapshot_id'], payload['source_id'], payload['account_id'])
                    payload['context'] = {**snapshot['context'], **payload['context']}
                result.append(payload)
            return result

    def update_batch(self, batch_id, **changes):
        allowed = {'status', 'stage', 'issue_id', 'comment_id', 'task_id', 'error', 'next_poll', 'deadline', 'lease_until', 'write_started', 'attempts', 'thread_id'}
        if not changes.keys() <= allowed:
            raise ValueError('Invalid batch fields')
        changes['updated'] = time.time()
        with self.db(True) as db:
            db.execute('UPDATE batches SET ' + ','.join(k + '=?' for k in changes) + ' WHERE id=?', (*changes.values(), batch_id))
            if changes.get('status') in ('waiting', 'attention', 'uncertain'):
                db.execute('UPDATE messages SET state=?,error=?,updated=? WHERE batch_id=?', (changes['status'], changes.get('error', ''), time.time(), batch_id))

    def remote_bound(self, batch_id, issue_id, comment_id=None):
        now = time.time()
        with self.db(True) as db:
            batch = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            db.execute("UPDATE batches SET issue_id=?,comment_id=?,status='waiting',stage='result',next_poll=?,lease_until=0,updated=? WHERE id=?", (issue_id, comment_id, now + 1, now, batch_id))
            count = db.execute('SELECT count(*) FROM messages WHERE batch_id=?', (batch_id,)).fetchone()[0]
            context_bytes = sum(len(x[0].encode()) for x in db.execute('SELECT payload FROM messages WHERE batch_id=?', (batch_id,)))
            db.execute('UPDATE threads SET issue_id=?,last_at=?,message_count=message_count+?,context_bytes=context_bytes+? WHERE id=?', (issue_id, now, count, context_bytes, batch['thread_id']))
            db.execute("UPDATE messages SET state='waiting',error='',updated=? WHERE batch_id=?", (now, batch_id))
            self.audit(db, 'batch.bound', batch_id, issue_id + ('/' + comment_id if comment_id else ''))

    def rotate_thread(self, batch_id, reason):
        now = time.time()
        with self.db(True) as db:
            batch = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            prior = db.execute('SELECT * FROM threads WHERE id=?', (batch['thread_id'],)).fetchone()
            db.execute("UPDATE threads SET status='closed',reason=? WHERE id=?", (reason, prior['id']))
            thread_id = new_id()
            db.execute('INSERT INTO threads(id,group_key,source,account,route_id,route_version,agent_id,reason,created,last_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (thread_id, prior['group_key'], prior['source'], prior['account'], prior['route_id'], prior['route_version'], prior['agent_id'], reason, now, now))
            db.execute('UPDATE batches SET thread_id=? WHERE id=?', (thread_id, batch_id))
            db.execute('UPDATE messages SET thread_id=? WHERE batch_id=?', (thread_id, batch_id))
        return self.thread(thread_id)

    def finish(self, batch_id, state, result=None, error=''):
        now = time.time()
        with self.db(True) as db:
            batch = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            db.execute('UPDATE batches SET status=?,error=?,lease_until=0,updated=? WHERE id=?', (state, error, now, batch_id))
            db.execute('UPDATE messages SET state=?,error=?,updated=? WHERE batch_id=?', (state, error, now, batch_id))
            events = db.execute('SELECT * FROM messages WHERE batch_id=?', (batch_id,)).fetchall()
            for event in events:
                if state == 'failed_preflight':
                    continue
                output = dict(event_id=event['id'], message_id=event['message_id'], source_id=event['source'], account_id=event['account'],
                              thread_id=batch['thread_id'], batch_id=batch_id, issue_id=batch['issue_id'], task_id=batch['task_id'],
                              state=state, output=result, error=error, created=now)
                db.execute('INSERT OR IGNORE INTO results(id,source,account,event_id,payload,created) VALUES(?,?,?,?,?,?)',
                           (new_id(), event['source'], event['account'], event['id'], dumps(output), now))
                snapshot_id = json.loads(event['payload']).get('session_snapshot_id')
                if state == 'succeeded' and snapshot_id and isinstance((result or {}).get('structured'), dict):
                    snap, _ = self.sessions.resolve(db, snapshot_id, event['source'], event['account'])
                    if snap['revision'] == snap['current_revision'] and snap['memory_revision'] == snap['current_memory_revision']:
                        structured = result['structured']
                        observation = {k: structured[k][:500] for k in ('summary','intent','style','relationship_stage','expression_memory') if isinstance(structured.get(k), str)}
                        observation.update(type='model_observation', evidence_snapshot_id=snapshot_id)
                        db.execute('INSERT OR IGNORE INTO session_decisions VALUES(?,?,?,?,?,?)', (event['id'], snapshot_id, snap['session_id'], snap['revision'], dumps(observation), now))
            self.audit(db, 'batch.' + state, batch_id, error)

    def results(self, source, account='', after=0, limit=100):
        with self.db() as db:
            rows = db.execute('SELECT * FROM results WHERE source=? AND (?="" OR account=?) AND seq>? ORDER BY seq LIMIT ?', (source, account, account, after, limit)).fetchall()
        return [{'cursor': x['seq'], 'id': x['id'], 'acknowledged': bool(x['acknowledged']), **json.loads(x['payload'])} for x in rows]

    def acknowledge(self, result_id, source):
        with self.db(True) as db:
            if not db.execute('SELECT 1 FROM results WHERE id=? AND source=?', (result_id, source)).fetchone():
                raise LookupError()
            db.execute('UPDATE results SET acknowledged=1 WHERE id=? AND source=?', (result_id, source))
        return {'ok': True}

    def stats(self):
        with self.db() as db:
            counts = {x['state']: x['n'] for x in db.execute('SELECT state,count(*) n FROM messages GROUP BY state')}
            age = db.execute("SELECT min(created) FROM messages WHERE state='queued'").fetchone()[0]
            return dict(counts=counts, queued_age_seconds=round(time.time() - age) if age else 0,
                        messages=sum(counts.values()), threads=db.execute('SELECT count(*) FROM threads').fetchone()[0],
                        running=db.execute("SELECT count(*) FROM batches WHERE status IN ('dispatching','waiting','reconciling','attention')").fetchone()[0],
                        unacknowledged=db.execute('SELECT count(*) FROM results WHERE acknowledged=0').fetchone()[0])
