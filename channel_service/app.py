import json
import os
from pathlib import Path
import secrets
import time
from flask import Flask, g, jsonify, redirect, request, send_file, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from .domain import Problem, group_key, integer, match_route, normalize_message
from .store import Store
from .worker import Worker
from .automation import Automation, register_automation
from .control import register_control
from .sessions import register_sessions

ROOT = Path(__file__).resolve().parents[1]


def private_file(path, value):
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(value)
    os.chmod(path, 0o600)


def create_app(directory, multica, auth=True, public_origin='', background=False, demo=False):
    store = Store(directory)
    private_file(store.directory / 'command-key', secrets.token_urlsafe(48))
    command_key = (store.directory / 'command-key').read_text().strip()
    private_file(store.directory / 'session-key', secrets.token_hex(32))
    private_file(store.directory / 'admin-password', secrets.token_urlsafe(24))
    password_hash = generate_password_hash((store.directory / 'admin-password').read_text().strip())
    app = Flask(__name__, static_folder='static')
    from .page_assets import install_pages
    install_pages(app, {'index.html','login.html','automation.html'})
    app.config.update(SECRET_KEY=(store.directory / 'session-key').read_text(), MAX_CONTENT_LENGTH=9 * 1024 * 1024,
                      SESSION_COOKIE_NAME='channel_console', SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      SESSION_COOKIE_SECURE=public_origin.startswith('https:'), PERMANENT_SESSION_LIFETIME=43200)
    worker = Worker(store, multica)
    automation = Automation(store)
    worker.automation = automation
    app.extensions.update(channel=store, worker=worker, multica=multica)
    app.extensions['automation'] = automation
    register_automation(app, automation)
    register_control(app, automation)
    register_sessions(app, store.sessions)
    attempts = {}

    @app.before_request
    def guard():
        if request.path == '/healthz':
            return
        if request.path.startswith('/control/'):
            if request.headers.get('Origin') or not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + command_key):
                raise Problem('请提供独立指挥部凭据', 401)
            g.command_owner = 'headquarters'
            if request.method != 'GET' and (not request.is_json or not isinstance(request.get_json(silent=True), dict)):
                raise Problem('请求必须是 JSON 对象')
            return
        if request.path.startswith('/v1/'):
            # Machine endpoints never accept browser cookies as credentials.
            if request.headers.get('Origin'):
                raise Problem('请通过工作台后端或 SDK 接入消息 API', 403)
            token = request.headers.get('Authorization', '')
            if not token.startswith('Bearer '):
                raise Problem('请提供来源 Bearer 凭据', 401)
            g.source = store.authenticate(token[7:])
            if request.method == 'POST' and request.path != '/v1/assets':
                if not request.is_json or not isinstance(request.get_json(silent=True), dict):
                    raise Problem('请求必须是 JSON 对象')
            return
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('Origin', '').rstrip('/') != (public_origin or request.host_url.rstrip('/')) or request.headers.get('X-App-Request') != '1':
                raise Problem('请求来源无效', 403)
            if request.path != '/api/assets' and (not request.is_json or not isinstance(request.get_json(silent=True), dict)):
                raise Problem('请求必须是 JSON 对象')
        if request.path in ('/login', '/api/login') or request.path.startswith('/static/'):
            return
        if auth and not session.get('authenticated'):
            if request.path.startswith('/api/'):
                raise Problem('请先登录 Channel 工作台', 401)
            return redirect('/login')

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, Problem):
            return jsonify(error=str(exc)), exc.status
        if isinstance(exc, LookupError):
            return jsonify(error='记录不存在'), 404
        if isinstance(exc, HTTPException):
            return jsonify(error='请求无效或文件过大'), exc.code
        app.logger.error('Channel API error: %s', type(exc).__name__)
        return jsonify(error='操作未完成，请查看服务状态'), 502

    @app.get('/healthz')
    def health():
        with store.db() as db:
            db.execute('SELECT 1')
        return {'ok': True}

    @app.get('/login')
    def login_page():
        return app.send_static_file('login.html')

    @app.post('/api/login')
    def login():
        now = time.time()
        ip = request.remote_addr
        attempts[ip] = [x for x in attempts.get(ip, []) if now - x < 60]
        if len(attempts[ip]) >= 8:
            raise Problem('尝试过于频繁，请稍后再试', 429)
        if auth and not check_password_hash(password_hash, str(request.json.get('password', ''))):
            attempts[ip].append(now)
            raise Problem('访问密码不正确', 401)
        session.clear()
        session['authenticated'] = True
        session.permanent = True
        return {'ok': True}

    @app.post('/api/logout')
    def logout():
        session.clear()
        return {'ok': True}

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/api/meta')
    def meta():
        try:
            agents = multica.agents()
            projects = multica.projects()
            connected = True
        except Exception:
            agents, projects, connected = [], [], False
        return {'name': 'Channel 消息中枢', 'demo': demo, 'connected': connected, 'agents': agents, 'projects': projects,
                'multica_url': multica.public_url, 'workspace_slug': multica.slug,
                'kinds': ['text', 'image', 'mixed'], 'categories': ['reply_draft', 'content_analysis', 'comment_analysis', 'product_research', 'image_analysis']}

    @app.get('/api/overview')
    def overview():
        return store.stats()

    @app.get('/api/sources')
    def sources():
        return {'items': store.sources()}

    @app.post('/api/sources')
    def create_source():
        return store.source(request.json.get('id', ''), request.json.get('name'))

    @app.patch('/api/sources/<source_id>')
    def update_source(source_id):
        return store.change_source(source_id, request.json)

    @app.get('/api/routes')
    def routes():
        return {'items': store.routes()}

    @app.post('/api/routes')
    def create_route():
        return store.save_route(request.json)

    @app.put('/api/routes/<route_id>')
    def update_route(route_id):
        return store.save_route(request.json, route_id)

    @app.post('/api/route-preview')
    def preview():
        data = request.json
        message = normalize_message(data, data.get('source_id', ''))
        route = match_route(message, store.routes())
        return {'route': route, 'group_key': group_key(message, route) if route else None,
                'reason': '将按来源、账号、类别和业务会话归并；明确回复会进一步校验原消息' if route else '无匹配路由，正式提交时将保留为待分流消息'}

    @app.get('/api/messages')
    def messages():
        return {'items': store.messages(request.args.get('state', ''), request.args.get('source', ''))}

    @app.get('/api/messages/<event_id>')
    def message(event_id):
        return store.message(event_id)

    @app.post('/api/messages')
    def submit():
        data = request.json
        source = data.get('source_id', '')
        message, duplicate = store.ingest(source, data)
        return jsonify(**message, duplicate=duplicate), 200 if duplicate else 202

    @app.post('/api/messages/<event_id>/cancel')
    def cancel(event_id):
        return store.cancel(event_id)

    @app.post('/api/messages/<event_id>/retry')
    def retry(event_id):
        return store.retry(event_id)

    @app.post('/api/batches/<batch_id>/reconcile')
    def reconcile(batch_id):
        batch = store.batch(batch_id)
        if batch['status'] not in ('uncertain', 'reconciling'):
            raise Problem('仅投递状态不确定的批次需要核对', 409)
        store.update_batch(batch_id, status='reconciling', attempts=0, next_poll=time.time(), lease_until=0)
        with store.db(True) as db:
            store.audit(db, 'batch.reconcile_requested', batch_id)
        return {'ok': True}

    @app.get('/api/threads')
    def threads():
        return {'items': store.threads()}

    @app.post('/api/threads/<thread_id>/close')
    def close_thread(thread_id):
        return store.close_thread(thread_id)

    @app.post('/api/assets')
    def upload():
        f = request.files.get('file')
        if not f:
            raise Problem('请选择图片')
        source = request.form.get('source_id', '')
        if not any(x['id'] == source and x['enabled'] for x in store.sources()):
            raise Problem('来源不存在或已停用')
        return store.put_asset(source, request.form.get('account_id'), f.read(8 * 1024 * 1024 + 1))

    @app.get('/api/assets/<asset_id>')
    def asset(asset_id):
        value = store.asset(asset_id)
        return send_file(value['path'], mimetype=value['media_type'])

    @app.get('/api/audit')
    def audit():
        with store.db() as db:
            return {'items': [dict(x) for x in db.execute('SELECT * FROM audit ORDER BY seq DESC LIMIT 200')]}

    @app.get('/v1/catalog')
    def inbound_catalog():
        # Agent visibility comes from the configured workspace's authenticated API.
        # Routes remain restricted to the authenticated workbench source.
        agents = {x['id']: x for x in multica.agents()}
        rows = []
        for route in store.routes():
            if not route['enabled'] or not ('*' in route['sources'] or g.source in route['sources']):
                continue
            if route.get('goal_ids', ['*']) != ['*']:
                continue
            rows.append({**{k: route[k] for k in ('id', 'version', 'name', 'categories', 'kinds', 'grouping', 'agent_id')},
                         'account_ids': route.get('account_ids', ['*']),
                         'surfaces': route.get('surfaces', ['*']), 'event_types': route.get('event_types', ['*']),
                         'execution_mode': route.get('execution_mode','issue'), 'priority': route.get('priority',50),
                         'agent': agents.get(route['agent_id'])})
        return {'routes': rows, 'agents': list(agents.values()), 'multica_url': multica.public_url, 'connected': True,
                'capabilities': {'sessions': 1, 'independent_turn_context': True},
                'limits': {'images_per_request': 8, 'session_context_bytes': 20000, 'session_sync_messages': 200}}

    @app.post('/v1/bindings')
    def inbound_binding():
        data = request.json
        agent = next((item for item in multica.agents() if item['id'] == data.get('agent_id')), None)
        if not agent:
            raise Problem('所选 Agent 已不存在或没有权限，请刷新目录', 409)
        route = store.bind_agent(g.source, data.get('account_id'), data.get('purpose'), agent)
        safe = {key: route[key] for key in ('id', 'version', 'name', 'agent_id', 'account_ids',
                'sources', 'categories', 'surfaces', 'event_types', 'kinds', 'enabled', 'grouping')}
        return {'route': {**safe, 'agent': agent, 'execution_mode':route.get('execution_mode','issue'),
                          'priority':route.get('priority',50)}, 'agent': agent}

    @app.post('/v1/messages')
    def inbound():
        message, duplicate = store.ingest(g.source, request.json)
        return jsonify(**message, duplicate=duplicate), 200 if duplicate else 202

    @app.get('/v1/messages/<event_id>')
    def inbound_status(event_id):
        return store.message(event_id, g.source, request.args.get('account_id'))

    @app.post('/v1/messages/<event_id>/retry')
    def inbound_retry(event_id):
        return store.retry(event_id, g.source, (request.json or {}).get('account_id'))

    @app.post('/v1/assets')
    def inbound_asset():
        f = request.files.get('file')
        if not f:
            raise Problem('请选择图片')
        return store.put_asset(g.source, request.form.get('account_id'), f.read(8 * 1024 * 1024 + 1))

    @app.get('/v1/results')
    def results():
        try:
            after = int(request.args.get('after', '0'))
        except ValueError:
            raise Problem('after 必须是结果游标')
        if after < 0:
            raise Problem('after 必须大于等于 0')
        rows = store.results(g.source, request.args.get('account_id', ''), after)
        return {'items': rows, 'next_cursor': rows[-1]['cursor'] if rows else after}

    @app.post('/v1/results/<result_id>/ack')
    def ack(result_id):
        return store.acknowledge(result_id, g.source)

    if background:
        worker.start()
    return app
