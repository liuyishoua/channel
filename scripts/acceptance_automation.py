"""Isolated browser + real HTTP connector acceptance; zero real platform calls.

Run with the existing browser environment:
../xianyu/.venv/bin/python scripts/acceptance_automation.py
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'sdk')]
from channel_service.store import Store
from xianyu_connector import Workbench, XianyuConnector


def wait_until(predicate, seconds=25):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(.2)
    raise AssertionError('验收等待超时')


def main():
    with tempfile.TemporaryDirectory(prefix='channel-automation-qa-') as directory:
        data = Path(directory)
        store = Store(data / 'channel')
        source = store.source('xianyu')
        config_file = data / 'source.json'
        config_file.write_text(json.dumps({'base_url': 'http://127.0.0.1:8870', 'source_id': 'xianyu', 'token': source['token']}))
        os.chmod(config_file, 0o600)
        channel_server = '''
import json,sys
from channel_service.app import create_app
from channel_service.demo import DemoMultica
from waitress import serve
class GoalModel(DemoMultica):
 def _run(self,issue_id,route,comment_id=None):
  super()._run(issue_id,route,comment_id)
  self.task_rows[issue_id][-1]['result']={'content':json.dumps({'summary':'合成咨询已处理','requires_human':False,'action':{'type':'dm.reply','text':'合成验收回复：示例商品支持包邮。','images':[]},'followup_after_seconds':300},ensure_ascii=False)}
app=create_app(sys.argv[1],GoalModel(),auth=False,background=True,demo=True)
serve(app,host='127.0.0.1',port=8870,threads=8)
'''
        processes = []
        connector = None
        try:
            processes.append(subprocess.Popen([str(ROOT / '.venv/bin/python'), '-c', channel_server, str(data / 'channel')], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            processes.append(subprocess.Popen([str(ROOT.parent / 'xianyu/.venv/bin/python'), '-m', 'web', '--demo', '--port', '8871', '--public-origin', 'https://workspace.test', '--data-dir', str(data / 'workbench')], cwd=ROOT.parent / 'xianyu', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for port in (8870, 8871):
                def ready():
                    try:
                        return urllib.request.urlopen(f'http://127.0.0.1:{port}/login', timeout=1).status == 200
                    except OSError:
                        return False
                wait_until(ready)
            config = dict(account_id='demo-seller', executor_id='acceptance-local', workbench_url='http://127.0.0.1:8871',
                workbench_origin='https://workspace.test', password_file=str(data / 'workbench/admin-password'),
                channel_config=str(config_file), journal=str(data / 'connector.sqlite3'), execute=True)
            web = Workbench(config)
            web.login()
            connector = XianyuConnector(config, web)
            connector.step()  # Establish a baseline; historical conversations are not replayed.
            out = ROOT / 'docs/previews'
            out.mkdir(exist_ok=True)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:8870/automation')
                page.get_by_role('button', name='新建业务目标 ＋').click()
                form = page.locator('#goal-form')
                form.locator('[name=name]').fill('合成客服与跟进验收')
                form.locator('[name=account_id]').fill('demo-seller')
                form.locator('[name=objective]').fill('仅用于离线验收，根据虚构商品事实回答问题。')
                form.locator('[name=enabled]').select_option('true')
                form.locator('summary').click()
                form.locator('[name=hour_start]').fill('0')
                form.locator('[name=hour_end]').fill('24')
                form.locator('button.primary').click()
                page.locator('dialog').wait_for(state='hidden')
                page.get_by_role('heading', name='合成客服与跟进验收').wait_for()
                page.screenshot(path=str(out / 'automation-goals.png'), full_page=True)

                def incoming(message_id, body):
                    # Insert a synthetic platform observation into the temporary demo DB.
                    message = dict(id=message_id, cid='900000', sender_id='800000', outgoing=False, kind='text', text=body, images=[], timestamp=time.time())
                    with sqlite3.connect(data / 'workbench/workspace.sqlite3') as db:
                        db.execute('INSERT INTO documents VALUES(?,?,?,?,?)', ('demo-seller', 'message', message_id, json.dumps(message, ensure_ascii=False), time.time()))
                    connector.step()

                incoming('qa-incoming-1', '示例商品可以包邮吗？')
                page.locator('[data-tab=actions]').click()
                page.get_by_role('button', name='采用执行', exact=True).wait_for(timeout=25000)
                page.get_by_role('button', name='采用执行', exact=True).click()

                def sent():
                    connector.step()
                    with sqlite3.connect(data / 'channel/channel.sqlite3') as db:
                        return db.execute("SELECT count(*) FROM account_actions WHERE state='succeeded'").fetchone()[0] == 1
                wait_until(sent)
                page.locator('#refresh').click()
                page.get_by_text('平台已确认', exact=True).wait_for()
                page.screenshot(path=str(out / 'automation-actions.png'), full_page=True)
                # Advance only the temporary timer; no real-time waiting or live follow-up.
                with sqlite3.connect(data / 'channel/channel.sqlite3') as db:
                    db.execute('UPDATE followup_timers SET due=0')
                page.get_by_role('button', name='采用执行', exact=True).wait_for(timeout=25000)
                incoming('qa-stop', '不要再联系我了')
                page.locator('[data-tab=cases]').click()
                page.locator('#refresh').click()
                page.get_by_text('已停止联系', exact=True).wait_for()
                page.locator('[data-tab=connectors]').click()
                page.get_by_text('在线', exact=True).wait_for()
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('[data-tab=goals]').click()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(out / 'automation-mobile.png'), full_page=True)
                assert not errors, errors
                browser.close()
            with sqlite3.connect(data / 'channel/channel.sqlite3') as db:
                assert db.execute("SELECT count(*) FROM account_actions WHERE state='succeeded'").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM business_events WHERE json_extract(payload,'$.event_type')='dm.followup_due'").fetchone()[0] == 1
                assert db.execute("SELECT count(DISTINCT issue_id) FROM batches WHERE issue_id IS NOT NULL").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM goal_cases WHERE state='stopped'").fetchone()[0] == 1
            report = dict(passed=True, real_platform_calls=False, browser_errors=errors, secure_workbench_session=True,
                flows=['goal creation', 'baseline skips history', 'local message observation', 'source API event ingestion',
                       'model plan and human adoption', 'account claim', 'demo platform send and exact receipt',
                       'follow-up timer and same-task append', 'stop-contact cancellation', 'mobile layout'])
            (ROOT / 'docs/automation-browser-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            if connector:
                connector.db.close()
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    main()
