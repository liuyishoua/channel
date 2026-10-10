"""Publish reviewed local chat skills and focused agent/route instructions.

Only the named skills, agent instructions and XHS DM route are changed. Existing
route snapshots, model/runtime, concurrency and skill bindings stay intact.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from channel_service.multica import Multica
from channel_service.store import Store
from fix_dm_task_reading import BOOTSTRAP

AGENT_ID = 'da72f33e-cac4-4ff0-a99d-d415219f3efc'
ROUTE_ID = '978ddec8-b2f4-4ac5-9580-9abf16f4c96d'
SKILLS = {'xhs-natural-expression': '47cd3abc-32bf-4d7a-bf8d-ae617beda872',
          'gifgrep': '2df12564-19b6-4fa1-9f29-96a2547e2899'}

INSTRUCTIONS = '''你为账号主人处理小红书一对一私聊。参与眼前的话题，有自己的反应，按当前亲疏程度说话。不要把所有聊天默认成恋爱、安慰或客服。

每轮读取当前分配任务。任务的“本轮表达规范”包含 xhs-natural-expression 当前主体时，视为已加载该 Skill，不重复读取文件；没有该规范才从运行时实际挂载路径读取 SKILL.md。首次工具调用可批量读取当前任务、必需技能和运行时要求的评论摘要，避免每个文件一轮调用。参考案例只在判断困难时读取；研究资料无需每轮读。只有本轮确有双方浪漫语境才读 lady-s-man；普通关心、游戏邀请和吃饭闲聊不因此进入恋爱话术。已有真实图片 catalog 时直接看图选择；需要 GIF 搜索/本地检查时才读 gifgrep 及 runtime 约束，不反复搜索已有素材。只回文字时不必下载候选图；选图时才实际查看。

以 xhs-natural-expression 为交流方式的唯一主规范。context.messages 是本轮双向快照，注意谁发了哪句以及哪些已实际发送。dialogue_state 标记已确认自动生成内容；这些是对话事实，不能证明主人喜欢那种说法。没有标记也不保证人工。previous_analysis 是待核对观察。不要从上一轮模型尾巴循环学习“哈哈、～、颜文字”。本人可用时间、游戏上线、私人经历等没有事实就不能编造或承诺。

任务里的 context.media_catalog 是可发送的账号素材；source=reaction_library 可以主动用来接话，不要求对方先发图。任务 Markdown URL 与下载成功都不等于看过画面。需要选图时用 `multica attachment download <id> -o ./attachments` 批量获取当前附件，再用运行时 view_image 图像工具查看选中的画面；不能只按标签猜。kind=sticker 返回 type=sticker，原生 emoji 返回 type=emoji；普通照片才 type=image。无合适图或无法实际查看就文字。只选有 image_number 的真实 asset_id，素材故障不阻止普通文字决策。

遵从本轮任务 JSON 契约和 context.control_mode。request_approval 仍生成最终消息，由系统在发送前请主人批准；auto_approve 自主判断普通聊天；full_access 仅 reply/no_reply，requires_human=false。只有 handoff 才 requires_human=true。分析字段简短，只供内部保存，不发报告或候选答案。

执行时不输出开场白、进度或工具解释，直接在最终回答返回 JSON。实际账号、收件人、消息间隔、停止和平台回执由工作台负责。

最终字段名严格使用 decision，不用 action 替代：{"decision":"reply|no_reply|handoff","reply_messages":[],"summary":"简短依据","intent":"当前动作","style":"有依据的观察","relationship_stage":"","expression_memory":"","requires_human":false}。reply 时数组为1至4条最终消息；no_reply/handoff 为空。文字气泡用 type/text，媒体用 type/asset_id，间隔 delay_seconds 为0至15。

''' + BOOTSTRAP

ROUTE_INSTRUCTION = ('按 xhs-natural-expression 处理当前私聊交流动作，使用本轮双向快照和真实附件。'
    '已确认自动生成历史只作对话事实，不作本人风格正例。'
    '账号反应图库可主动选用，图片必须本轮实际读取；不要为已有素材执行在线搜索。'
    '本轮确有双方浪漫语境才使用 lady-s-man。按本轮 JSON 契约直接返回最终消息计划；发送由工作台负责。')


def main():
    p = argparse.ArgumentParser(); p.add_argument('--execute', action='store_true'); args = p.parse_args()
    if not args.execute:
        p.error('--execute required')
    remote = Multica(ROOT / '.local/multica.json'); store = Store(ROOT / '.local')
    agent = remote.request('GET', '/api/agents/' + AGENT_ID)
    route = next(r for r in store.routes() if r['id'] == ROUTE_ID)
    old_skills = {name: remote.request('GET', '/api/skills/' + sid) for name, sid in SKILLS.items()}
    natural_content = (ROOT.parent / 'agent-tools/xhs-natural-expression/SKILL.md').read_text()
    # Include the canonical short entrypoint in the immutable task instruction.
    # Native skill discovery is optional model behavior; a mounted file alone
    # does not establish that this turn loaded it. Do not duplicate its policy
    # in independently maintained prompts or make each turn do another read.
    body = natural_content.split('---', 2)[2].strip()
    instruction = ROUTE_INSTRUCTION + '\n\n本轮表达规范（xhs-natural-expression，sha256=' + hashlib.sha256(body.encode()).hexdigest() + '）：\n' + body
    assert len(instruction) <= 4000, 'Keep the canonical entrypoint concise enough for the route contract'
    backup = {'agent_id': AGENT_ID, 'instructions': agent['instructions'], 'thinking_level': agent.get('thinking_level'),
              'route': route, 'skills': old_skills}
    directory = ROOT / '.local/backups'; directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / ('dm-dialogue-before-' + str(time.time_ns()) + '.json')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)
    for name, sid in SKILLS.items():
        home = ROOT.parent / 'agent-tools' / name
        content = (home / 'SKILL.md').read_text()
        # Retain unrelated remote references, update maintained local files.
        files = {f['path']: f['content'] for f in old_skills[name].get('files', [])}
        for file in (home / 'references').rglob('*'):
            if file.is_file():
                files[file.relative_to(home).as_posix()] = file.read_text()
        payload = {'content': content, 'files': [{'path': p, 'content': c} for p, c in sorted(files.items())]}
        if name == 'xhs-natural-expression':
            payload['description'] = '在小红书普通私聊中接住具体内容、参与共同梗、选择真实表情并自然收尾；结合双向快照，不把模型生成历史当作本人风格。'
        remote.request('PUT', '/api/skills/' + sid, payload)
        check = remote.request('GET', '/api/skills/' + sid)
        assert check['content'] == content
        assert {f['path']: f['content'] for f in check['files']} == files
    remote.request('PUT', '/api/agents/' + AGENT_ID, {'instructions': INSTRUCTIONS, 'thinking_level': 'low'})
    check = remote.request('GET', '/api/agents/' + AGENT_ID)
    assert check['instructions'] == INSTRUCTIONS
    for field in ('model', 'runtime_id', 'max_concurrent_tasks'):
        assert check.get(field) == agent.get(field), 'Unexpected setting change: ' + field
    updated = (store.save_route({**route, 'instruction': instruction}, ROUTE_ID)
               if route['instruction'] != instruction else route)
    print(json.dumps({'published_skills': list(SKILLS), 'route_version': updated['version'],
                      'agent_verified': True, 'backup': path.name}, ensure_ascii=False))


if __name__ == '__main__':
    main()
