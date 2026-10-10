# 目标编排接入协议

旧 `/v1/messages`、结果 outbox 与八个工作台主动分析接口保持兼容。新业务事件不会自动改写旧请求。

## 业务事件

`POST /v1/events` 使用所属来源的 Bearer key，拒绝浏览器 Origin 和仅 Cookie 认证。

```json
{
  "account_id": "stable-platform-account",
  "event_id": "dm:platform-message-unique-id",
  "event_type": "dm.received",
  "conversation_id": "stable-platform-conversation",
  "direction": "incoming",
  "transport": "socket",
  "content": {"text": "这个能包邮吗？", "images": []},
  "context": {"business_product": {"id": "product-123", "postage": "0"}}
}
```

首次202，重复200，内容冲突409。surface 从事件类型导出；若显式提供必须一致。dm 必须有 conversation_id；评论、通知、作品必须有 resource.id，可附 parent_id、author_id。来源、账号与图片归属校验复用 Channel。采集方式不纳入业务指纹，但正文、方向及上下文变化会冲突；生产者应持久保存首次规范事件，重试不能临时重组不同历史。

事件类型：dm.received、dm.sent、comment.received、notification.received、content.requested、content.published。dm.followup_due 仅服务计时器生成。

## 在线账号执行器

`POST /v1/connectors/heartbeat`

```json
{"account_id":"stable-platform-account","executor_id":"xianyu-local-v1","capabilities":["dm.reply"]}
```

同一账号同时只允许一个在线执行器，120秒视为离线。推荐30秒心跳。只观察时 capabilities=[]。

`POST /v1/actions/claim`：相同 account_id、executor_id；返回 action=null 或唯一动作与一次性 claim_token。账号、target、正文及图片全部使用响应中的固定值；执行器仍须验证当前平台身份。claim_token 是私有执行凭据，不能交给模型或浏览器。

`POST /v1/actions/{id}/receipt`

```json
{
  "account_id": "stable-platform-account",
  "claim_token": "private-value-from-claim",
  "status": "succeeded",
  "external_id": "platform-message-id",
  "detail": "平台接口确认"
}
```

status 为 succeeded、failed 或 uncertain。仅明确未执行或平台明确拒绝才能报告 failed；传输超时、响应丢失和服务重启中断报告 uncertain。成功要求平台标识；任务已受理不等于成功。终态相同回执可重复，不同结果409。

自动化新 API 不支持把不确定动作重新领取。原执行器持私有 claim_token 核对原工作台任务后补回执。不要另造 action_id 发送同一内容绕过核对。

## 管理 API

- GET /api/automation：目标、最近100条事件/事项/动作、执行器状态。
- POST /api/goals、PUT /api/goals/{id}：创建/修改目标；修改撤销未领取方案和待跟进计时。来源及执行账号不可改，需新建独立目标。
- POST /api/business-events：同一管理登录下提交规范事件，可能触发真实 agent 和已启用执行器。
- POST /api/actions/{id}/approve 或 reject：仅处理 review 状态。
- POST /api/goal-cases/{id}/close：结束事项并撤销未领取动作/计时，不删除平台执行记录。

管理写请求沿用精确 Origin、X-App-Request:1、JSON 与登录校验。

分流规则新增 surfaces、event_types、goal_ids 筛选维度，缺省*。目标专属规则由目标管理，不能通过通用规则接口修改。旧消息未携带新维度时沿用原指纹和默认 manual 入口。
