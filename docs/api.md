# 接入协议

通用会话存储、增量同步、记忆与独立上下文执行见 [Session 接入协议](sessions.md)。新接入的持续聊天渠道优先使用 Session API；历史同步不触发模型。

生产者使用各工作台后端的 `.local/web/channel.json`，包含 source_id、base_url、token；不得传给浏览器。公网地址 `https://portal.example.com:20042`，同机可使用 `http://127.0.0.1:20010`。

所有 `/v1/` 请求使用 `Authorization: Bearer <来源凭据>`。Cookie 和访问密码不能代替来源凭据，带浏览器 Origin 的机器请求会被拒绝。不支持任意回调 URL 或服务端抓取任意图片 URL。

## 上传图片

`POST /v1/assets`，multipart 表单字段 account_id、file。支持 PNG/JPEG/WebP/GIF；返回 id、media_type、bytes。同来源账号的同一图片重复上传返回同一 ID。图片 ID 只能用于对应来源账号。

## 提交消息

`POST /v1/messages`，JSON：

```json
{
  "source_id": "xianyu",
  "account_id": "平台账号稳定ID",
  "message_id": "来源内稳定且唯一的消息ID",
  "category": "reply_draft",
  "conversation_id": "客户会话ID",
  "content": {"text": "客户咨询内容或本次处理要求", "images": []},
  "context": {"business_key": "order:123", "product": {"title": "示例商品", "price": 99}},
  "reply_to": "",
  "new_task": false
}
```

- source_id 可省略，若提供必须与凭据一致。account_id、message_id 必填。
- content.text、content.images 至少一个非空；images 为上传返回的图片 ID。
- 类别可按业务定义，默认 content_analysis。初始规则支持 reply_draft、content_analysis、comment_analysis、product_research、image_analysis。
- conversation_id 用于会话归并；context.topic_id 用于主题归并；context.business_key 在同一会话内区分订单等事项。
- reply_to 为同来源、同账号的原平台 message_id；thread_id 可显式指定已有业务会话，仍须通过范围校验。
- new_task 与 reply_to/thread_id 互斥。
- 禁止在 context 携带 Cookie、密码、令牌、请求头或私钥。平台完整响应应先选择业务字段，不能直接转发鉴权响应。

新消息返回 202，字段 id 为 Channel event_id；state 表示当前阶段，duplicate=false。相同唯一键且内容一致返回 200、duplicate=true；内容不同返回 409。

`POST /v1/messages/{event_id}/retry` 携带 `{"account_id":"账号ID"}`，仅允许同一来源及账号恢复 `failed_preflight`（确认未投递）任务。排队、等待或已成功的重复请求返回当前状态，不重新投递；不确定或已执行失败的任务返回409。恢复沿用原event ID及冻结请求，更新当前路由，替换旧失败结果；旧批次和审计保留。

重要：提交响应丢失时，使用原 message_id 和完全相同的 payload 重试。不要为了绕过超时更换 ID。

## 查询结果

`GET /v1/messages/{event_id}?account_id=...` 返回原消息、接收时的规则快照、业务会话、batch_id、execution 和 result。

- queued/processing/waiting：尚未取得最终结果。
- blocked_no_route/blocked_reply：消息已保留，等待规则或原消息。
- attention：模型等待超时或权限需关注；系统仍在核对，不代表失败重跑。
- uncertain：无法确认远程写入是否已生效，不会重复投递。
- succeeded：已取得准确对应执行的模型输出。
- cancelled：已在投递前取消，结果记录中保留取消原因。
- failed：模型执行失败或取消；failed_preflight：未确认投递的前置失败，可在管理台处理。

result 包含 event_id、message_id、source_id、account_id、thread_id、batch_id、issue_id、task_id、state、output、error。output.text 为模型最终文本，output.structured 为可解析 JSON，requires_human 为模型返回的建议字段，不是平台自动发送授权。

## 结果消费

`GET /v1/results?account_id=...&after=0` 返回最多 100 条结果及 next_cursor。每条结果带独立 result id、cursor、acknowledged；失败执行也会进入结果记录。

工作台在保存业务结果后，再 `POST /v1/results/{result_id}/ack`，JSON `{}`。重复确认安全。只有持有该来源凭据的生产者可以确认。游标由消费者自己持久保存，不因确认而删除结果。

## 管理 API

管理 API 使用 Channel 工作台登录 Cookie；写请求需要匹配 Origin、`X-App-Request: 1` 与 JSON（图片上传除外）。

- `/api/sources` GET/POST，`PATCH /api/sources/{id}` 停用或 rotate 凭据。
- `/api/routes` GET/POST，`PUT /api/routes/{id}` 保存新版本。
- 路由 `account_ids` 为 1–30 个账号稳定 ID 的列表，精确匹配并与 `sources` 同时生效；省略或包含 `*` 表示全部账号，旧配置缺失此字段也按 `*` 兼容。账号 ID 最长 160 字。身份专属 agent 的路由应显式填写账号 ID，避免账号串用。
- `POST /api/route-preview` 预览匹配，不触发模型。
- `/api/messages` GET/POST，`GET /api/messages/{id}`。
- `POST /api/messages/{id}/cancel` 取消尚未投递的排队或阻塞消息。
- `POST /api/messages/{id}/retry` 仅未投递、阻塞或前置失败消息允许重新排队。
- `POST /api/batches/{id}/reconcile` 再次核对不确定的投递，不重新发送。
- `/api/threads` GET，`POST /api/threads/{id}/close` 结束无未决批次的会话。
- `/api/assets` POST，`/api/overview`、`/api/meta`、`/api/audit` GET。
- `/healthz` 仅报告本机队列可访问，不表示模型任务成功。

各工作台的适配入口为 `/api/channel/messages` POST 与 `/api/channel/messages/{event_id}` GET，均沿用各工作台自己的登录/同源保护。工作台后端取得当前账号并补充已存的业务上下文，再使用来源凭据调用 Channel。

## 工作台模型配置

`GET /v1/catalog` 使用来源凭据读取该来源可用的普通路由及对应 Multica agent/model 信息；不暴露其他来源或目标专属规则，不返回凭据和处理指令。返回 `account_ids` 供工作台按当前账号筛选，旧路由缺少账号或业务入口维度时按 `*` 兼容。目录按来源列出，最终账号限制由消息接入再次校验。

`agents` 实时读取已配置工作区的 Multica Open API Agent 目录，包含尚未建立路由的 Agent；返回名称、运行状态等安全元数据，不返回运行配置或凭据。它独立于来源限定的 `routes`。目录请求失败返回明确错误，不使用旧目录代替。

`POST /v1/bindings` 使用来源凭据接收 `{agent_id, account_id, purpose}`，重新核对 Agent 实时可见性并复用或创建仅属于该来源、账号和用途的路由，返回 `{route, agent}`。用途为 `text`、`image`、`dm`、`comment` 或 `content`。工作台后端先验证当前登录用户拥有该账号；浏览器不接触来源或 Multica 凭据。显式选择不以预建路由或账号名称限制，账号名称只用于现有配置迁移和空默认的唯一匹配。绑定不会改写 Agent 的指令或挂载技能，也不会改另一路由的执行身份。提交时继续校验来源、账号与用途，历史以当时快照为准。

`POST /v1/messages` 可携带 `route_id` 选择目录内路由。仍需满足来源、账号、类别、媒体和业务入口全部条件，不可跨来源或账号选择；不匹配返回 409，不回退到其他规则。未指定时继续按优先级匹配。路由 ID 计入消息指纹，接收后保存不可变规则快照，后续路由修改不改变历史消息快照或同来源同账号的重试结果。

XHS 工作台按内容、图片、私信用途绑定路由。`source_id=xhs`、`surface=dm`、`category=reply_draft` 的结果格式含 `decision`、`reply_messages`、`summary`、`requires_human`；reply 时 `reply_messages` 为1至4条按顺序发送的文字或已读取媒体，每条含0至15秒 `delay_seconds`；no_reply 时为空。保留单条 `reply_text` / `reply_message` 输入。只有 `requires_human=false` 且整组校验有效时，工作台才可依照账号控制模式与自动回复开关发送。Channel 和 Multica 不执行平台发送。

Additional binding purposes: `research` produces product_research/content/content.analysis_requested; Zhihu uses comment/comment.analysis_requested for content/research, and Douyin dm uses dm/dm.draft_requested. Each route remains exact source/account and immutable per selected Agent.

小红书独立私信快照可配置 `execution_mode=frozen_reply`，其他路由仍为 `issue`。快照、来源和账号校验保持一致；即时回复由 Multica 的独立 Chat 任务执行，完整冻结资料直接作为模型输入，不通过 issue/comment 二次读取。每批使用一个 Chat，创建与提交前记录持久化意图，回执不明只核对已有记录，不重放模型请求。完成后归档该 Chat；平台发送仍由原工作台校验和执行。`/v1/catalog` 和 `/v1/bindings` 的 route 增加安全字段 `execution_mode`、`priority`，消息状态的 `execution.chat_session_id` 标识执行会话，`task_id` 仍为真实 Multica 任务 ID。即时模式仅允许 `sources=[xhs]`、`categories=[reply_draft]`、`surfaces=[dm]`，否则返回 400。
