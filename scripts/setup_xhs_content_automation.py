"""Install the dedicated publishing skill, agent and paused native automation.

No platform credential enters an agent task. Existing chat agents/routes stay
unchanged. Re-running updates only objects recorded by this installer.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'channel'))
from channel_service.multica import Multica
from channel_service.store import Store

HOME = ROOT / 'agent-tools/xhs-lifestyle-publisher'
CONFIG = ROOT / 'xhs/.local/content-automation.json'


def write_config(value):
    temp = CONFIG.with_suffix('.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(temp, CONFIG)


def main():
    p = argparse.ArgumentParser(); p.add_argument('--execute', action='store_true')
    args = p.parse_args()
    if not args.execute:
        p.error('--execute required')
    remote = Multica(ROOT / 'channel/.local/multica.json')
    store = Store(ROOT / 'channel/.local')
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {
        'enabled': False, 'pc_account_id': 2, 'creator_account_id': 3,
        'expected_external_user_id': '60d73b36000000002002dc93',
        'user_id': 1, 'timezone': 'Asia/Shanghai', 'comment_poll_seconds': 60,
        'publish_limit': 2, 'started_at': '',
    }
    content = (HOME / 'SKILL.md').read_text()
    body = content.split('---', 2)[2].strip()
    files = [{'path': str(f.relative_to(HOME)), 'content': f.read_text()}
             for f in (HOME / 'references').glob('*.md')]
    payload = {'name': 'xhs-lifestyle-publisher', 'description': '小桃生活笔记的选题、真实照片、图文验收与评论回复',
               'content': content, 'files': files}
    if config.get('skill_id'):
        remote.request('PUT', '/api/skills/' + config['skill_id'], payload)
    else:
        config['skill_id'] = remote.request('POST', '/api/skills', payload)['id']
        write_config(config)
    instructions = '''你是小桃先吃一口的内容编辑，按任务mode完成post/review/comment。当前任务含完整规范时直接使用，不重复加载主文件。需要内部输入时允许读取当前分配的issue和触发评论、当前附件；这不属于对小红书的平台操作。不要以尚未读取任务为由声称缺少上下文。可访问公开互联网研究选题。禁止读取账号凭据、直接发帖或评论、创建额外任务或定时任务；平台写入由工作台执行。必须逐张查看review附件，不能只读标签。最终仅返回当前mode的JSON，不发过程报告。没有可用事实就hold，不伪造本人生活经历。\n\n''' + body
    if config.get('agent_id'):
        remote.request('PUT', '/api/agents/' + config['agent_id'], {'instructions': instructions})
    else:
        template = remote.request('GET', '/api/agents/82ce74c8-e570-4f8f-9b07-dc3fa59e567c')
        config['agent_id'] = remote.request('POST', '/api/agents', {
            'name': '小桃生活内容编辑', 'description': '生活选题、真实摄影配图、发布验收及公开评论',
            'instructions': instructions, 'runtime_id': template['runtime_id'],
            'runtime_config': template.get('runtime_config'), 'model': 'gpt-6.1-sol',
            'thinking_level': 'medium', 'max_concurrent_tasks': 2,
            'visibility': 'workspace', 'skill_ids': [config['skill_id']],
        })['id']
        write_config(config)
    route = store.save_route({
        'name': '小桃内容发布与评论验收', 'agent_id': config['agent_id'], 'sources': ['xhs'],
        'categories': ['lifestyle_editor'], 'surfaces': ['manual', 'comment', 'content'],
        'event_types': ['manual.request'], 'grouping': 'message', 'batch_seconds': 0,
        'max_batch': 1, 'timeout_seconds': 900, 'instruction': body,
    }, config.get('route_id'))
    config['route_id'] = route['id']; write_config(config)
    description = '''mode=post。为小桃先吃一口写一篇生活笔记候选稿。先搜索互联网了解普通人当前讨论的小问题，再写原创内容。每天另有一篇，主题不要局限一个领域。网页研究必须实际访问并在sources中记录，素材优先真实摄影，照片2–6张。Pexels候选请提供真实作品页面和数字ID，工作台会下载后让你另跑一次看图验收，验收通过才发布。不要访问小红书凭据或执行发布。不得虚构亲测、购买、本人住址、身份、摄影或经验。不要复制其他笔记文字/图片。返回post契约JSON。\n\n''' + body
    if config.get('autopilot_id'):
        existing = remote.request('GET', '/api/autopilots/' + config['autopilot_id'])['autopilot']
        marker = '\n\n近期已发布记录（工作台提供）：'
        if marker in (existing.get('description') or ''):
            description += marker + existing['description'].split(marker, 1)[1]
    payload = {'title': '小桃｜每日随机两帖＋评论回复', 'description': description,
               'assignee_id': config['agent_id'], 'execution_mode': 'create_issue',
               'issue_title_template': '小桃生活笔记候选 · {{date}}'}
    if config.get('autopilot_id'):
        remote.request('PATCH', '/api/autopilots/' + config['autopilot_id'], payload)
    else:
        config['autopilot_id'] = remote.request('POST', '/api/autopilots', payload)['id']
        remote.request('PATCH', '/api/autopilots/' + config['autopilot_id'], {'status': 'paused'})
        write_config(config)
    print(json.dumps({k: config[k] for k in ('agent_id', 'skill_id', 'route_id', 'autopilot_id')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
