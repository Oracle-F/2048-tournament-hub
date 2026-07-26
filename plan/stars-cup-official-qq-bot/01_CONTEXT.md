# Context

## Control boundary
- 当前可直接控制本地源码、测试、Git 本地分支、公开文档和离线导出。
- 当前不可确认官方应用的 AppID、权限、审核状态、群 OpenID、主动消息开关或部署回调。
- 未经用户授权不得登录 QQ、发真实消息、修改开放平台、群设置或系统定时任务。
- Git 本地快照已提交为 `88b7be7`；HTTPS 远端缺少可用凭据，尚未推送。

## Existing modules
- `bot_private_qq/app.py`: NoneBot 启动、OneBot v11 事件识别、文件补全、CQ 图片渲染和发送。
- `services/bot_private_service.py`: 私聊/群聊业务、白名单、限流、绑定、查分、流程状态和帮助。
- `services/verse_query_service.py`: 通用 Verse 查询解析、网络读取和回复缓存。
- `services/bot_binding_service.py`: 按 `bot_platform + bot_user_id` 保存绑定，支持新增 `qq_official` 而不改表。
- `event_hub.py`: 群星杯名单、Verse 增量缓存、完整性保护和榜图导出。
- `services/match_rank_image_service.py`: 快照校验、Top3/队伍计算和两张 1920×1080 PNG 渲染。
- `scripts/export_match_rank_images.py`: 通用快照或数据库导出入口。
- `services/bot_connection_watchdog.py`: NapCat/OneBot 专用运维，不复用于官方适配器。

## Current call relationships
- OneBot event → `bot_private_qq/app.py` → `handle_private_message` 或 `handle_group_message` → service → string/CQ reply → OneBot send。
- 群星杯抓取 → `event_hub.query_live_scores` → Verse → `data/tmp/annual_4x4_2026_live_cache.json`。
- 群星杯导出 → `event_hub.export_rank_images` → `build_snapshot_from_roster_records` → `render_match_rank_images`。
- 渲染输出 → 时间戳目录内 `总榜.png`、`六队明细.png`、`榜图数据.json`。

## Official capability matrix
| Existing behavior | Official support | Required adaptation |
|---|---|---|
| 私聊文字 | Supported by `/v2/users/{user_openid}/messages` | Numeric QQ ID becomes opaque user OpenID |
| 群内 @ 文字 | Supported by `GROUP_AT_MESSAGE_CREATE` | Requires special `GROUP_AND_C2C_EVENT` intent |
| 主动群消息 | Supported | Respect opt-out, 20/qpm/group and 1000/day |
| 被动群回复 | Supported for 5 minutes, 5 replies/message | Preserve `msg_id` and increment `msg_seq` |
| 私聊回复 | Supported for 60 minutes, 4 replies/message | Preserve `msg_id` and increment `msg_seq` |
| 引用消息 | Supported through `message_reference.message_id` | Do not model as OneBot reply segment |
| 图片/文件收发 | Supported through attachments and `msg_type=7` | Upload per C2C/group scope; `file_info` has TTL |
| 事件接收 | Webhook and WebSocket supported | First adapter uses WebSocket; no public callback required |
| 消息审核结果 | `MESSAGE_AUDIT` exists | Log result; never retry rejected content unchanged |

Official sources:
- https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html
- https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html
- https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html
- https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html
- https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html
- https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html

## OneBot/NapCat coupling
- Entry: `OneBotV11Adapter`, OneBot event classes, matcher lifecycle and `ActionFailed` are imported in `bot_private_qq/app.py`.
- Input model: `event.get_user_id`, `event.group_id`, OneBot message segments and file lookup APIs are read directly.
- Addressing: numeric QQ/group IDs and current whitelist semantics are assumed.
- Output model: service help images return `[CQ:image,file=...]`; app parses CQ and builds OneBot `MessageSegment`.
- Reply semantics: @ detection, reply segment checks and `to_me` fallback are OneBot-specific.
- Error handling: OneBot timeout/connection messages and reply lookup monkey patch live in business service/app.
- Operations: heartbeat, lifecycle, NapCat log watcher, Windows startup and QR login recovery are OneBot-only.

## Dependencies
- Existing runtime: `nonebot2`, `nonebot-adapter-onebot`, `websockets`, `python-dotenv`, `Pillow`.
- Official reference SDK: Tencent `tencent-connect/botpy`; add only when official adapter phase begins.
- Daily single-run task uses existing Python standard library, `event_hub.py` and Pillow; no scheduler package.
- Scheduler remains external to application code; enabling it is an authorized deployment step.

## Database impact
- No schema or migration.
- Existing `bot_account_bindings` can store official users with `bot_platform=qq_official`.
- OpenID cannot be inferred from old numeric QQ IDs; official users must bind again with the existing PIN flow.
- Daily run/delivery state is JSON under `data/tmp/stars_cup_bot/`, not SQLite.

## Test baseline
- Basic unittest group: 12/12.
- Fast project suite: `TOTAL PASS`.
- Bot cases: 43/47; four failures are expected Windows path strings versus Fedora separators.
