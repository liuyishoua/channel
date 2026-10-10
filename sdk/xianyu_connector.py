"""Opt-in local connector: observe saved inbound DMs and execute text replies.

No account cookies enter Channel. HTTP side effects use the existing workbench
and its durable request_id. A local journal persists claims and receipts before
network calls. Uncertain writes are never automatically sent again.
"""
import argparse
import hashlib
import http.cookiejar
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from channel_client import ChannelClient, ChannelError, NoRedirect

log = logging.getLogger('channel.xianyu')


class Workbench:
    def __init__(self, config):
        self.base = config['workbench_url'].rstrip('/')
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('账号连接器只连接固定的本机工作台')
        self.password_file = Path(config['password_file'])
        self.expected_account = str(config['account_id'])
        self.origin = config['workbench_origin'].rstrip('/')
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, data=None):
        headers = {'Origin': self.origin, 'X-App-Request': '1'}
        if path != '/api/login':
            headers['X-Execution-Account'] = self.expected_account
        # Match the existing local operations CLI: browser cookies stay Secure;
        # only this fixed loopback transport explicitly supplies its session.
        if self.cookies:
            headers['Cookie'] = '; '.join(cookie.name + '=' + cookie.value for cookie in self.cookies)
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path, body, headers, method='POST' if data is not None else 'GET')
        try:
            with self.opener.open(request, timeout=25) as response:
                return json.loads(response.read(4 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            raise ValueError('工作台请求失败 HTTP ' + str(exc.code)) from None

    def login(self):
        return self.request('/api/login', {'password': self.password_file.read_text().strip()})

    def identity(self):
        value = self.request('/api/account')['account']
        if not value or not value.get('id'):
            raise ValueError('工作台尚无已连接账号')
        return str(value['id'])


class XianyuConnector:
    def __init__(self, config, workbench=None, channel=None):
        self.config = config
        self.account = str(config['account_id'])
        self.executor_id = config.get('executor_id', 'xianyu-local-v1')
        self.web = workbench or Workbench(config)
        self.channel = channel or ChannelClient(config['channel_config'])
        path = Path(config['journal'])
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS seen(account TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(account,id));
            CREATE TABLE IF NOT EXISTS baseline(account TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY,action TEXT NOT NULL,job_id TEXT,state TEXT NOT NULL,receipt TEXT);
            CREATE TABLE IF NOT EXISTS observations(account TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(account,id));
        ''')
        os.chmod(path, 0o600)
        self.last_heartbeat = 0
        self.last_sync = 0
        self.sync_job = None

    def verify(self):
        if self.web.identity() != self.account:
            raise ValueError('工作台当前账号与连接器配置不一致，已停止处理')

    def observe(self):
        self.verify()
        baseline = bool(self.db.execute('SELECT 1 FROM baseline WHERE account=?', (self.account,)).fetchone())
        # No broad network synchronization: consume the workbench's saved messages.
        conversations = self.web.request('/api/conversations')['items']
        for conversation in conversations:
            cid = str(conversation['id'])
            messages = self.web.request('/api/conversations/' + urllib.parse.quote(cid, safe='') + '/messages')['items']
            for message in messages:
                if message.get('outgoing'):
                    continue
                platform_id = str(message.get('platform_id') or message.get('id') or '')
                if not platform_id:
                    continue
                key = cid + ':' + platform_id
                if self.db.execute('SELECT 1 FROM seen WHERE account=? AND id=?', (self.account, key)).fetchone():
                    continue
                if baseline:
                    unavailable = bool(message.get('images')) or message.get('kind') not in ('text', None)
                    payload = dict(account_id=self.account, event_id='dm-' + hashlib.sha256(key.encode()).hexdigest(),
                        event_type='dm.received', conversation_id=cid, transport='poll',
                        text=message.get('text') or '[对方发来非文字内容，请人工查看]',
                        context={'peer_id': str(conversation.get('peer_id') or ''), 'media_unavailable': unavailable})
                    self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?)', (self.account, key, json.dumps(payload, ensure_ascii=False)))
                self.db.execute('INSERT OR IGNORE INTO seen VALUES(?,?)', (self.account, key))
        self.db.execute('INSERT OR IGNORE INTO baseline VALUES(?)', (self.account,))
        self.db.commit()
        for observation in self.db.execute('SELECT * FROM observations WHERE account=? ORDER BY rowid LIMIT 100', (self.account,)).fetchall():
            self.channel.event(**json.loads(observation['payload']))
            self.db.execute('DELETE FROM observations WHERE account=? AND id=?', (self.account, observation['id']))
            self.db.commit()

    def step(self):
        self.verify()
        if time.time() - self.last_heartbeat >= 30:
            self.channel.heartbeat(self.account, self.executor_id, ['dm.reply'] if self.config.get('execute') else [])
            self.last_heartbeat = time.time()
        if self.config.get('sync_recent'):
            if self.sync_job:
                job = self.web.request('/api/jobs/' + urllib.parse.quote(self.sync_job, safe=''))
                if job['status'] not in ('pending', 'running'):
                    self.sync_job = None
            interval = max(30, int(self.config.get('sync_interval_seconds', 60)))
            if not self.sync_job and time.time() - self.last_sync >= interval:
                request_id = 'channel-sync-' + str(int(time.time() // interval))
                self.sync_job = self.web.request('/api/conversations/sync', {'request_id': request_id, 'expected_account_id': self.account})['id']
                self.last_sync = time.time()
        self.observe()
        self.recover()
        if not self.config.get('execute'):
            return
        if self.db.execute('SELECT 1 FROM pending LIMIT 1').fetchone():
            return
        action = self.channel.claim_action(self.account, self.executor_id)
        if not action:
            return
        # Commit before the potentially irreversible call.
        self.db.execute('INSERT INTO pending VALUES(?,?,?,?,?)', (action['id'], json.dumps(action), None, 'intent', None))
        self.db.commit()
        self.execute(action)

    def execute(self, action):
        try:
            self.verify()
            if action['source'] != 'xianyu' or action['account'] != self.account or action['type'] != 'dm.reply' or not action['target'].startswith('dm:') or action['payload'].get('images') or not action['payload'].get('text'):
                self.save_receipt(action, 'failed', detail='连接器不支持此动作或账号不一致；尚未发送')
                return
            cid = action['target'][3:]
            result = self.web.request('/api/conversations/' + urllib.parse.quote(cid, safe='') + '/send', {'text': action['payload']['text'], 'request_id': 'channel-' + action['id'], 'expected_account_id': self.account})
            if not result.get('id'):
                raise ValueError('未获得发送任务标识')
            self.db.execute("UPDATE pending SET job_id=?,state='waiting' WHERE id=?", (result['id'], action['id']))
            self.db.commit()
        except Exception:
            self.save_receipt(action, 'uncertain', detail='工作台发送响应未确认；请核对原发送任务，不自动重发')

    def save_receipt(self, action, status, external_id='', detail=''):
        value = dict(status=status, external_id=external_id, detail=detail)
        self.db.execute("UPDATE pending SET receipt=?,state='receipt' WHERE id=?", (json.dumps(value), action['id']))
        self.db.commit()

    def recover(self):
        for row in self.db.execute('SELECT * FROM pending').fetchall():
            action = json.loads(row['action'])
            if row['state'] == 'intent':
                self.save_receipt(action, 'uncertain', detail='连接器在发送期间重启；请核对原动作，不自动重发')
            elif row['state'] in ('waiting', 'uncertain'):
                try:
                    if row['job_id']:
                        job = self.web.request('/api/jobs/' + urllib.parse.quote(row['job_id'], safe=''))
                    else:
                        job = self.web.request('/api/operations/' + urllib.parse.quote('channel-' + action['id'], safe=''))
                        self.db.execute('UPDATE pending SET job_id=? WHERE id=?', (job['id'], action['id']))
                        self.db.commit()
                except Exception:
                    continue
                if job['status'] == 'completed':
                    external = str((job.get('result') or {}).get('message_id') or '')
                    self.save_receipt(action, 'succeeded' if external else 'uncertain', external, '平台接口确认' if external else '工作台完成，但缺少平台消息标识')
                elif job['status'] in ('unknown', 'failed') and row['state'] != 'uncertain':
                    self.save_receipt(action, 'uncertain', detail='工作台发送未获明确平台确认；请核对原任务')
            fresh = self.db.execute('SELECT receipt FROM pending WHERE id=?', (row['id'],)).fetchone()
            if fresh and fresh['receipt']:
                receipt = json.loads(fresh['receipt'])
                self.channel.action_receipt(self.account, action, **receipt)
                if receipt['status'] == 'uncertain':
                    # Retain the original lease for late reconciliation. Never
                    # lose the only credential that can confirm this action.
                    self.db.execute("UPDATE pending SET state='uncertain',receipt=NULL WHERE id=?", (row['id'],))
                else:
                    self.db.execute('DELETE FROM pending WHERE id=?', (row['id'],))
                self.db.commit()


def main():
    parser = argparse.ArgumentParser(description='Opt-in local XianYu account connector')
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = json.loads(args.config.read_text())
    if config.get('enabled') is not True:
        raise SystemExit('连接器未启用；先配置执行账号、目标和运行模式')
    connector = XianyuConnector(config)
    connector.web.login()
    try:
        while True:
            try:
                connector.step()
            except (ChannelError, ValueError, OSError) as exc:
                log.warning('Connector step deferred: %s', type(exc).__name__)
                # Sessions may expire; the following cycle always rechecks account identity.
                try:
                    connector.web.login()
                except Exception:
                    pass
            time.sleep(5)
    finally:
        connector.db.close()


if __name__ == '__main__':
    main()
