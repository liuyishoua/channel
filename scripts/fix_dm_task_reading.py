"""Clarify the XHS agent bootstrap boundary; preserves all other settings."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from channel_service.multica import Multica

BOOTSTRAP = '''内部任务读取与对外操作的边界：
你通过 Multica 接收私信任务。启动时可能只收到 assigned issue ID 和“multica issue get”指令，私信正文与 context.messages 会话快照保存在该任务单内，并非尚未提供。
必须先执行 `multica issue get <当前 assigned issue ID> --output json`，读取当前任务正文；若本轮由评论触发，按运行时指引只读取得本轮触发评论。然后读取所需技能并基于本轮快照作出聊天决策。不能在读取当前任务之前以“缺少私信上下文”结束任务。
读取当前分配的内部任务单、本轮触发评论及其附件属于已授权的任务输入读取，不属于禁止的对外平台操作。身份规则和技能中“不执行平台操作”“不查阅旧任务”不禁止这些当前任务读取。只使用本轮快照，不查询其他任务、其他聊天对象或旧任务来重建历史。
禁止的是直接向小红书发送私信、发布内容、购买付款、修改账号资源、创建任务或定时追发，以及读取凭据。最终消息由工作台执行发送。若当前任务实际读取失败，明确报告读取失败，不把技术故障伪装为基于聊天内容的 no_reply 或 handoff。
'''

def main():
    p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true');args=p.parse_args()
    if not args.execute:p.error('--execute is required')
    m=Multica(ROOT/'.local/multica.json')
    aid='da72f33e-cac4-4ff0-a99d-d415219f3efc'
    a=m.request('GET','/api/agents/'+aid)
    old=a['instructions']
    if BOOTSTRAP in old:print('Already applied');return
    directory=ROOT/'.local/backups';directory.mkdir(exist_ok=True,mode=0o700)
    path=directory/('dm-instructions-before-task-read-'+str(int(time.time()))+'.json')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump({'agent_id':aid,'instructions':old},f,ensure_ascii=False,indent=2)
    new=BOOTSTRAP+'\n'+old
    m.request('PUT','/api/agents/'+aid,{'instructions':new})
    assert m.request('GET','/api/agents/'+aid)['instructions']==new
    print('Applied and verified current-task read boundary for XHS agent')

if __name__=='__main__':main()
