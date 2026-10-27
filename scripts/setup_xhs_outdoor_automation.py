"""Prepare an isolated persona and paused tasks; bind only verified account IDs."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'channel'))
from channel_service.multica import Multica
sys.path.insert(0, str(ROOT / 'agent-tools/leo-project-ops/scripts'))
from leo_api import api as workbench_api

CONFIG = ROOT / 'xhs/.local/outdoor-automation.json'
HOME = ROOT / 'agent-tools/xhs-outdoor-fitness'
TITLE = '户外运动号每天随机发两篇'
PERSONA = ('当前账号为户外健身运动、御姐仙气审美的成年女性生活账号，普通上班族、热爱生活。'
           '说话利落温和、有边界，能接梗，不默认撒娇暧昧，不自称小桃。'
           '不编职业公司、城市、年龄、身材、训练成绩、本人安排或私人经历；不代主人承诺见面。'
           'account_nickname 以当前任务为准。')


def save(config):
    temporary = CONFIG.with_suffix('.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(temporary, CONFIG)


def unique(items, field, value):
    found = [item for item in items if item.get(field) == value]
    if len(found) > 1:
        raise RuntimeError('发现同名对象，请先核对，未继续写入：' + value)
    return found[0] if found else None


def channel_api(method, path, payload=None):
    result = workbench_api('channel', path, method, payload)
    if not 200 <= result['status'] < 300:
        raise RuntimeError('Channel 操作未完成，HTTP ' + str(result['status']))
    return result['data']


def main():
    raise SystemExit('This legacy setup entry is retired. Use account identity agents and the maintained capability registry; do not recreate the unverified outdoor agents.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--pc-account-id', type=int)
    parser.add_argument('--creator-account-id', type=int)
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        parser.error('--execute required')
    if args.activate and not (args.pc_account_id and args.creator_account_id):
        parser.error('启用前须提供手机确认后的 PC 与 Creator 账号 ID')
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {
        'enabled': False, 'publishing_only': True, 'user_id': 1,
        'pc_account_id': None, 'creator_account_id': None,
        'expected_external_user_id': '', 'account_nickname': '户外运动号（待验证）',
        'timezone': 'Asia/Shanghai', 'publish_limit': 2, 'started_at': '',
        'login_hint': 'QQ 1813120647@qq.com；仅用于主人辨认，不能证明小红书身份',
        'setup_state': 'awaiting_account_verification',
    }
    remote = Multica(ROOT / 'channel/.local/multica.json')
    content = (HOME / 'SKILL.md').read_text()
    body = content.split('---', 2)[2].strip()
    natural = (ROOT / 'agent-tools/xhs-natural-expression/SKILL.md').read_text().split('---', 2)[2].strip()
    skill = (remote.request('GET', '/api/skills/' + config['skill_id']) if config.get('skill_id')
             else unique(remote.request('GET', '/api/skills'), 'name', 'xhs-outdoor-fitness'))
    payload = {'name': 'xhs-outdoor-fitness', 'description': '户外健身上班族的自然生活创作、看图验收与评论私信',
               'content': content, 'files': [{'path': str(p.relative_to(HOME)), 'content': p.read_text()}
                                          for p in (HOME / 'references').glob('*.md')]}
    # A lost creation receipt is reconciled by the unique name on the next run.
    if skill:
        remote.request('PUT', '/api/skills/' + skill['id'], payload)
    else:
        config['pending_create'] = 'skill:xhs-outdoor-fitness'; save(config)
        skill = remote.request('POST', '/api/skills', payload)
    config['skill_id'] = skill['id']; config.pop('pending_create', None); save(config)
    common = ('先读取当前分配任务及本轮触发评论/附件；内部输入读取不属于对外社交操作。'
              '当前任务含完整规范时不重复加载文件。禁止读取凭据或直接操作平台；'
              '只返回当前mode的JSON，不写开场白和过程报告。\n\n')
    agents = remote.agents()
    content_template = remote.request('GET', '/api/agents/05f361ac-72f8-456a-9a89-31b31db57dff')
    dm_template = remote.request('GET', '/api/agents/da72f33e-cac4-4ff0-a99d-d415219f3efc')
    for role, name, template, instructions, skills in [
        ('content', '户外运动号内容与评论', content_template, common + '按 post/review/comment 模式执行。\n\n' + body, [skill['id']]),
        ('dm', '户外运动号自然私信', dm_template, common + 'mode=dm。\n' + PERSONA + '\n\n' + natural + '\n\n' + body,
         [skill['id'], '47cd3abc-32bf-4d7a-bf8d-ae617beda872', '2df12564-19b6-4fa1-9f29-96a2547e2899']),
    ]:
        agent = (remote.request('GET', '/api/agents/' + config[role + '_agent_id'])
                 if config.get(role + '_agent_id') else unique(agents, 'name', name))
        if agent:
            remote.request('PUT', '/api/agents/' + agent['id'], {'instructions': instructions})
        else:
            config['pending_create'] = 'agent:' + name; save(config)
            agent = remote.request('POST', '/api/agents', {
                'name': name, 'description': PERSONA, 'instructions': instructions,
                'runtime_id': template['runtime_id'], 'runtime_config': template.get('runtime_config', {}),
                'model': template['model'], 'thinking_level': template.get('thinking_level') or 'low',
                'max_concurrent_tasks': template.get('max_concurrent_tasks', 2),
                'visibility': 'workspace', 'skill_ids': skills,
            })
        config[role + '_agent_id'] = agent['id']; config.pop('pending_create', None); save(config)
        route_name = name + '专属路由'
        old = unique(channel_api('GET', '/api/routes')['items'], 'name', route_name)
        route = channel_api('PUT' if old else 'POST', '/api/routes' + ('/' + old['id'] if old else ''), {
            'name': route_name, 'agent_id': agent['id'], 'sources': ['xhs'],
            'categories': ['reply_draft'] if role == 'dm' else ['lifestyle_editor'],
            'surfaces': ['dm'] if role == 'dm' else ['manual', 'comment', 'content'],
            'event_types': ['dm.received'] if role == 'dm' else ['manual.request'],
            'grouping': 'conversation' if role == 'dm' else 'message',
            'batch_seconds': 2 if role == 'dm' else 0, 'max_batch': 1,
            'timeout_seconds': 900,
            'instruction': PERSONA + '\n\n' + natural if role == 'dm' else body,
        })
        config[role + '_route_id'] = route['id']; save(config)
    config['agent_id'] = config['content_agent_id']; config['route_id'] = config['content_route_id']; save(config)
    autopilot = (remote.request('GET', '/api/autopilots/' + config['autopilot_id'])['autopilot']
                 if config.get('autopilot_id') else unique(remote.request('GET', '/api/autopilots')['autopilots'], 'title', TITLE))
    if not autopilot:
        config['pending_create'] = 'autopilot:' + TITLE; save(config)
        autopilot = remote.request('POST', '/api/autopilots', {
            'title': TITLE,
            'description': ('每天上午、晚上各一个随机时段，mode=post。按 xhs-outdoor-fitness 创建一篇原创生活图文。'
                            '两篇主题不同：运动/户外与通勤/休息/生活相搭配，不代表一天训练两次。'
                            '先读普通人真实公开日常，选2–6张有使用权的真实照片，遵循人设和post JSON契约。'
                            '没有本人素材不得编本人训练/自拍。账号绑定、审稿和发布由工作台处理。'),
            'assignee_id': config['content_agent_id'], 'execution_mode': 'create_issue',
            'issue_title_template': '户外运动生活候选 · {{date}}',
        })
        remote.request('PATCH', '/api/autopilots/' + autopilot['id'], {'status': 'paused'})
    config['autopilot_id'] = autopilot['id']; config.pop('pending_create', None); save(config)
    if args.activate:
        os.environ.setdefault('CONFIG_FILE', str(ROOT / 'xhs/.local/runtime.yaml'))
        sys.path.insert(0, str(ROOT / 'xhs'))
        import requests
        from backend.app.core.security import create_access_token
        from backend.app.services.content_automation import now
        session = requests.Session(); session.trust_env = False
        session.headers['Authorization'] = 'Bearer ' + create_access_token(config['user_id'])
        def api(method, path, payload=None):
            response = session.request(method, 'http://127.0.0.1:8772/api' + path, json=payload, timeout=60)
            response.raise_for_status()
            return response.json()
        pc = api('POST', '/accounts/' + str(args.pc_account_id) + '/check')
        creator = api('POST', '/accounts/' + str(args.creator_account_id) + '/check')
        if not (pc['status'] == creator['status'] == 'active' and pc['sub_type'] == 'pc' and creator['sub_type'] == 'creator'
                and pc['external_user_id'] and pc['external_user_id'] == creator['external_user_id']):
            raise RuntimeError('PC 与 Creator 未通过同一身份的有效登录检查，未启用任务')
        existing = json.loads((ROOT / 'xhs/.local/content-automation.json').read_text())
        if pc['external_user_id'] == existing['expected_external_user_id']:
            raise RuntimeError('这是小桃账号，不能作为新户外账号绑定')
        if config.get('expected_external_user_id') and config['expected_external_user_id'] != pc['external_user_id']:
            raise RuntimeError('已绑定身份不允许自动更换')
        config.update(pc_account_id=pc['id'], creator_account_id=creator['id'],
                      expected_external_user_id=pc['external_user_id'], account_nickname=pc['nickname'])
        save(config)
        api('PUT', '/xhs/direct-messages/listeners/' + str(pc['id']), {
            'enabled': True, 'auto_reply': True, 'control_mode': 'full_access', 'route_id': config['dm_route_id']})
        api('PUT', '/xhs/comments/listeners/' + str(pc['id']), {
            'enabled': True, 'auto_reply': True, 'control_mode': 'full_access', 'route_id': config['content_route_id']})
        config.update(enabled=True, started_at=config.get('started_at') or now().isoformat(), setup_state='active')
        save(config)
        remote.request('PATCH', '/api/autopilots/' + config['autopilot_id'], {'status': 'active'})
    skill_check = remote.request('GET', '/api/skills/' + config['skill_id'])
    assert skill_check['content'] == content
    print(json.dumps({k: config[k] for k in ('setup_state', 'skill_id', 'content_agent_id', 'dm_agent_id',
                                             'content_route_id', 'dm_route_id', 'autopilot_id', 'enabled')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
