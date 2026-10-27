"""Hub-authenticated WSGI tenant boundary. No browser-selected tenant paths.

Only a verified hub subject selects the application's private data directory.
Legacy data belongs to one explicitly provisioned subject, never first visitor.
"""
from http.cookies import SimpleCookie
from pathlib import Path
import json
import secrets
import threading
import urllib.request
import urllib.error
from werkzeug.wrappers import Request, Response

COOKIE='ops_workspace_session'
HUB='https://d.58nft.net:9461/platform-select'


class IdentityUnavailable(Exception):pass


def verify_identity(token,endpoint='http://127.0.0.1:8787/api/auth/workspace/verify'):
    if not token or len(token)>4096:return None
    req=urllib.request.Request(endpoint,headers={'Authorization':'Bearer '+token})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5) as response:value=json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401,403):return None
        raise IdentityUnavailable() from None
    except Exception:raise IdentityUnavailable() from None
    if not isinstance(value,dict) or type(value.get('id')) is not int or value['id']<1:raise IdentityUnavailable()
    return value


class WorkspaceIdentity:
    def __init__(self,app,directory,slug,factory,owner_id,verify=verify_identity,machine_prefixes=(),origin="",machine_key="",owner_only=False):
        self.primary_app=app;self.original=app.wsgi_app;self.root=Path(directory);self.slug=slug;self.factory=factory
        self.owner_id=int(owner_id);self.verify=verify;self.origin=origin;self.machine_key=machine_key;self.owner_only=owner_only;self.apps={};self.lock=threading.RLock();self.machine_prefixes=machine_prefixes
        self.attach(app)
        # Rebuild existing tenants so their explicitly enabled workers survive restart.
        users=self.root/'users'
        if users.exists():
            for path in users.iterdir():
                if path.is_dir() and path.name.isdigit() and int(path.name)!=self.owner_id:self.tenant(int(path.name))

    @staticmethod
    def attach(app):
        if not app.secret_key:return  # Runtime has no legacy Flask session guard.
        from flask import request,session
        def trusted_identity():
            identity=request.environ.get('ops.verified_identity')
            if identity:
                if session.get('ops_owner')!=identity['id']:session.clear()
                session['authenticated']=True;session['ops_owner']=identity['id']
        app.before_request_funcs.setdefault(None,[]).insert(0,trusted_identity)

    def tenant(self,uid):
        if uid==self.owner_id:return self.original
        with self.lock:
            if uid not in self.apps:
                path=self.root/'users'/str(uid);path.mkdir(mode=0o700,parents=True,exist_ok=True)
                app=self.factory(path);self.attach(app);self.apps[uid]=app
            return self.apps[uid]

    def __call__(self,environ,start_response):
        req=Request(environ);path=req.path
        # Public resources carry no account data. Machine endpoints retain their
        # existing source credentials and map only to the declared legacy owner.
        if path.startswith('/static/') or any(path.startswith(p) for p in self.machine_prefixes):
            return self.original(environ,start_response)
        machine=req.headers.get('X-Ops-Machine-Key','')
        if self.machine_key and machine and secrets.compare_digest(machine,self.machine_key):
            environ['ops.verified_identity']={'id':self.owner_id,'username':'service'}
            if path=='/api/login':return Response('{"ok":true}',content_type='application/json')(environ,start_response)
            return self.original(environ,start_response)
        # Official verification frames use a capability nonce on their isolated host.
        # Route that nonce to its owning runtime; never expose general console APIs.
        if self.slug=='douyin' and req.host.split(':')[0]=='verification.localhost':
            if path=='/verification-frame':return self.original(environ,start_response)
            if path.startswith('/verification-request/'):
                nonce=path.split('/')[2]
                with self.lock:
                    candidates=[(self.primary_app,self.original)]+[(a,a) for a in self.apps.values()]
                    for app,handler in candidates:
                        accounts=app.extensions['console'].accounts
                        with accounts.guard:
                            found=any(c.get('id')==nonce for c in accounts.verifications.values())
                        if found:return handler(environ,start_response)
                return Response('{"error":"验证已失效"}',status=404,content_type='application/json')(environ,start_response)
        token=req.cookies.get(COOKIE,'')
        try:identity=self.verify(token)
        except IdentityUnavailable:
            return Response(json.dumps({'error':'统一登录服务暂不可用，请稍后重试'}),status=503,content_type='application/json')(environ,start_response)
        if not identity:
            if path.startswith('/api/'):
                response=Response(json.dumps({'error':'请返回应用中心登录','login_url':HUB+'?open='+self.slug}),status=401,content_type='application/json')
            else:response=Response('',status=302,headers={'Location':HUB+'?open='+self.slug})
            response.headers['Cache-Control']='no-store';return response(environ,start_response)
        if path=='/api/logout':
            return self.logout(req,token,environ,start_response)
        if self.owner_only and not identity.get('app_access', {}).get(self.slug, False):
            access_url = 'https://d.58nft.net/permissions?app=' + self.slug
            response = (Response(json.dumps({'error': '尚未开通此应用，请在彩虹桥申请权限。', 'access_url': access_url}), status=403, content_type='application/json')
                        if path.startswith('/api/') else Response('', status=302, headers={'Location': access_url}))
            return response(environ, start_response)
        if path=='/api/login':return Response('{"error":"请使用应用中心账号登录"}',status=409,content_type='application/json')(environ,start_response)
        if path=='/login':return Response('',status=302,headers={'Location':'/'})(environ,start_response)
        if path=='/api/workspace-identity':
            return Response(json.dumps({'owner':identity,'hub_url':HUB,'app':self.slug}),content_type='application/json',headers={'Cache-Control':'no-store'})(environ,start_response)
        environ['ops.verified_identity']=identity
        return (self.original if self.owner_only else self.tenant(identity['id']))(environ,start_response)

    def logout(self,req,token,environ,start_response):
        if req.method!='POST' or req.headers.get('Origin','').rstrip('/')!=(self.origin or req.host_url.rstrip('/')) or req.headers.get('X-App-Request')!='1':
            return Response('{"error":"请求来源无效"}',status=403,content_type='application/json')(environ,start_response)
        request=urllib.request.Request('http://127.0.0.1:8787/api/auth/workspace/logout',data=b'{}',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as result:result.read()
        except Exception:
            return Response('{"error":"退出未完成，请稍后重试"}',status=503,content_type='application/json')(environ,start_response)
        response=Response('{"ok":true}',content_type='application/json')
        response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='strict')
        return response(environ,start_response)


def install_identity(app,directory,slug,factory,machine_prefixes=()):
    path=Path(directory)/'workspace-owner.json'
    if not path.exists():return  # Explicit provisioning only; demo and tests unchanged.
    config=json.loads(path.read_text())
    if type(config.get('owner_id')) is not int or config['owner_id']<1:raise ValueError('Explicit workspace owner required')
    gate=WorkspaceIdentity(app,directory,slug,factory,config['owner_id'],machine_prefixes=machine_prefixes,origin=config.get('public_origin',''),machine_key=(Path(directory)/'workspace-machine-key').read_text().strip(),owner_only=config.get('owner_only',False))
    app.wsgi_app=gate
    app.extensions['workspace_identity']=gate
