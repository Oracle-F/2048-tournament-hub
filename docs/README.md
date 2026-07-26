# 文档目录

`docs/` 里放项目对外可见、适合公开的参考资料。

## 入口

- [项目主页](../README.md)
- [开发者指南](developer_guide.md)
- [测试说明](testing.md)
- [2026-07 比赛项目计划与统榜设计](比赛项目计划与统榜设计.md)

## 公共文件

- `Stone成绩模板.csv`：示例成绩模板
- `经典4x4成绩模板.csv`：示例成绩模板

## 说明

- 只保留公开安全、范围清晰的文档与模板。
- 维护说明和测试规则已经拆到独立文档中，不再放在主页 README。

## 群星杯 QQ Bot 当前边界

- OneBot/NapCat 入口仍保留，现有私聊、群聊和回滚路径未删除。
- 群星杯查分只读取 `data/tmp/stars_cup_bot/latest_export.json`
  指向的已校验不可变快照，不直接查询 Verse、赛事数据库或计分服务。
- `scripts/run_stars_cup_daily.py` 是单次执行命令：先在暂存缓存中查询，
  再生成并校验两张 1920×1080 PNG 与 `榜图数据.json`，全部成功后才更新
  latest 指针。该命令不启动定时器，也不发送 QQ 消息。
- `bot_official_qq/` 已包含离线适配层、受保护的 `qq-botpy` WebSocket
  运行入口和群星杯日任务调度器。普通导入和默认配置检查不会读取 dotenv
  文件、连接 WebSocket、登录 QQ 或发送消息。
- 官方运行入口需要 `OFFICIAL_QQ_BOT_ENABLED=true` 与命令行 `--start`
  同时满足；正式环境还需要第二个
  `OFFICIAL_QQ_BOT_PRODUCTION_CONFIRMED=true`。默认使用沙箱。
- 群星杯每日任务另有独立开关，默认关闭。它只在官方网关 ready 后启动，
  通过现有不可变导出、哈希校验和逐图投递状态发送两张图片；榜图生成或
  发送失败不会修改比赛数据库或阻塞 OneBot 路径。
- 官方群关系事件独立于消息业务处理：加群、退群、接受或拒收主动消息只
  更新 `data/tmp/stars_cup_bot/official_target_relationship.json`。
  该文件只保存目标 OpenID 的 SHA-256 摘要、状态、事件类型和时间；不会
  保存原始 OpenID，也不会打开赛事 SQLite。状态记录为退群或拒收时，日榜
  调度器会在查询、导出和发送前终止。
- 调度器启动后先核对当日逐图投递状态：两图均成功时不再导出或发送；
  `unknown`、发送中断和终态失败不盲重试；仅部分成功或可重试失败继续走
  现有哈希验证及逐图跳过逻辑。
- 官方稳定 PyPI 包 `qq-botpy==1.2.1` 锁定在独立的
  `requirements-official-qq.txt`，不会强加给 OneBot-only 环境。2026-07
  分片上传接口与 SDK 的版本差异继续集中在
  `bot_official_qq/sdk_facade.py`。

官方能力依据：

- [QQ 机器人消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)
- [发送群聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)
- [群聊富媒体上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_files.post.html)
- [群聊富媒体预上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_id_upload_prepare.post.html)
- [事件订阅与通知](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)
- [Python SDK 接入指南](https://bot.q.qq.com/wiki/develop/pythonsdk/)

## 离线验证

以下命令不连接真实 QQ：

```bash
./.venv/bin/python -m tests.scripts.case_runner --suite bot
./.venv/bin/python tests/run_all.py --fast
./.venv/bin/python -m unittest \
  tests.test_stars_cup_bot_service \
  tests.test_stars_cup_daily_service \
  tests.test_official_qq_transport \
  tests.test_official_qq_app \
  tests.test_official_qq_media_upload \
  tests.test_official_qq_sdk_facade \
  tests.test_official_qq_runtime \
  tests.test_official_qq_scheduler \
  tests.test_official_qq_entrypoint \
  tests.test_bot_attachment_service
```

查看单次每日任务参数：

```bash
./.venv/bin/python scripts/run_stars_cup_daily.py --help
```

真正执行该命令会读取 Verse 并写入本地 cache、不可变榜图目录和 latest
指针，但仍不会发送 QQ 消息。正式运行前应先确认名单、截止时刻、背景图和
当前缓存路径。

## 官方运行入口（默认不联网）

安装独立依赖：

```bash
./.venv/bin/pip install -r requirements-official-qq.txt
```

凭据必须由进程环境或服务管理器的私有 `EnvironmentFile` 注入；入口不会
自动读取 `.env` 或 `.env.bot.secret`。先执行只读检查：

```bash
./.venv/bin/python scripts/run_official_qq_bot.py --check-config
```

安装 SDK 后可执行更强的离线 preflight；它会构造并关闭真实 Client、核对
Intent 和 API facade。若群星杯定时开关已打开，还会重新验证 latest
快照与两张榜图哈希，但仍不登录或联网。输出中的
`platform_readiness` 会把应用审核、Intent 实际授权、富媒体权限和真实
回执保持为 `UNKNOWN`；本地 intent 数值通过不代表平台已经授权：

```bash
./.venv/bin/python scripts/run_official_qq_bot.py --preflight
```

只有获得授权后才执行真实入口：

```bash
./.venv/bin/python scripts/run_official_qq_bot.py --start
```

需要每日自动发送时，还需显式配置：

- `OFFICIAL_QQ_STARS_CUP_SCHEDULE_ENABLED=true`
- `OFFICIAL_QQ_STARS_CUP_GROUP_OPENID=<获准测试群或正式群 OpenID>`
- `OFFICIAL_QQ_STARS_CUP_SEND_TIME=HH:MM`（新加坡时区）
- `OFFICIAL_QQ_STARS_CUP_RELATIONSHIP_STATE_PATH=<可写的私有状态路径>`

调度器在指定时间后执行一次；同日重启会复用已发布导出和逐图投递状态。
配额、连接、超时或锁冲突按配置间隔重试；配置、契约、产物校验、权限拒绝、
关系拒收和未知回执等状态同日不盲重试。
Fedora systemd 的未启用模板见
`deploy/systemd/official-qq-bot.service.example`，其中路径和服务账号都是
占位符，当前没有安装、enable 或 start。

## 上线与回滚门

只有在用户明确提供并确认官方平台权限、测试机器人、允许的群 OpenID 和
发送时刻后，才执行真实入口并进行沙箱/白名单群冒烟。上线时一次只启用
一个传输；任何审核、权限、拒收或未知回执都停止自动重试并保留状态。

回滚不涉及数据库或计分数据迁移：停止官方运行入口及其内置调度器，继续使用
原 OneBot 入口；保留最后有效 latest 指针、不可变榜图和逐图投递回执。
