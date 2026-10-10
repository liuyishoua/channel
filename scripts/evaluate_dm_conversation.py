"""Real-model dialogue checks using synthetic sessions; never dispatch to XHS.

Reports contain only invented dialogue and owned test attachments, kept in .local.
This is a qualitative regression exercise, not a human-equivalence benchmark.
"""
import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from channel_service.multica import Multica
from channel_service.store import Store
from channel_service.worker import Worker
from channel_service.sessions import iso

CASES = {
    'detail': [('inbound', '今天忙死'), ('outbound', '咋啦'), ('inbound', '老板又让我改第八版了')],
    'invitation': [('inbound', '打游戏不'), ('outbound', '玩啥'), ('inbound', '王者，你到底打不打')],
    'shared_joke': [('inbound', '我这上班上得像个NPC'), ('outbound', '你的日常任务是什么'), ('inbound', '假装认真，实则摸鱼')],
    'celebration': [('inbound', '我终于过了！！那个考了三次的驾照！！')],
    'laughter': [('inbound', '同事说早八不苦，命苦'), ('outbound', '他这班上得还挺押韵'), ('inbound', '哈哈哈哈哈哈')],
    'goodnight': [('inbound', '我先睡啦'), ('outbound', '好，晚安'), ('inbound', '嗯嗯晚安')],
    'distress': [('inbound', '今天被当众批评，回家一直哭，感觉自己挺没用的')],
    'no_images': [('inbound', '先别发表情包了，字好小。我今天考过啦')],
    'sticker_turn': [('inbound', '来斗图，你先来！')],
    'style_feedback': [('outbound', '哈哈～你也太可爱了⊙ω⊙'), ('inbound', '别这么说了'), ('outbound', '哈哈～好呀⊙ω⊙'), ('inbound', '我是说今晚还得加班两小时，烦死了')],
    'repair_holdout': [('inbound', '今天真无语'), ('outbound', '哈哈～怎么还撒起娇了⊙ω⊙'), ('inbound', '我是说高铁又晚点四十分钟，不是跟你撒娇')],
    'ordinary': [('inbound', '准备回家了，还得先去取个快递，绕好远')],
    'visual_probe': [('inbound', '这几张里哪张是抱着另一只猫的？直接发那张，不要按素材标签猜。')],
    'visual_probe_left': [('inbound', '这几张里哪张是抱着另一只猫的？直接发那张，不要按素材标签猜。')],
    'visual_probe_right': [('inbound', '这几张里哪张是抱着另一只猫的？直接发那张，不要按素材标签猜。')],
}


def test_images(store, account):
    """Use four reviewed cached GIFs as real visual inputs, with synthetic IDs."""
    import sqlite3
    home = ROOT.parent / 'xhs'
    with sqlite3.connect(home / 'data/spider_xhs.db') as db:
        assets = db.execute("SELECT filename,label FROM xhs_dm_media WHERE account_id=1 AND library=1 AND filename!='' ORDER BY id").fetchall()
    selected = []
    for cue in ('偷笑', '抱抱', '震惊', '赞'):
        found = next(((f, label) for f, label in assets if cue in label), None)
        if found and found not in selected:
            selected.append(found)
    images, catalog = [], []
    for index, (filename, label) in enumerate(selected, 1):
        with Image.open(home / '.local/dm-media' / filename) as gif:
            frames = []
            count = getattr(gif, 'n_frames', 1)
            for n in sorted(set([0, count // 2, count - 1])):
                gif.seek(n)
                frame = gif.convert('RGB'); frame.thumbnail((480, 480)); frames.append(frame.copy())
            canvas = Image.new('RGB', (max(f.width for f in frames), sum(f.height for f in frames)), 'white')
            y = 0
            for frame in frames:
                canvas.paste(frame, (0, y)); y += frame.height
            output = io.BytesIO(); canvas.save(output, 'JPEG', quality=85)
        image = store.put_asset('xhs', account, output.getvalue())
        images.append(image['id'])
        catalog.append({'asset_id': 'test-reaction-' + str(index), 'kind': 'sticker', 'content_type': 16, 'library': True, 'label': label,
                        'source': 'reaction_library', 'image_number': index, 'vision': 'GIF首中末采样'})
    assert len(images) >= 3, 'Reviewed fixture pack incomplete'
    return images, catalog


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--execute', action='store_true')
    p.add_argument('--phase', required=True)
    p.add_argument('--cases', default=','.join(CASES))
    args = p.parse_args()
    if not args.execute:
        p.error('--execute required: this creates synthetic Multica model runs')
    if not args.phase.replace('-', '').isalnum():
        p.error('phase must be a simple label')
    cases = args.cases.split(',')
    assert all(key in CASES for key in cases)
    production = Store(ROOT / '.local')
    route = next(r for r in production.routes() if r['id'] == '978ddec8-b2f4-4ac5-9580-9abf16f4c96d')
    remote = Multica(ROOT / '.local/multica.json')
    reports = []
    path = ROOT / ('.local/dialogue-evaluation-' + args.phase + '.json')
    with tempfile.TemporaryDirectory(prefix='synthetic-dialogue-') as directory:
        store = Store(directory); store.source('xhs')
        route = store.save_route({**route, 'batch_seconds': 0})
        worker = Worker(store, remote)
        account = 'synthetic-dialogue-evaluation'
        images, catalog = test_images(store, account)
        for key in cases:
            current_images, current_catalog, expected_visual = images, catalog, None
            if key.startswith('visual_probe'):
                order = {'visual_probe': [2, 0, 1], 'visual_probe_left': [1, 2, 0], 'visual_probe_right': [0, 1, 2]}[key]
                current_images = [images[i] for i in order]
                current_catalog = []
                for index, original in enumerate(order, 1):
                    asset_id = 'probe-' + uuid.uuid4().hex
                    current_catalog.append({**catalog[original], 'asset_id': asset_id,
                                            'label': '候选' + str(index), 'image_number': index})
                    if original == 1:
                        expected_visual = asset_id
            now = time.time()
            session = store.sessions.open('xhs', dict(account_id=account, surface='dm', conversation_id='synthetic-' + key))
            messages = [dict(message_id='m-' + str(i), direction=direction, text=text, sent_at=iso(now - (len(CASES[key]) - i) * 8))
                        for i, (direction, text) in enumerate(CASES[key])]
            store.sessions.sync('xhs', session['id'], dict(account_id=account, messages=messages,
                                coverage=dict(since=iso(now - 172800), until=iso(now), complete=True)))
            snapshot = store.sessions.snapshot('xhs', session['id'], dict(account_id=account, request_id=key, trigger_message_ids=[messages[-1]['message_id']]))
            automated = [m['message_id'] for m in messages if m['direction'] == 'outbound'] if key in ('style_feedback', 'repair_holdout') else []
            event = store.ingest('xhs', dict(account_id=account, message_id=key, conversation_id='synthetic-' + key,
                category='reply_draft', surface='dm', event_type='dm.received', route_id=route['id'], session_snapshot_id=snapshot['id'],
                content={'text': '虚构私聊验收，只根据当前快照决定回复。没有可用的本人日程或游戏上线事实。按JSON契约输出，不执行平台发送。', 'images': current_images},
                context={'control_mode': 'full_access', 'media_catalog': current_catalog,
                         'dialogue_state': {'known_automated_message_ids': automated, 'self_style_candidates': [], 'peer_is_managed_account': False}}))[0]
            start = time.monotonic()
            state = store.message(event['id'])
            while time.monotonic() - start < 240:
                with store.db(True) as db:
                    db.execute('UPDATE batches SET next_poll=0,lease_until=0')
                batch = store.claim()
                if batch:
                    worker.process(batch)
                state = store.message(event['id'])
                if state['state'] in ('succeeded', 'failed', 'failed_preflight', 'cancelled', 'uncertain', 'attention'):
                    break
                time.sleep(1)
            output = ((state.get('result') or {}).get('output') or {}).get('structured') or {}
            execution = state.get('execution') or {}
            transcript = remote.transcript(execution['task_id']) if execution.get('task_id') else []
            record = dict(case=key, messages=messages, catalog=current_catalog, expected_visual_asset=expected_visual, elapsed_seconds=round(time.monotonic()-start, 1),
                          state=state['state'], error=state.get('error'), execution=execution, output=output, transcript=transcript)
            reports.append(record)
            path.write_text(json.dumps(reports, ensure_ascii=False, indent=2)); path.chmod(0o600)
            print(json.dumps({k: record[k] for k in ('case', 'elapsed_seconds', 'state', 'execution', 'output')}, ensure_ascii=False), flush=True)
            assert state['state'] == 'succeeded', record['error']
            assert output.get('decision') in ('reply', 'no_reply') and output.get('requires_human') is False
            for proposal in output.get('reply_messages', []):
                if proposal.get('asset_id'):
                    assert proposal['asset_id'] in {a['asset_id'] for a in current_catalog}, 'Invented media'
            if key in ('detail', 'invitation', 'celebration', 'distress', 'no_images', 'sticker_turn', 'style_feedback', 'repair_holdout', 'ordinary'):
                assert output.get('decision') == 'reply', 'Missed an active turn'
            if key == 'goodnight':
                assert output['decision'] == 'no_reply', 'Prolonged completed farewell'
            if key == 'no_images':
                assert not any(m.get('asset_id') for m in output['reply_messages']), 'Ignored no-image preference'
            if key.startswith('visual_probe'):
                assert any(m.get('asset_id') == expected_visual and m.get('type') == 'sticker'
                           for m in output.get('reply_messages', [])), 'Did not identify actual hug picture'
    print('Saved synthetic run report:', path, flush=True)


if __name__ == '__main__':
    main()
