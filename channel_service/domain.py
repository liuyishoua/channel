import hashlib
import json
import re
import uuid


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def identifier(value, name, maximum=160, optional=False):
    if optional and value in (None, ''):
        return ''
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise Problem(f'{name} 必须是 1–{maximum} 字的标识')
    return value.strip()


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Problem(f'{name} 必须在 {low}–{high} 之间')
    return value


SECRET_KEYS = re.compile(r'^(?:password|passwd|cookie|authorization|secret|access_token|refresh_token|api_key|token|private_key|headers|session_key)$', re.I)


def inspect_context(value, depth=0):
    if depth > 8:
        raise Problem('上下文层级过深')
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEYS.match(key):
                raise Problem('上下文不能携带密码、Cookie、令牌或请求头')
            inspect_context(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            inspect_context(child, depth + 1)
    elif isinstance(value, str) and re.search(r'\b(?:mul_|mat_|sk-)[A-Za-z0-9_-]{16,}', value):
        raise Problem('上下文不能携带访问凭据')


def normalize_message(data, source):
    if not isinstance(data, dict):
        raise Problem('消息必须是 JSON 对象')
    if data.get('source_id', source) != source:
        raise Problem('凭据与请求来源不一致', 403)
    account = identifier(data.get('account_id'), 'account_id')
    message = identifier(data.get('message_id'), 'message_id')
    category = identifier(data.get('category', 'content_analysis'), 'category', 80)
    conversation = identifier(data.get('conversation_id'), 'conversation_id', optional=True)
    content = data.get('content', {})
    if not isinstance(content, dict):
        raise Problem('content 必须是对象')
    text = content.get('text', '')
    images = content.get('images', [])
    if not isinstance(text, str) or len(text) > 16000:
        raise Problem('正文最多 16000 字')
    if not isinstance(images, list) or len(images) > 8:
        raise Problem('最多携带 8 张图片')
    images = [identifier(x, 'image asset_id', 80) for x in images]
    if len(set(images)) != len(images):
        raise Problem('图片不能重复')
    if not text.strip() and not images:
        raise Problem('请提供文字或图片')
    context = data.get('context', {})
    if not isinstance(context, dict) or len(dumps(context).encode()) > 24000:
        raise Problem('context 必须是 24KB 以内的对象')
    inspect_context({'text': text, 'context': context})
    reply = identifier(data.get('reply_to'), 'reply_to', optional=True)
    if reply == message:
        raise Problem('消息不能回复自身')
    thread = identifier(data.get('thread_id'), 'thread_id', 80, optional=True)
    fresh = data.get('new_task', False)
    if not isinstance(fresh, bool) or (fresh and (reply or thread)):
        raise Problem('new_task 不能同时指定回复消息或任务会话')
    dimensions = {}
    if data.get('session_snapshot_id'):
        dimensions['session_snapshot_id'] = identifier(data['session_snapshot_id'], 'session_snapshot_id', 80)
    if 'surface' in data or 'event_type' in data:
        surface = data.get('surface', 'manual')
        if surface not in ('manual', 'dm', 'comment', 'notification', 'content', 'schedule'):
            raise Problem('业务入口无效')
        dimensions.update(surface=surface, event_type=identifier(data.get('event_type', 'manual.request'), 'event_type', 80))
    if data.get('route_id'):
        dimensions['route_id'] = identifier(data['route_id'], 'route_id', 80)
    return dict(source_id=source, account_id=account, message_id=message, category=category,
                conversation_id=conversation, content={'text': text.strip(), 'images': images}, context=context,
                reply_to=reply, thread_id=thread, new_task=fresh,
                kind='mixed' if images and text.strip() else 'image' if images else 'text', **dimensions)


def normalize_route(data, route_id=None, version=1):
    if not isinstance(data, dict):
        raise Problem('路由必须是对象')
    route_id = route_id or str(uuid.uuid4())
    agent = identifier(data.get('agent_id'), 'agent_id', 80)
    try:
        uuid.UUID(agent)
    except ValueError:
        raise Problem('请选择有效的 Multica agent')
    project = identifier(data.get('project_id'), 'project_id', 80, optional=True)
    if project:
        try:
            uuid.UUID(project)
        except ValueError:
            raise Problem('project_id 必须是 UUID')
    sources = data.get('sources', ['*'])
    categories = data.get('categories', ['*'])
    kinds = data.get('kinds', ['text', 'image', 'mixed'])
    surfaces = data.get('surfaces', ['*'])
    event_types = data.get('event_types', ['*'])
    goal_ids = data.get('goal_ids', ['*'])
    for name, values in [('sources', sources), ('categories', categories), ('kinds', kinds), ('surfaces', surfaces), ('event_types', event_types), ('goal_ids', goal_ids)]:
        if not isinstance(values, list) or not 1 <= len(values) <= 30:
            raise Problem(f'{name} 至少选择一项')
        for value in values:
            identifier(value, name, 80)
    if not set(kinds) <= {'text', 'image', 'mixed'}:
        raise Problem('消息类型无效')
    grouping = data.get('grouping', 'conversation')
    if grouping not in ('message', 'conversation', 'topic'):
        raise Problem('归并方式无效')
    enabled = data.get('enabled', True)
    if not isinstance(enabled, bool):
        raise Problem('enabled 必须是布尔值')
    prompt = data.get('instruction', '分析上下文，给出可以直接采用的中文建议。')
    if not isinstance(prompt, str) or len(prompt) > 4000:
        raise Problem('处理要求最多 4000 字')
    return dict(id=route_id, version=version, name=identifier(data.get('name'), '路由名称', 80),
                agent_id=agent, project_id=project, sources=sources, categories=categories, kinds=kinds,
                surfaces=surfaces, event_types=event_types, goal_ids=goal_ids,
                grouping=grouping, enabled=enabled, instruction=prompt,
                priority=integer(data.get('priority', 50), '优先级', 0, 100),
                idle_seconds=integer(data.get('idle_seconds', 86400), '会话间隔', 60, 2592000),
                max_messages=integer(data.get('max_messages', 100), '会话消息上限', 1, 1000),
                batch_seconds=integer(data.get('batch_seconds', 2), '合批等待秒数', 0, 30),
                max_batch=integer(data.get('max_batch', 10), '单批消息上限', 1, 20),
                max_context_bytes=integer(data.get('max_context_bytes', 262144), '任务上下文上限', 65536, 2097152),
                timeout_seconds=integer(data.get('timeout_seconds', 900), '任务超时秒数', 30, 7200))


def match_route(message, routes):
    for route in sorted(routes, key=lambda x: (-x['priority'], x['id'])):
        if message.get('route_id') and route['id'] != message['route_id']:
            continue
        if route['enabled'] and ('*' in route['sources'] or message['source_id'] in route['sources']) and \
                ('*' in route['categories'] or message['category'] in route['categories']) and message['kind'] in route['kinds'] and \
                ('*' in route.get('surfaces', ['*']) or message.get('surface', 'manual') in route['surfaces']) and \
                ('*' in route.get('event_types', ['*']) or message.get('event_type', 'manual.request') in route['event_types']) and \
                ('*' in route.get('goal_ids', ['*']) or message['context'].get('goal_id') in route['goal_ids']):
            return route
    return None


def group_key(message, route):
    if message.get('session_snapshot_id'):
        # Store has verified the snapshot belongs to precisely this business scope.
        return digest(['session', message['source_id'], message['account_id'], message.get('surface', 'manual'), message['conversation_id']])
    if message['new_task'] or route['grouping'] == 'message':
        scope = ['message', message['message_id']]
    elif route['grouping'] == 'conversation' and message['conversation_id']:
        scope = ['conversation', message['conversation_id'], identifier(message['context'].get('business_key'), 'business_key', optional=True)]
    elif route['grouping'] == 'topic' and message['context'].get('topic_id'):
        scope = ['topic', identifier(message['context']['topic_id'], 'topic_id')]
    else:
        scope = ['message', message['message_id']]
    if message.get('surface', 'manual') != 'manual':
        scope = [message['surface'], scope]
    return digest([message['source_id'], message['account_id'], message['category'], route['id'], route['version'],
                   route['agent_id'], route['project_id'], scope])
