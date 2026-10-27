import unittest

from channel_service.multica import Multica, RemoteError


class DuplicateBatchTests(unittest.TestCase):
    def test_duplicate_error_explains_cause_without_echoing_remote_data(self):
        self.assertIn('任务标题', str(RemoteError(409, code='active_duplicate_issue')))
        self.assertEqual(str(RemoteError(409, code='untrusted secret')), 'Multica 返回 HTTP 409')

    def test_independent_batches_with_same_title_create_distinct_issues(self):
        class GuardedRemote(Multica):
            def __init__(self):
                self.created = []

            def request(self, method, path, data=None):
                if any(row['title'] == data['title'] for row in self.created) and not data.get('allow_duplicate'):
                    raise RemoteError(409)
                self.created.append(data)
                return {'id': str(len(self.created))}

        remote = GuardedRemote()
        route = {'agent_id': 'synthetic-agent', 'project_id': 'owned-project', 'parent_issue_id': 'owned-parent'}
        first = remote.create_issue('图文审核', '<!-- channel-batch:first -->', route, ['photo-a'])
        second = remote.create_issue('图文审核', '<!-- channel-batch:second -->', route, ['photo-b'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(remote.created[1]['attachment_ids'], ['photo-b'])
        self.assertEqual(remote.created[1]['project_id'], 'owned-project')
        self.assertEqual(remote.created[1]['parent_issue_id'], 'owned-parent')
        self.assertIn('channel-batch:second', remote.created[1]['description'])
