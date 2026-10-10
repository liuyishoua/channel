"""Source-scoped conversation facts, evidence-backed memory and immutable turn inputs.

A session is a business identity, never a provider session or Multica issue.
Synchronization cannot enqueue a model task. All SQL writes use Store's lock.
"""
from datetime import datetime, timezone
import re
import time
import uuid
from flask import g, request
from .domain import Problem, digest, dumps, identifier, inspect_context, integer

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,source TEXT NOT NULL,account TEXT NOT NULL,surface TEXT NOT NULL,conversation TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,memory_revision INTEGER NOT NULL DEFAULT 0,coverage TEXT NOT NULL DEFAULT '{}',created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(source,account,surface,conversation));
CREATE TABLE IF NOT EXISTS session_messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,message_id TEXT NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL,body TEXT NOT NULL,sent_at REAL NOT NULL,changed_revision INTEGER NOT NULL,UNIQUE(session_id,message_id));
CREATE INDEX IF NOT EXISTS session_timeline ON session_messages(session_id,sent_at,seq);
CREATE INDEX IF NOT EXISTS session_changes ON session_messages(session_id,changed_revision);
CREATE TABLE IF NOT EXISTS session_memories(session_id TEXT NOT NULL,memory_key TEXT NOT NULL,body TEXT NOT NULL,version INTEGER NOT NULL,expires REAL NOT NULL,PRIMARY KEY(session_id,memory_key));
CREATE TABLE IF NOT EXISTS session_snapshots(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,revision INTEGER NOT NULL,memory_revision INTEGER NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,UNIQUE(session_id,request_id));
CREATE TABLE IF NOT EXISTS session_decisions(event_id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL,session_id TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS session_dispatches(snapshot_id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,turn_key TEXT UNIQUE NOT NULL);
'''


def instant(value, name):
    try:
        if not isinstance(value, str):
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        result = parsed.timestamp()
        if result <= 0:
            raise ValueError()
        return result
    except (ValueError, OverflowError, OSError):
        raise Problem(name + ' 必须是带时区的 ISO 时间') from None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def terms(text):
    # Deterministic lexical recall, not a claim of semantic search. A bounded
    # contiguous tail is always included, even if the latest text has no keywords.
    words = re.findall(r'[a-zA-Z0-9]{3,}', text.lower())
    for chunk in re.findall(r'[\u4e00-\u9fff]{2,}', text):
        words.extend(chunk[i:i+2] for i in range(len(chunk)-1))
    return list(dict.fromkeys(x for x in words if x not in {'哈哈','好的','可以','什么','怎么','这个','那个','就是','我们','你们'}))[:24]


class Sessions:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript(SCHEMA)

    def owned(self, db, session_id, source, account):
        account = identifier(account, 'account_id')
        row = db.execute('SELECT * FROM sessions WHERE id=? AND source=? AND account=?', (session_id, source, account)).fetchone()
        if not row:
            raise LookupError()
        return row

    def open(self, source, data):
        account = identifier(data.get('account_id'), 'account_id')
        conversation = identifier(data.get('conversation_id'), 'conversation_id')
        surface = identifier(data.get('surface', 'dm'), 'surface', 80)
        if surface not in ('dm','comment','notification','content','manual','schedule'):
            raise Problem('业务入口无效')
        now = time.time()
        with self.store.db(True) as db:
            db.execute('INSERT OR IGNORE INTO sessions(id,source,account,surface,conversation,created,updated) VALUES(?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()),source,account,surface,conversation,now,now))
            row = db.execute('SELECT * FROM sessions WHERE source=? AND account=? AND surface=? AND conversation=?', (source,account,surface,conversation)).fetchone()
        return dict(row)

    def sync(self, source, session_id, data):
        entries = data.get('messages')
        if not isinstance(entries,list) or len(entries)>200 or len(dumps(data).encode())>1024*1024:
            raise Problem('每次同步最多200条消息、1MB')
        normalized=[]
        for entry in entries:
            if not isinstance(entry,dict):
                raise Problem('消息必须是对象')
            mid=identifier(entry.get('message_id'),'message_id',200)
            version=integer(entry.get('revision',1),'revision',1,2147483647)
            stamp=instant(entry.get('sent_at'),'sent_at')
            if stamp>time.time()+300:
                raise Problem('消息时间超出允许的时钟偏差')
            direction=entry.get('direction')
            if direction not in ('inbound','outbound'):
                raise Problem('direction 必须是 inbound/outbound')
            text=entry.get('text','')
            media=entry.get('media',[])
            revoked=entry.get('revoked',False)
            if not isinstance(text,str) or len(text)>16000 or not isinstance(media,list) or len(media)>8 or not isinstance(revoked,bool):
                raise Problem('消息正文、媒体或撤回状态无效')
            for ref in media:
                if not isinstance(ref,dict) or ref.keys()-{'asset_id','kind','label','content_type','library'}:
                    raise Problem('媒体仅接受素材标识和描述，不接受地址或原始载荷')
                identifier(ref.get('asset_id'),'asset_id',200)
            inspect_context({'text':text,'media':media})
            if len(dumps(media).encode())>8192:
                raise Problem('媒体引用过大')
            actor=identifier(entry.get('actor_id'),'actor_id',200,optional=True)
            body=dict(message_id=mid,direction=direction,role='self' if direction=='outbound' else 'peer',actor_id=actor,text='' if revoked else text,media=[] if revoked else media,sent_at=iso(stamp),revoked=revoked)
            normalized.append((mid,version,stamp,body,digest(body)))
        coverage=data.get('coverage')
        if coverage is not None:
            if not isinstance(coverage,dict) or not isinstance(coverage.get('complete'),bool):
                raise Problem('coverage 必须包含 complete')
            since,until=instant(coverage.get('since'),'coverage.since'),instant(coverage.get('until'),'coverage.until')
            if since>until or until>time.time()+300:
                raise Problem('历史覆盖时间无效')
            coverage={'since':iso(since),'until':iso(until),'complete':coverage['complete']}
        with self.store.db(True) as db:
            session=self.owned(db,session_id,source,data.get('account_id'))
            revision=session['revision']; changed=0
            for mid,version,stamp,body,fingerprint in normalized:
                old=db.execute('SELECT * FROM session_messages WHERE session_id=? AND message_id=?',(session_id,mid)).fetchone()
                if old:
                    if version<old['revision']:
                        continue  # An out-of-order transport retry cannot undo a correction.
                    if version==old['revision']:
                        if fingerprint!=old['digest']:
                            raise Problem('同一消息版本内容冲突',409)
                        continue
                    previous=json_load(old['body'])
                    if previous['direction']!=body['direction'] or previous.get('actor_id','')!=actor or old['sent_at']!=stamp:
                        raise Problem('消息身份与发送时间不可修改',409)
                revision+=1;changed+=1
                db.execute('INSERT INTO session_messages(session_id,message_id,revision,digest,body,sent_at,changed_revision) VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id,message_id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,body=excluded.body,changed_revision=excluded.changed_revision',
                           (session_id,mid,version,fingerprint,dumps(body),stamp,revision))
            db.execute('UPDATE sessions SET revision=?,coverage=?,updated=? WHERE id=?',(revision,dumps(coverage) if coverage is not None and (not json_load(session['coverage']) or instant(coverage['until'],'until')>=instant(json_load(session['coverage'])['until'],'until')) else session['coverage'],time.time(),session_id))
            self.store.audit(db,'session.synced',session_id,str(changed)+' changed')
        return {'session_id':session_id,'revision':revision,'changed':changed}

    def messages(self,source,session_id,account,limit=50,before='',since=None,until=None,query=''):
        integer(limit,'limit',1,100)
        if not isinstance(query,str) or len(query)>200:
            raise Problem('query 最多200字')
        with self.store.db() as db:
            self.owned(db,session_id,source,account)
            sql='SELECT * FROM session_messages WHERE session_id=?';args=[session_id]
            if before:
                anchor=db.execute('SELECT sent_at,seq FROM session_messages WHERE session_id=? AND seq=?',(session_id,before)).fetchone()
                if not anchor:
                    raise Problem('分页游标不属于本会话')
                sql+=' AND (sent_at<? OR (sent_at=? AND seq<?))';args.extend([anchor['sent_at'],anchor['sent_at'],anchor['seq']])
            if since:
                sql+=' AND sent_at>=?';args.append(instant(since,'since'))
            if until:
                sql+=' AND sent_at<=?';args.append(instant(until,'until'))
            if query:
                sql+=" AND instr(lower(json_extract(body,'$.text')),lower(?))>0";args.append(query)
            rows=db.execute(sql+' ORDER BY sent_at DESC,seq DESC LIMIT ?',(*args,limit+1)).fetchall()
        return {'items':[{**json_load(r['body']),'revision':r['revision']} for r in rows[:limit]],'next_cursor':rows[limit-1]['seq'] if len(rows)>limit else None}

    def memory(self,source,session_id,data):
        key=identifier(data.get('key'),'key',80)
        expected=integer(data.get('expected_version',0),'expected_version',0,2147483647)
        kind=data.get('kind');content=data.get('text');evidence=data.get('evidence',[])
        if kind not in ('fact','preference','style','open_loop','boundary','observation') or not isinstance(content,str) or not 1<=len(content)<=1000 or not isinstance(evidence,list) or not 1<=len(evidence)<=20:
            raise Problem('记忆必须包含类型、正文和1至20条消息依据')
        inspect_context({'text':content})
        ttl=integer(data.get('ttl_seconds',86400),'ttl_seconds',60,2592000)
        with self.store.db(True) as db:
            session=self.owned(db,session_id,source,data.get('account_id'))
            evidence_versions={}
            for mid in evidence:
                identifier(mid,'evidence message_id',200)
                row=db.execute('SELECT body,revision FROM session_messages WHERE session_id=? AND message_id=?',(session_id,mid)).fetchone()
                if not row or json_load(row['body'])['revoked']:
                    raise Problem('记忆依据不存在或已撤回')
                evidence_versions[mid]=row['revision']
            old=db.execute('SELECT version,body FROM session_memories WHERE session_id=? AND memory_key=?',(session_id,key)).fetchone()
            # Idempotent retry after a lost successful response.
            body={'key':key,'kind':kind,'text':content,'evidence':evidence,'evidence_versions':evidence_versions,'ttl_seconds':ttl}
            if old and old['version']==expected+1 and json_load(old['body'])==body:
                return {'key':key,'version':old['version'],'memory_revision':session['memory_revision']}
            if (old['version'] if old else 0)!=expected:
                raise Problem('记忆已更新，请重新读取后修改',409)
            if not old and db.execute('SELECT count(*) FROM session_memories WHERE session_id=?',(session_id,)).fetchone()[0]>=100:
                raise Problem('单会话记忆最多100条，请更新已有条目')
            db.execute('INSERT INTO session_memories VALUES(?,?,?,?,?) ON CONFLICT(session_id,memory_key) DO UPDATE SET body=excluded.body,version=excluded.version,expires=excluded.expires',(session_id,key,dumps(body),expected+1,time.time()+ttl))
            db.execute('UPDATE sessions SET memory_revision=memory_revision+1 WHERE id=?',(session_id,))
            self.store.audit(db,'session.memory_updated',session_id,key)
            return {'key':key,'version':expected+1,'memory_revision':session['memory_revision']+1}

    def memories(self,db,session_id,now):
        result=[]
        for row in db.execute('SELECT * FROM session_memories WHERE session_id=? AND expires>? ORDER BY memory_key',(session_id,now)):
            body=json_load(row['body'])
            # A correction or withdrawal of evidence invalidates memory too.
            evidence=[]
            for mid in body['evidence']:
                fact=db.execute('SELECT body,revision FROM session_messages WHERE session_id=? AND message_id=?',(session_id,mid)).fetchone()
                if not fact or json_load(fact['body'])['revoked'] or fact['revision']!=body['evidence_versions'][mid]:
                    break
                evidence.append(mid)
            else:
                result.append({**body,'version':row['version'],'expires_at':iso(row['expires'])})
        return result

    def retire_memory(self,source,session_id,key,data):
        expected=integer(data.get('expected_version'),'expected_version',1,2147483647)
        with self.store.db(True) as db:
            session=self.owned(db,session_id,source,data.get('account_id'))
            row=db.execute('SELECT * FROM session_memories WHERE session_id=? AND memory_key=?',(session_id,key)).fetchone()
            if not row: raise LookupError()
            body=json_load(row['body'])
            if body.get('retired') and row['version']==expected+1:
                return {'key':key,'version':row['version'],'memory_revision':session['memory_revision']}
            if row['version']!=expected: raise Problem('记忆版本已变化',409)
            body['retired']=True
            db.execute('UPDATE session_memories SET body=?,version=?,expires=? WHERE session_id=? AND memory_key=?',(dumps(body),expected+1,time.time(),session_id,key))
            db.execute('UPDATE sessions SET memory_revision=memory_revision+1 WHERE id=?',(session_id,))
            self.store.audit(db,'session.memory_retired',session_id,key)
            return {'key':key,'version':expected+1,'memory_revision':session['memory_revision']+1}

    def snapshot(self,source,session_id,data):
        request_id=identifier(data.get('request_id'),'request_id',200)
        if not isinstance(data.get('reanalysis',False),bool):
            raise Problem('reanalysis 必须是布尔值')
        mids=data.get('trigger_message_ids')
        if not isinstance(mids,list) or not 1<=len(mids)<=20 or not all(isinstance(x,str) for x in mids) or len(set(mids))!=len(mids):
            raise Problem('必须指定1至20条不同的触发消息')
        for mid in mids: identifier(mid,'trigger message_id',200)
        recent=integer(data.get('recent_limit',20),'recent_limit',1,40)
        related=integer(data.get('related_limit',6),'related_limit',0,12)
        window=integer(data.get('window_hours',48),'window_hours',1,168)
        budget=integer(data.get('max_context_bytes',14000),'max_context_bytes',4096,20000)
        max_age=integer(data.get('max_trigger_age_seconds',900),'max_trigger_age_seconds',30,86400)
        fingerprint=digest(data);now=time.time()
        with self.store.db(True) as db:
            session=self.owned(db,session_id,source,data.get('account_id'))
            old=db.execute('SELECT * FROM session_snapshots WHERE session_id=? AND request_id=?',(session_id,request_id)).fetchone()
            if old:
                if old['digest']!=fingerprint: raise Problem('同一快照请求 ID 内容冲突',409)
                return json_load(old['body'])
            triggers=[]
            for mid in mids:
                r=db.execute('SELECT * FROM session_messages WHERE session_id=? AND message_id=?',(session_id,mid)).fetchone()
                if not r: raise Problem('触发消息尚未同步')
                if json_load(r['body'])['direction']!='inbound': raise Problem('只能由收到的消息触发决策')
                triggers.append(r)
            newest=max(triggers,key=lambda r:(r['sent_at'],r['seq']))
            last=db.execute("SELECT * FROM session_messages WHERE session_id=? AND sent_at<=? AND json_extract(body,'$.revoked')=0 ORDER BY sent_at DESC,seq DESC LIMIT 1",(session_id,now)).fetchone()
            eligible=all(not json_load(r['body'])['revoked'] and now-max_age<=r['sent_at']<=now for r in triggers)
            reason='' if eligible else '触发消息已过期、撤回或时间异常'
            if last and (last['sent_at'],last['seq'])>(newest['sent_at'],newest['seq']):
                eligible=False;reason='已有更新消息或本人已回复'
            already=db.execute("SELECT 1 FROM session_decisions d JOIN session_snapshots s ON s.id=d.snapshot_id WHERE d.session_id=? AND json_extract(s.body,'$.trigger_message_ids')=? LIMIT 1",(session_id,dumps(mids))).fetchone()
            if already and not data.get('reanalysis',False): eligible=False;reason='此轮消息已有决策'
            tail=db.execute("SELECT * FROM session_messages WHERE session_id=? AND sent_at>=? AND sent_at<=? ORDER BY sent_at DESC,seq DESC LIMIT ?",(session_id,now-window*3600,now,recent)).fetchall()
            chosen={r['message_id']:r for r in tail};chosen.update({r['message_id']:r for r in triggers})
            words=terms(' '.join(json_load(r['body'])['text'] for r in triggers))
            recall=[]
            if related and words:
                clauses=' OR '.join("instr(lower(json_extract(body,'$.text')),?)>0" for _ in words)
                candidates=db.execute("SELECT * FROM session_messages WHERE session_id=? AND sent_at>=? AND sent_at<=? AND json_extract(body,'$.revoked')=0 AND ("+clauses+") ORDER BY sent_at DESC,seq DESC LIMIT 200",(session_id,now-window*3600,now,*words)).fetchall()
                candidates=[r for r in candidates if r['message_id'] not in chosen]
                candidates.sort(key=lambda r:(sum(w in json_load(r['body'])['text'].lower() for w in words),r['sent_at']),reverse=True)
                recall=candidates[:related]
                chosen.update({r['message_id']:r for r in recall})
            memories=self.memories(db,session_id,now)
            # Relevance selects optional memory; never evict the immediate dialog
            # to make room for a growing profile. Boundary records are prioritized.
            memories.sort(key=lambda x:(x['kind']=='boundary',sum(w in x['text'].lower() for w in words)),reverse=True)
            memories=memories[:8]
            coverage=json_load(session['coverage'])
            context={'session':{'id':session_id,'revision':session['revision'],'memory_revision':session['memory_revision']},'history':{'since':iso(now-window*3600),'until':iso(now),'window_hours':window,'complete':bool(coverage.get('complete')),'coverage':coverage},'current_message':{'message_id':newest['message_id']},'trigger_message_ids':mids,'messages':[],'memories':memories,'previous_analysis':{}}
            decision=db.execute('SELECT d.body,p.body AS input_body FROM session_decisions d JOIN session_snapshots p ON p.id=d.snapshot_id WHERE d.session_id=? AND d.created>? ORDER BY d.created DESC LIMIT 1',(session_id,now-86400)).fetchone()
            if decision:
                for fact in json_load(decision['input_body'])['context']['messages']:
                    current=db.execute('SELECT digest FROM session_messages WHERE session_id=? AND message_id=?',(session_id,fact['message_id'])).fetchone()
                    if not current or current['digest']!=digest(fact): break
                else: context['previous_analysis']=json_load(decision['body'])
            total=db.execute('SELECT count(*) FROM session_messages WHERE session_id=? AND sent_at>=? AND sent_at<=?',(session_id,now-window*3600,now)).fetchone()[0]
            context['history']['total_messages']=total
            omitted=False
            def rebuild():
                context['messages']=[json_load(r['body']) for r in sorted(chosen.values(),key=lambda r:(r['sent_at'],r['seq']))]
                context['history'].update(included_messages=len(chosen),context_truncated=omitted or len(chosen)<total)
            rebuild()
            while len(dumps(context).encode())>budget:
                omitted=True
                if context['memories']: context['memories'].pop()
                elif context['previous_analysis']: context['previous_analysis']={}
                elif recall:
                    r=recall.pop();chosen.pop(r['message_id'],None)
                else:
                    removable=[r for k,r in chosen.items() if k not in mids]
                    if not removable: raise Problem('本轮新消息超过上下文预算，请分批处理')
                    chosen.pop(min(removable,key=lambda r:(r['sent_at'],r['seq']))['message_id'])
                rebuild()
            sid=str(uuid.uuid4())
            deadline=min([now+300,min(r['sent_at'] for r in triggers)+max_age]+[instant(m['expires_at'],'expires_at') for m in context['memories']])
            body={'id':sid,'session_id':session_id,'revision':session['revision'],'memory_revision':session['memory_revision'],'eligible':eligible,'reason':reason,'created_at':iso(now),'expires_at':iso(deadline),'trigger_message_ids':mids,'reanalysis':data.get('reanalysis',False),'context':context,'context_bytes':len(dumps(context).encode())}
            db.execute('INSERT INTO session_snapshots VALUES(?,?,?,?,?,?,?,?)',(sid,session_id,request_id,fingerprint,session['revision'],session['memory_revision'],dumps(body),now))
            self.store.audit(db,'session.snapshot_created',sid,str(body['context_bytes'])+' bytes')
        return body

    def resolve(self,db,snapshot_id,source,account):
        account=identifier(account,'account_id')
        row=db.execute('SELECT p.*,s.source,s.account,s.surface,s.conversation,s.revision AS current_revision,s.memory_revision AS current_memory_revision FROM session_snapshots p JOIN sessions s ON s.id=p.session_id WHERE p.id=? AND s.source=? AND s.account=?',(snapshot_id,source,account)).fetchone()
        if not row: raise LookupError()
        return row,json_load(row['body'])

    def validate(self,source,snapshot_id,account):
        with self.store.db() as db:
            row,body=self.resolve(db,snapshot_id,source,account)
        current=body['eligible'] and row['revision']==row['current_revision'] and row['memory_revision']==row['current_memory_revision'] and instant(body['expires_at'],'expires_at')>time.time()
        return {'current':current,'reason':'' if current else body['reason'] or '会话、记忆已更新或快照过期','session_id':row['session_id'],'revision':row['current_revision']}


def json_load(value):
    import json
    return json.loads(value)


def register_sessions(app,sessions):
    @app.get('/v1/sessions')
    def list_sessions():
        account=identifier(request.args.get('account_id'),'account_id')
        try:
            limit=integer(int(request.args.get('limit','50')),'limit',1,100)
        except ValueError: raise Problem('分页参数无效')
        with sessions.store.db() as db:
            sql='SELECT * FROM sessions WHERE source=? AND account=?';args=[g.source,account]
            if request.args.get('before'):
                anchor=sessions.owned(db,request.args['before'],g.source,account)
                sql+=' AND (created<? OR (created=? AND id<?))';args.extend([anchor['created'],anchor['created'],anchor['id']])
            rows=db.execute(sql+' ORDER BY created DESC,id DESC LIMIT ?',(*args,limit+1)).fetchall()
        return {'items':[dict(x) for x in rows[:limit]],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}
    @app.get('/v1/sessions/<session_id>')
    def get_session(session_id):
        with sessions.store.db() as db: return dict(sessions.owned(db,session_id,g.source,request.args.get('account_id')))
    @app.post('/v1/sessions')
    def open_session(): return sessions.open(g.source,request.json)
    @app.post('/v1/sessions/<session_id>/messages')
    def sync_session(session_id): return sessions.sync(g.source,session_id,request.json)
    @app.get('/v1/sessions/<session_id>/messages')
    def session_messages(session_id):
        try: limit=int(request.args.get('limit','50'))
        except ValueError: raise Problem('limit 必须是整数')
        return sessions.messages(g.source,session_id,request.args.get('account_id'),limit,request.args.get('before',''),request.args.get('since'),request.args.get('until'),request.args.get('query',''))
    @app.get('/v1/sessions/<session_id>/memories')
    def session_memories(session_id):
        with sessions.store.db() as db:
            row=sessions.owned(db,session_id,g.source,request.args.get('account_id'))
            return {'items':sessions.memories(db,session_id,time.time()),'memory_revision':row['memory_revision']}
    @app.post('/v1/sessions/<session_id>/memories')
    def session_memory(session_id): return sessions.memory(g.source,session_id,request.json)
    @app.post('/v1/sessions/<session_id>/memories/<key>/retire')
    def retire_session_memory(session_id,key): return sessions.retire_memory(g.source,session_id,key,request.json)
    @app.post('/v1/sessions/<session_id>/snapshots')
    def session_snapshot(session_id): return sessions.snapshot(g.source,session_id,request.json)
    @app.get('/v1/session-snapshots/<snapshot_id>/validate')
    def validate_snapshot(snapshot_id): return sessions.validate(g.source,snapshot_id,request.args.get('account_id'))
    @app.get('/v1/session-snapshots/<snapshot_id>')
    def get_snapshot(snapshot_id):
        with sessions.store.db() as db:
            _,body=sessions.resolve(db,snapshot_id,g.source,request.args.get('account_id'))
            return body
