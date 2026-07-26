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
- `bot_official_qq/` 目前是离线适配层和测试门面。导入它不会读取
  AppID/AppSecret、连接 WebSocket、登录 QQ 或发送消息；真实运行入口尚未启用。
- 官方稳定 PyPI 包 `qq-botpy` 与 2026-07 官方接口存在版本差距，因此当前
  不在 `requirements-bot.txt` 中锁定它。分片上传差异集中封装在
  `bot_official_qq/sdk_facade.py`，待沙箱授权时再做版本和兼容性冒烟测试。

官方能力依据：

- [QQ 机器人消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)
- [发送群聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)
- [群聊富媒体上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_files.post.html)
- [群聊富媒体预上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_id_upload_prepare.post.html)

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
  tests.test_official_qq_sdk_facade
```

查看单次每日任务参数：

```bash
./.venv/bin/python scripts/run_stars_cup_daily.py --help
```

真正执行该命令会读取 Verse 并写入本地 cache、不可变榜图目录和 latest
指针，但仍不会发送 QQ 消息。正式运行前应先确认名单、截止时刻、背景图和
当前缓存路径。

## 上线与回滚门

只有在用户明确提供并确认官方平台权限、测试机器人、允许的群 OpenID 和
发送时刻后，才添加真实运行入口并进行沙箱/白名单群冒烟。上线时一次只启用
一个传输；任何审核、权限、拒收、配额或未知回执都停止自动重试并保留状态。

回滚不涉及数据库或计分数据迁移：停止官方运行入口和外部定时器，继续使用
原 OneBot 入口；保留最后有效 latest 指针、不可变榜图和逐图投递回执。
