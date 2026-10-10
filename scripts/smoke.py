"""Real model acceptance with synthetic data only; run explicitly with --execute."""
import argparse
import io
import json
from pathlib import Path
import sys
import time
import uuid
from PIL import Image, ImageDraw
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'sdk'))
from channel_client import ChannelClient


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    if not args.execute:parser.error('--execute is required for real Multica/model calls')
    client=ChannelClient(ROOT.parent/'xianyu/.local/web/channel.json')
    account='channel-acceptance';conversation='acceptance-'+str(uuid.uuid4());records=[]
    def run(text,images=None):
        event=client.send(account,str(uuid.uuid4()),text,images,category='reply_draft',conversation_id=conversation,
                          context={'sample':True,'purpose':'通道联调，纯虚构信息，不发送任何平台消息'})
        print('Accepted',event['id'],flush=True)
        started=time.monotonic();last=''
        while time.monotonic()-started<240:
            event=client.status(event['id'],account)
            if event['state']!=last:print('State',event['state'],flush=True);last=event['state']
            if event['state'] in ('succeeded','failed','failed_preflight','uncertain','blocked_no_route','attention'):
                break
            time.sleep(2)
        record={'event_id':event['id'],'message_id':event['message_id'],'state':event['state'],'thread_id':event['thread_id'],
                'execution':event['execution'],'output':event.get('result',{}),'error':event['error']}
        records.append(record)
        (ROOT/'docs/acceptance.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        print(json.dumps(record,ensure_ascii=False,indent=2),flush=True)
        if event['state']!='succeeded':raise SystemExit('Model acceptance did not succeed')
        return event
    first=run('这是纯虚构客服验收：客户问“99元可以包邮吗？”，已知该示例商品支持包邮。请起草一句自然的中文回复，不实际发送，不执行工具。')
    image=Image.new('RGB',(640,400),'white');draw=ImageDraw.Draw(image)
    draw.ellipse((45,100,215,270),fill='blue');draw.rectangle((270,110,430,260),fill='yellow');draw.polygon([(540,90),(460,280),(620,280)],fill='red')
    blob=io.BytesIO();image.save(blob,'PNG');asset=client.upload(account,blob.getvalue(),'channel-vision-check.png')
    second=run('继续这个验收任务：请读取附件图片，说明里面从左到右的三个图形分别是什么颜色、什么形状。只依据实际看到的图像。', [asset['id']])
    third=run('', [asset['id']])
    if len({first['execution']['issue_id'],second['execution']['issue_id'],third['execution']['issue_id']})!=1:
        raise SystemExit('Append did not retain the same issue')
    if len({first['execution']['task_id'],second['execution']['task_id'],third['execution']['task_id']})!=3:
        raise SystemExit('Turns did not map to distinct model runs')
    print('PASS: text, mixed, image-only; one issue; three distinct model runs.',flush=True)

if __name__=='__main__':main()
