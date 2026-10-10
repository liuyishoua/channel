"""Flask workspace integration. Account identity is supplied by the backend."""
from pathlib import Path
from flask import request
from .channel_client import ChannelClient


def register_channel(app, service, config_path, record_lookup=None):
    def account():
        identity = service.platform.identity()
        return str(identity['id']) if identity else 'anonymous'
    def client():
        return ChannelClient(config_path)
    def channel_meta():
        return {'configured': Path(config_path).is_file(), 'console_url': 'https://d.58nft.net:9456/'}
    def submit():
        data = request.json
        owner = account()
        prompt = data.get('text', '分析这条内容，提炼要点并给出建议。')
        if not isinstance(prompt, str) or len(prompt) > 4000:
            raise ValueError('处理要求最多 4000 字')
        context = data.get('context', {})
        if not isinstance(context, dict):
            raise ValueError('上下文必须是对象')
        item_id = data.get('item_id')
        if item_id and record_lookup:
            item = record_lookup(str(item_id), owner)
            context = {**context, 'topic_id': str(item_id), 'item': {k: item.get(k) for k in ('id','title','text','url','author','uid','price','note') if item.get(k) is not None}}
        category = data.get('category', 'product_research' if 'price' in context.get('item', {}) else 'content_analysis')
        message_id = data.get('message_id') or data.get('request_id')
        if not isinstance(message_id, str) or not 8 <= len(message_id) <= 160:
            raise ValueError('请提供稳定的唯一消息 ID')
        channel = client()
        result = channel.send(owner, message_id, prompt, data.get('images', []), category,
                               str(data.get('conversation_id') or ('item:' + str(item_id) if item_id else '')),
                               context, data.get('reply_to', ''), data.get('new_task', False),
                               surface='comment' if category == 'comment_analysis' or channel.source == 'zhihu' else 'content',
                               event_type='comment.analysis_requested' if category == 'comment_analysis' or channel.source == 'zhihu' else 'content.analysis_requested')
        return result, 202
    def status(event_id):
        return client().status(event_id, account())
    app.add_url_rule('/api/channel/meta', 'channel_meta', channel_meta)
    app.add_url_rule('/api/channel/messages', 'channel_submit', submit, methods=['POST'])
    app.add_url_rule('/api/channel/messages/<event_id>', 'channel_status', status)
