# 群星杯官方 QQ Bot 剩余工作需求

## Required behavior
- 保留 NapCat/OneBot v11 路径；官方 QQ 与 OneBot 共用 `services/bot_private_service.py` 业务入口。
- 群星杯群查分只读最后一次完整快照，不在消息回调中实时抓取 72 名玩家。
- 每日任务生成不可变快照、两张榜图和逐图投递状态；失败不覆盖 last-known-good。
- 官方运行时默认关闭；配置检查、preflight、测试和普通 import 均不得登录或联网。
- `GROUP_AND_C2C_EVENT` 下的关系事件必须纳入目标群状态，拒收或退群后禁止盲发。
- 平台权限、审核、关系、主动消息开关和真实回执不得由离线测试推断。
- 逐步补齐旧功能的官方事件夹具；成绩提交继续默认关闭。
- 不修改 event_hub、计分、Verse 缓存算法、快照 schema 或榜图布局。

## Acceptance criteria
- 基础测试 12/12、Bot 47/47、`tests/run_all.py --fast` 和官方适配专项测试保持通过。
- `qq-botpy==1.2.1` 离线 preflight 验证 SDK、`1<<25` intent、API facade 和当前榜图哈希。
- preflight 对平台侧权限、审核、机器人入群、主动消息许可输出 `UNKNOWN`，不得输出 OpenID/密钥。
- 生命周期事件使用 SDK 实际字段：群 `group_openid`，私聊 `openid`；仅持久化目标摘要与状态。
- `GROUP_DEL_ROBOT`/`GROUP_MSG_REJECT` 阻止目标群主动发送；`GROUP_ADD_ROBOT`/`GROUP_MSG_RECEIVE` 恢复资格。
- 调度器将配置/产物/权限异常归为 terminal，将网络、5xx、配额归为 retryable。
- 同日重启复用不可变 run 与逐图回执；第一张成功后第二张重试不得重发第一张。
- `赛事`、`我的报名`、`我的成绩`、`我的档案` 和“更多”分页具备官方 C2C 端到端离线夹具。
- 真实恢复只能由用户授权后的 QQ 沙箱或白名单群 canary 证明。

## Scope
- `plan/` 架构、关系状态、调度分类、preflight 证据、只读功能夹具和运维文档。
- 仅使用现有 JSON 临时状态目录，不改数据库 schema。

## Out of scope
- 登录真实 QQ、发送消息、修改开放平台/群配置、安装或启用 systemd。
- 写入真实 AppID/AppSecret/OpenID；修改正式名单、比赛数据或历史导出。
- 移除 OneBot、NapCat、NoneBot 或其 watchdog。

## UNKNOWN
- 应用审核及 `GROUP_AND_C2C_EVENT`、主动消息、富媒体权限。
- AppID/AppSecret、测试身份、目标群 OpenID、入群和主动消息接收状态。
- 平台真实审核/频控回执、正式发送时间、部署主机与 WebSocket 连通性。
