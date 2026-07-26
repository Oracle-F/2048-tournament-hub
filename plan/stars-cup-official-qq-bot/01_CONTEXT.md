# Context

## Control boundary
- 可直接控制：本地源码、`data/testing.db`、离线测试、公开资料核验、不可变导出、Git 分支与远端同步。
- 不可直接确认：官方应用审核/权限、真实凭据、群 OpenID、机器人关系状态、主动消息开关、真实频控和部署网络。
- 未经授权不得登录真实 QQ、发送消息、修改开放平台/群设置、启用服务或写入真实配置。
- 当前分支 `codex/stars-cup-bot-prep-20260727` 按小提交持续与远端同步；提交号以 `git log` 为准。
- 工作树有用户未跟踪文件；不得纳入提交、删除或覆盖。

## Implemented architecture
- `services/bot_transport.py`: 中立 DTO、业务分发、旧 CQ 图片归一化。
- `bot_private_qq/onebot_transport.py`: OneBot 事件/消息段与中立契约互转。
- `bot_private_qq/app.py`: 保留 NoneBot、NapCat lifecycle 和 OneBot 入口。
- `bot_official_qq/transport.py`: 官方 C2C/群 @ 映射、被动回复、主动群图和回执分类。
- `bot_official_qq/sdk_facade.py`: 隔离 `qq-botpy` 公开 API 与 2026 分片路由差异。
- `bot_official_qq/media_upload.py`: 本地文件 prepare/PUT/finish/merge，大小/hash/分片校验。
- `bot_official_qq/runtime.py`: 双开关、沙箱默认、短 SQLite 连接、WebSocket callback、离线 preflight。
- `bot_official_qq/scheduler.py`: gateway ready 后启动的进程内日榜调度器。
- `services/stars_cup_bot_service.py`: latest 快照查分，无网络、无数据库写。
- `services/stars_cup_daily_service.py`: 暂存缓存、不可变 run、原子 latest、逐图投递状态。
- `services/bot_private_service.py`: 复用旧绑定、报名、查分、预约、回放和管理员权限业务。

## Current data flow
1. OneBot 或官方事件被各自 adapter 转为 `BotInboundMessage`。
2. `dispatch_business_message` 调用现有私聊/群聊 handler。
3. 旧 string/CQ 回复转为 `BotReply`，再由对应 transport 发送。
4. 群星杯查询只读 `data/tmp/stars_cup_bot/latest_export.json` 指向的完整 run。
5. 日榜调度在 worker thread 查询/导出，异步逐图发送并原子写 delivery JSON。
6. OneBot 数字 ID 与官方 OpenID 以不同 `bot_platform` 命名空间保存，不做推断或迁移。

## Official capability matrix
| Existing behavior | Official capability | Project adaptation |
| --- | --- | --- |
| C2C 私聊 | `C2C_MESSAGE_CREATE`；被动窗口 60 分钟/4 次 | opaque `user_openid`、保留 `msg_id`/`msg_seq` |
| 群内命令 | `GROUP_AT_MESSAGE_CREATE`；被动窗口 5 分钟/5 次 | opaque `group_openid`、仅 @ 事件、沿用群门禁 |
| 主动群榜图 | 群主动消息；已验证机器人 60 qpm、单关系 20 qpm、1000/日/群；未验证 30 qpm | 两图两次发送、逐图回执、退避和终止分类 |
| 图片/文件 | scene-specific 上传后以 `msg_type=7` 发送；`file_info` 有时效 | C2C/群分别上传，本地文件走分片 facade |
| 引用/多回复 | `message_reference`、同一 `msg_id` 配不同 `msg_seq` | DTO 保存 reference，序号单调递增 |
| 事件订阅 | WebSocket 或 Webhook；`GROUP_AND_C2C_EVENT=1<<25` | 当前 WebSocket + `public_messages=True` |
| 关系变化 | 加/退群、接受/拒收主动消息、加/删好友 | 八类 callback 已接入；目标状态哈希持久化并门禁主动发送 |
| 审核/权限 | 特殊 intent 需平台授权；未授权可导致连接关闭 | preflight 只能验本地配置，平台项一律 `UNKNOWN` |

Official sources:
- https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html
- https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html
- https://bot.q.qq.com/wiki/develop/pythonsdk/
- https://github.com/tencent-connect/botpy
- https://github.com/tencent-connect/botpy/releases/tag/v1.2.1

## OneBot/NapCat coupling retained for rollback
- `bot_private_qq/app.py`: `OneBotV11Adapter`、matcher、event classes、`ActionFailed`、启动/关闭 hooks。
- `bot_private_qq/onebot_transport.py`: numeric IDs、message segments、file lookup、reply/@ 语义。
- `services/bot_private_service.py`: `patch_onebot_reply_lookup`、CQ 帮助图和 OneBot timeout fallback。
- `.env.bot.example` 与运维代码：OneBot token、NapCat 日志、watchdog、Windows alert/task。
- 以上不得进入官方 adapter；canary 验收前不清理。

## Verified local baseline
- 基础命令由历史 12 项增至 15 项并全部通过；Bot 47/47；快速全量 `TOTAL PASS`。
- 全量 unittest 218 项通过；`pip check` 通过。
- 实际安装 `qq-botpy==1.2.1` 的离线 preflight 已用当前 snapshot/两图通过。
- 官方路由已有群星杯、帮助图、绑定/解绑、报名/取消、预约、看板、Verse、成绩门禁、管理员、回放流程夹具。

## Remaining gates
- 离线实现缺口已按 `05_TODO.md` 完成；命名的旧功能均有官方事件链路证据。
- 平台审核、实际 Intent/富媒体权限、目标关系和真实回执只能由授权后的平台检查或 canary 证明。
- 正式发送时间、部署主机、服务账号、可写目录和持续 WebSocket 连通性仍需用户确认。
- 成绩提交保持默认关闭；是否在隔离测试赛事启用属于后续显式产品/权限决定。
