"""Deliver static workspace entry assets in HTML with exact CSP hashes.

Only repository-owned /static resources are inlined. No user data or credentials
are embedded. The original source files remain independently downloadable.
"""
import base64
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

_SCRIPT=re.compile(r'<script\b(?P<attrs>[^>]*\bsrc=["\'](?P<src>[^"\']+)["\'][^>]*)>\s*</script>',re.I)
_STYLE=re.compile(r'<link\b(?=[^>]*\brel=["\']stylesheet["\'])(?=[^>]*\bhref=["\'](?P<src>[^"\']+)["\'])[^>]*>',re.I)

def policy_with_hashes(policy, hashes):
    directives=[part.strip() for part in policy.split(';') if part.strip()]
    for kind,values in hashes.items():
        for index,directive in enumerate(directives):
            if directive.split()[0]==kind:
                directives[index]=directive+' '+ ' '.join(sorted(set(values)));break
    return '; '.join(directives)

class PageAssets:
    def __init__(self,directory,bundles=None):
        self.directory=Path(directory).resolve();self.bundles=bundles or {};self.cache={}

    def asset(self,url):
        parsed=urlsplit(url)
        if parsed.scheme or parsed.netloc:return None
        path=parsed.path
        if path.startswith('/static/'):path=path[8:]
        elif path.startswith('static/'):path=path[7:]
        else:return None
        target=(self.directory/path).resolve()
        if not target.is_relative_to(self.directory) or not target.is_file():return None
        return target

    def render(self,filename):
        path=self.directory/filename;original=path.read_text(encoding='utf-8')
        paths=[path]
        for pattern in (_SCRIPT,_STYLE):
            for match in pattern.finditer(original):
                source=self.asset(match['src'])
                if source:
                    source=self.directory/self.bundles.get(source.name,source.name)
                    paths.append(source)
        stamp=tuple((str(p),p.stat().st_mtime_ns,p.stat().st_size) for p in paths)
        previous=self.cache.get(filename)
        if previous and previous[0]==stamp:return previous[1],previous[2]
        hashes={'script-src':[],'style-src':[]}
        def digest(kind,value):
            hashes[kind].append("'sha256-"+base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()+"'")
        def script(match):
            source=self.asset(match['src'])
            if source is None:return match[0]
            source=self.directory/self.bundles.get(source.name,source.name)
            code=re.sub(r'</script',lambda _: '<\\/script',source.read_text(encoding='utf-8'),flags=re.I)
            # Inline modules resolve relative specifiers against the document.
            # Bundled entries have none; preserve correct paths for any others.
            code=re.sub(r"(from\s*['\"]|import\s*\(['\"])(\./[^'\"]+)(['\"])",lambda m:m[1]+'/static/'+m[2][2:]+m[3],code)
            attrs=re.sub(r'\s*src=["\'][^"\']+["\']','',match['attrs'],flags=re.I)
            digest('script-src',code)
            return '<script'+attrs+'>'+code+'</script>'
        def style(match):
            source=self.asset(match['src'])
            if source is None:return match[0]
            css=source.read_text(encoding='utf-8').replace('</style','<\\/style')
            digest('style-src',css);return '<style>'+css+'</style>'
        html=_STYLE.sub(style,_SCRIPT.sub(script,original))
        if '<div id="app"></div>' in html:
            html=html.replace('<div id="app"></div>','<div id="app"><div class="workspace-loading" role="status">正在读取工作台…</div></div>',1)
        self.cache[filename]=(stamp,html,hashes)
        return html,hashes

def install_pages(app,pages,bundles=None):
    from flask import make_response
    renderer=PageAssets(app.static_folder,bundles)
    original=app.send_static_file
    def send(filename):
        if filename not in pages:return original(filename)
        html,hashes=renderer.render(filename)
        response=make_response(html);response._workspace_hashes=hashes
        return response
    app.send_static_file=send
    # Register before normal security headers so this runs after them.
    @app.after_request
    def secure_inline(response):
        hashes=getattr(response,'_workspace_hashes',None)
        if hashes:
            response.headers['Content-Security-Policy']=policy_with_hashes(response.headers.get('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'"),hashes)
            response.headers['Cache-Control']='no-store'
            response.headers.pop('ETag',None)
        return response
    return renderer
