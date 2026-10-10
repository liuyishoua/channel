import json
import threading
import uuid
from .multica import RemoteError

AGENT_ID = '11111111-1111-4111-8111-111111111111'


class DemoMultica:
    public_url = 'https://d.58nft.net:9447'
    workspace = 'demo'
    slug = 'leo'

    def __init__(self):
        self.issues = {}
        self.comment_rows = {}
        self.task_rows = {}
        self.calls = []
        self.lock = threading.RLock()
        self.drop_create_response = False
        self.drop_comment_response = False
        self.auto_complete = True

    def agents(self):
        return [{'id': AGENT_ID, 'name': '离线验收助手', 'model': 'demo', 'status': 'idle'}]

    def projects(self):
        return []

    def issue(self, issue_id):
        return dict(self.issues[issue_id])

    def _run(self, issue_id, route, comment_id=None):
        task = {'id': str(uuid.uuid4()), 'agent_id': route['agent_id'], 'status': 'completed' if self.auto_complete else 'running',
                'result': {'content': json.dumps({'reply': '这是一条离线验收建议，没有调用真实模型。', 'summary': '已读取上下文', 'labels': ['演示'], 'requires_human': True}, ensure_ascii=False)},
                'trigger_comment_id': comment_id, 'created_at': str(len(self.task_rows.get(issue_id, [])))}
        self.task_rows.setdefault(issue_id, []).append(task)

    def create_issue(self, title, content, route, attachments):
        with self.lock:
            issue = {'id': str(uuid.uuid4()), 'title': title, 'description': content, 'assignee_type': 'agent', 'assignee_id': route['agent_id'], 'status': 'in_progress'}
            self.issues[issue['id']] = issue
            self.calls.append(('create', issue['id'], attachments))
            self._run(issue['id'], route)
            if self.drop_create_response:
                self.drop_create_response = False
                raise RemoteError(0, True)
            return dict(issue)

    def comment(self, issue_id, content, route, attachments):
        with self.lock:
            comment = {'id': str(uuid.uuid4()), 'content': content}
            self.comment_rows.setdefault(issue_id, []).append(comment)
            self.calls.append(('append', issue_id, attachments))
            self._run(issue_id, route, comment['id'])
            if self.drop_comment_response:
                self.drop_comment_response = False
                raise RemoteError(0, True)
            return dict(comment)

    def comments(self, issue_id):
        return list(self.comment_rows.get(issue_id, []))

    def runs(self, issue_id):
        return list(self.task_rows.get(issue_id, []))

    def transcript(self, task_id):
        return []

    def upload(self, path, media_type, issue_id=None):
        self.calls.append(('upload', media_type, issue_id))
        return str(uuid.uuid4())

    def find_issue(self, marker, batch_id):
        return next((dict(x) for x in self.issues.values() if marker in x['description']), None)

    def issue_url(self, issue_id):
        return self.public_url + '/leo/issues/' + issue_id
