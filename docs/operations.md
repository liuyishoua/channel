# 部署与维护

## 持久状态与服务

- `.local/channel.sqlite3`：消息、规则、来源凭据哈希、会话映射、批次、结果 outbox 和操作记录。
- `.local/assets/`：经过格式/大小验证的业务图片，账号范围内内容寻址。
- `.local/multica.json`：独立 Channel PAT、工作区和固定 Multica 地址。PAT 使用当前所有者的权限访问已配置的私有 agent，不是细粒度操作系统权限隔离。
- `.local/admin-password`、`.local/session-key`：管理台认证。
- 各来源 `.local/web/channel.json`：只供该工作台后端使用的独立来源凭据。

私有目录 0700、文件 0600，均由 Git 忽略；systemd 使用 UMask=0077。源码和部署 unit 不保存密码或凭据。

```sh
sudo systemctl status channel channel-proxy
sudo systemctl restart channel.service
curl -fsS https://portal.example.com:20042/healthz
sudo journalctl -u channel --since today
```

修改工作台适配代码后，只重启对应工作台服务。Channel 不重启 Multica、数据库或 agent runtime。Caddy 只增加独立 20042 入口，未修改域名首页或既有模型 API。

## 恢复

停服备份时，备份完整 `.local/`（含数据库、图片、私有配置），并备份各工作台来源配置；备份须维持私有权限。在线备份数据库应用 SQLite backup API，不只复制正在使用的主数据库文件而遗漏 WAL。

启动后，worker 接管租约到期的批次。已记录写入意图但未取得响应的批次先核对 Multica 标记；已绑定任务的批次继续查询原执行。不会把恢复当作新的业务请求。

不确定投递会阻塞该业务会话。先打开关联 Multica 任务核对，再在 Channel 点「重新核对投递」。系统没有“忽略原记录直接重发”的按钮。

模型任务长时间等待时，检查 Multica agent/runtime 和对应执行记录；Channel 不擅自重启 runtime 或重复创建模型任务。模型已被取消后，Channel 取得该精确执行的终态并释放会话。

小红书代聊身份需明确允许读取当前 Multica 任务单和本轮触发评论：任务启动只提供 issue ID，私信快照在任务正文内。“禁止平台操作”限制对外发送、发布和修改资源，不能阻止读取当前任务输入。`scripts/fix_dm_task_reading.py --execute` 备份并修正当前代聊 Agent 的指令；`scripts/smoke_dm_task_reading.py --execute` 使用独立临时 Channel 队列与虚构聊天验证真实 Agent 读取任务，添加 `--question` 验证明确提问能生成回复。验收只生成模型结果，不接入真实账号发送，报告及 issue/task ID 保存在私有 `.local/dm-task-reading*-verification.json`。

代聊耗时排查先区分排队和执行：查询精确 run 的 created_at、started_at、completed_at，再按 transcript 时间分析首次输出与工具调用，不以任务看板的 Todo 状态推断队列积压。当前代聊使用 Mac runtime、gpt-6-luna 和 low 推理强度；身份要求把当前任务、必需技能与评论摘要合并到一次工具调用，额外情境参考按需读取。模型、运行节点、媒体规则及安装技能保持原配置。合成提问对照验证的执行时间从 28 秒/5 次工具调用变为 18 秒/1 次调用；单次样本不构成延迟保证。

2026-10-10 自然对话更新：`scripts/publish_dm_dialogue_skill.py --execute` 将仓库维护的 xhs-natural-expression、gifgrep 及引用同步到 Multica，备份原 Skill、Agent 指令与路由到私有 `.local/backups/`。只调整代聊指令、low 推理强度和指定私信路由的 instruction；不改变模型、运行节点、并发数或技能绑定。已有 route snapshot 保持不变。该脚本重新发布相同路由文案时不增加路由版本。

路由指令直接包含从 xhs-natural-expression 提取的短版主体及 SHA256，保持在4000字以内。新任务随不可变路由快照取得该版本主规范，不依赖模型自愿读取技能文件；挂载文件继续提供条件参考。Agent 在任务已有“本轮表达规范”时不重复读取主体，纯文字决策不下载无用图片，选择媒体时才下载并查看实际候选。

真实模型回归：`scripts/evaluate_dm_conversation.py --execute --phase <label> --cases detail,invitation,shared_joke,celebration,laughter,goodnight,distress,no_images,sticker_turn,style_feedback,ordinary`。使用独立临时队列、虚构会话与本地已准备 GIF 的视觉采样，创建真实 Multica 执行但不连接社交发送器。原始结果与精确 issue/run ID 保存在 `.local/dialogue-evaluation-<label>.json`。另外以 `visual_probe,visual_probe_left,visual_probe_right` 去掉语义标签、随机化资产 ID、调整图片顺序，检查能否认出真实拥抱画面。当前 Codex 适配层不转存原生图像查看事件，不能从 transcript 缺少 view_image 直接断言没看图；视觉验证以盲化后的选图结果为依据。

可供审阅的合成输出、对应执行和验证边界见 dialogue-acceptance.json（本地资料）。三个换序视觉场景全部认对，主动斗图、共笑和庆祝返回原生 sticker。XHS 的 `deploy/verify_dialogue_media.py --report <私有合成报告路径> --case <情境>` 将这些真实模型结果交给临时数据库与假传输，验证 `content_type=16`、GIF 标志、文字/图顺序、匹配回执与防重发；不调用真实小红书发送接口。

最终路由5追加7项真实模型检查通过，包含加班纠正及未出现在示例中的高铁晚点纠正；此前的通用休息建议在这轮消失。一次候选返回 action 而非 decision，已明确 Agent 的精确字段契约，原始失败记录保留。最终7项端到端模型墙钟时间16.8–42.9秒，中位数23.2秒；较早一轮曾达113.6秒。这里包含附件及排队等开销，不能以这些样本宣称延迟或真人自然度已全面达标。

## 凭据维护

首次部署 `deploy/bootstrap.py` 在非 task 环境使用当前已授权的 Multica CLI 用户签发独立服务 PAT，创建八个来源及三条默认业务规则。该脚本不打印凭据；重复执行不会复制新 PAT 或轮换已有来源。

后台每天调用 Multica 官方 `/api/tokens/current/renew` 检查独立 PAT；接近到期时由 Multica 延长有效期，并更新本机配置中的到期时间。续期失败按小时重查并写入不含凭据的诊断日志。凭据被撤销或 agent 权限变化时，在管理台显示连接异常或投递前失败，不能借用其他 agent/task 的令牌。

在管理台轮换来源凭据后，必须将一次性返回的新凭据更新到该工作台 `.local/web/channel.json`。旧凭据立即失效；已有队列和模型任务不被删除。

## 可选账号自动化

新增 goals、business_events、goal_cases、goal_jobs、account_actions、account_connectors、contact_blocks、followup_timers 表与原 Channel 队列使用同一个 SQLite 事务存储。仅增加新表，不删除原消息/结果。上线前使用 SQLite backup API 保存私有快照。

目标管理位于 `/automation`。部署不自动创建真实账号目标，也不启动执行器。可选 `deploy/channel-xianyu-connector.service` 与示例配置见[账号目标设计](account-automation.md)。连接器私有日志保存动作租约和恢复记录，随 `.local/` 备份，不能导出到公开验收材料。

暂停目标撤销未领取动作与计时器；已领取动作以平台实际结果为准。uncertain 阻止账号继续领取，原执行器凭原领取凭据补交核对结果；不能删除账本后重发。停止联系记录跨目标修改保留。

## 原消息链路验证范围

单元测试覆盖去重并发、账号/来源隔离、路由版本、归并分支、乱序回复、合批/串行、附件绑定、写入响应丢失、重启恢复、任务结果关联、超时及结果确认。浏览器验收使用临时队列、演示模型与临时工作台数据；正式页面另外验证 HTTPS 登录和真实连接状态。

真实模型验收使用纯虚构客服请求和生成的几何图形，验证文字、图文、纯图片三轮沿用一张任务单，并取得三个不同执行 ID 的正确输出。它不代表真实平台私信监听或自动发送已启用，也不替代平台账号认证。

## 独立指挥部

跨平台业务目标移至 [Headquarters](https://portal.example.com:20043/)。Channel 的 `/control/v1/*` 使用独立 `.local/command-key`，平台来源凭据和浏览器会话无权访问。新增控制归属和动作变更账本，指挥部拥有的规则由其管理；90 秒授权到期暂停新模型任务及动作领取，保留新事件等待续约。现有本地规则和来源接口继续兼容。
