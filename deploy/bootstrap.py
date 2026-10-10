"""Initialize a dedicated Multica service credential and workspace sources without logging secrets."""
import json
import os
from pathlib import Path
import sys
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from channel_service.app import private_file
from channel_service.store import Store

ROOT = Path('/home/ubuntu/leo-project')
DIRECTORY = ROOT / 'channel/.local'
SOURCES = [('xhs','小红书','xhs'),('xianyu','闲鱼','xianyu'),('douyin','抖音','douyin'),
           ('bilibili','B站','bilibili'),('weibo','微博','weibo'),('jd','京东','jd'),
           ('kuaishou','快手','kuaishou'),('zhihu','知乎','zhihu')]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    if any(os.environ.get(x) for x in ('MULTICA_TASK_ID','MULTICA_AGENT_ID','MULTICA_DAEMON_PORT')):
        raise SystemExit('Run bootstrap as the service owner, outside an agent task')
    store = Store(DIRECTORY)
    config_path = DIRECTORY / 'multica.json'
    if not config_path.exists():
        profile = json.loads(Path('/home/ubuntu/.multica/config.json').read_text())
        payload = json.dumps({'name':'Channel message service','expires_in_days':90}).encode()
        req = urllib.request.Request('http://127.0.0.1:8080/api/tokens', payload,
            {'Authorization':'Bearer '+profile['token'],'Content-Type':'application/json'}, method='POST')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=20) as response:
            credential = json.load(response)
        write(config_path, dict(base_url='http://127.0.0.1:8080', public_url='https://d.58nft.net:9447', workspace_slug='leo',
            workspace_id=profile['workspace_id'],token=credential['token'],token_id=credential['id'],expires_at=credential['expires_at']))
    private_file(DIRECTORY / 'admin-password', '8'*8)
    known = {x['id'] for x in store.sources()}
    for slug, name, folder in SOURCES:
        client_path = ROOT / folder / '.local/web/channel.json'
        if slug in known:
            if not client_path.exists():
                raise SystemExit('Existing source missing client configuration: '+slug)
            continue
        source = store.source(slug, name+'工作台')
        write(client_path, {'source_id':slug,'base_url':'http://127.0.0.1:8780','token':source['token']})
    if not store.routes():
        agent='82ce74c8-e570-4f8f-9b07-dc3fa59e567c'
        store.save_route(dict(name='客户沟通 → 回复助手',sources=['xianyu','douyin','xhs','weibo','kuaishou'],categories=['reply_draft'],agent_id=agent,
            grouping='conversation',priority=90,instruction='结合客户会话和商品信息，起草自然、简洁的中文回复。没有证据的价格、库存、承诺不得编造；有待确认事项时标明。只起草，不发送。'))
        store.save_route(dict(name='京东商品 → 研究助手',sources=['jd'],categories=['product_research'],agent_id=agent,grouping='topic',priority=80,
            instruction='围绕同一商品汇总参数、价格观察和用户评价。区分平台提供的事实、观察与建议。给出中文研究结论，不下单、不支付。'))
        store.save_route(dict(name='社交内容 → 分析助手',sources=[x[0] for x in SOURCES],categories=['content_analysis','comment_analysis','image_analysis'],agent_id=agent,
            grouping='topic',priority=50,instruction='结合当前内容、评论或图片及已有上下文，提炼要点、受众需求和可操作建议。图片信息只按可见内容判断；资料不足时明确指出。'))
    print('Initialized Channel: dedicated Multica credential, 8 sources, '+str(len(store.routes()))+' routing rules; no credentials printed.')

if __name__ == '__main__':
    main()
