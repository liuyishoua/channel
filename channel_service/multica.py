import json
import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit
import requests


class RemoteError(Exception):
    def __init__(self, status=0, ambiguous=False, code=''):
        reasons = {'active_duplicate_issue': '任务标题与已有活动任务重复'}
        message = 'Multica 请求未完成' if not status else f'Multica 返回 HTTP {status}'
        super().__init__(message + ('：' + reasons[code] if isinstance(code, str) and code in reasons else ''))
        self.status = status
        self.ambiguous = ambiguous


class Multica:
    def __init__(self, config_file):
        self.config_file = Path(config_file)
        self.config = json.loads(self.config_file.read_text())
        self.base = self.config['base_url'].rstrip('/')
        self.public_url = self.config.get('public_url', 'https://d.58nft.net:9447').rstrip('/')
        self.workspace = self.config['workspace_id']
        self.slug = self.config.get('workspace_slug', 'leo')
        u = urlsplit(self.base)
        if u.scheme != 'https' and not (u.scheme == 'http' and u.hostname in ('127.0.0.1', 'localhost', '::1')):
            raise ValueError('Multica must use HTTPS or loopback')

    def request(self, method, path, data=None, files=None):
        headers = {'Authorization': 'Bearer ' + self.config['token'], 'X-Workspace-ID': self.workspace}
        try:
            with requests.Session() as s:
                if urlsplit(self.base).hostname in ('127.0.0.1', 'localhost', '::1'):
                    s.trust_env = False
                r = s.request(method, self.base + path, headers=headers, json=data if files is None else None,
                              files=files, data=data if files is not None else None, timeout=(5, 30), allow_redirects=False)
                if not 200 <= r.status_code < 300:
                    try:
                        error = r.json()
                        code = error.get('code', '') if isinstance(error, dict) else ''
                    except ValueError:
                        code = ''
                    raise RemoteError(r.status_code, method != 'GET' and (r.status_code >= 500 or 300 <= r.status_code < 400), code)
                if not r.content:
                    return {}
                try:
                    return r.json()
                except ValueError:
                    raise RemoteError(502, method != 'GET')
        except requests.RequestException:
            raise RemoteError(0, method != 'GET') from None

    def renew_credential(self):
        result = self.request('POST', '/api/tokens/current/renew', {})
        if result.get('expires_at'):
            self.config['expires_at'] = result['expires_at']
            temporary = self.config_file.with_suffix('.renewing')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(self.config, f)
            os.replace(temporary, self.config_file)
        return result

    def agents(self):
        result = self.request('GET', '/api/agents')
        if not isinstance(result, list):
            raise RemoteError(502)
        return [{k: x.get(k) for k in ('id', 'name', 'description', 'model', 'runtime_id', 'status',
                 'runtime_bound', 'runtime_availability', 'system_key', 'permission_mode')} for x in result]

    def projects(self):
        return self.request('GET', '/api/projects').get('projects', [])

    def issue(self, issue_id):
        return self.request('GET', '/api/issues/' + issue_id)

    def create_issue(self, title, content, route, attachments):
        data = {'title': title, 'description': content, 'status': 'todo', 'priority': 'medium',
                'assignee_type': 'agent', 'assignee_id': route['agent_id'], 'attachment_ids': attachments,
                # Distinct batches can share a display title. Durable batch
                # intents and reconciliation provide request deduplication.
                'allow_duplicate': True}
        if route['project_id']:
            data['project_id'] = route['project_id']
        if route.get('parent_issue_id'):
            data['parent_issue_id'] = route['parent_issue_id']
        return self.request('POST', '/api/issues', data)

    def comment(self, issue_id, content, route, attachments):
        content = f"[@Channel agent](mention://agent/{route['agent_id']})\n\n" + content
        return self.request('POST', f'/api/issues/{issue_id}/comments', {'content': content, 'type': 'comment', 'attachment_ids': attachments})

    def comments(self, issue_id):
        return self.request('GET', f'/api/issues/{issue_id}/comments')

    def runs(self, issue_id):
        result = self.request('GET', f'/api/issues/{issue_id}/task-runs')
        if not isinstance(result, list):
            raise RemoteError(502)
        return result

    def transcript(self, task_id):
        result = self.request('GET', f'/api/tasks/{task_id}/messages')
        return result if isinstance(result, list) else []

    def upload(self, path, media_type, issue_id=None):
        with Path(path).open('rb') as f:
            result = self.request('POST', '/api/upload-file', {'issue_id': issue_id} if issue_id else None, files={'file': (Path(path).name + {'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','image/gif':'.gif'}.get(media_type,''), f, media_type)})
        if not isinstance(result, dict) or not result.get('id'):
            raise RemoteError(502)
        return result['id']

    def find_issue(self, marker, batch_id):
        # ListIssues does not apply q. Search descriptions, including closed
        # issues, so recovery is independent of a task's user-facing title.
        matches = []
        offset = 0
        while True:
            query = urlencode({'q': marker, 'limit': 50, 'offset': offset, 'include_closed': 'true'})
            result = self.request('GET', '/api/issues/search?' + query)
            if not isinstance(result, dict) or not isinstance(result.get('issues'), list):
                raise RemoteError(502)
            rows = result['issues']
            matches.extend(x for x in rows if marker in (x.get('description') or ''))
            if len({x['id'] for x in matches}) > 1:
                raise RemoteError(409)
            if len(rows) < 50:
                break
            offset += len(rows)
        return matches[0] if matches else None

    def issue_url(self, issue_id):
        return f'{self.public_url}/{self.slug}/issues/{issue_id}'


def extract_output(task, comments, transcript):
    result = task.get('result')
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        text = next((result[x] for x in ('content', 'text', 'output', 'message', 'summary') if isinstance(result.get(x), str)), '')
    else:
        text = ''
    if not text:
        matching = [x for x in comments if x.get('author_type') == 'agent' and
                    x.get('author_id') == task.get('agent_id') and x.get('source_task_id') == task['id']]
        text = '\n\n'.join(x.get('content', '') for x in matching)
    if not text:
        texts = [x for x in transcript if x.get('type') == 'text' and x.get('content')]
        if texts:
            text = max(texts, key=lambda x: x.get('seq', 0))['content']
    text = text.strip()[:100000]
    structured = None
    candidate = text
    if candidate.startswith('```'):
        lines = candidate.splitlines()
        candidate = '\n'.join(lines[1:-1])
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            structured = parsed
    except (ValueError, TypeError):
        pass
    return {'text': text, 'structured': structured, 'requires_human': bool((structured or {}).get('requires_human', True))}
