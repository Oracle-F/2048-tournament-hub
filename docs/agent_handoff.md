# Agent 交接说明

本文档给后续接手的 Agent 看的。目标不是讲代码怎么写，而是告诉你：

* 先看什么
* 现在要盯什么
* 哪些问题已经结束
* 接下来应该怎么继续往文档里写

## 先看顺序

1. `docs/README.md`
2. `docs/index_map.md`
3. `docs/business_map.md`
4. `docs/maintenance_rules.md`
5. `docs/bug_history.md`
6. `docs/bug_archive.md`
7. `docs/incident_timeline.md`
8. `docs/项目现状总结_供新对话.txt`
9. `tests/README.md`

## 当前仍需重点关注的问题

* Bot刷屏与账号风控
  * 风险最高。
  * 目前维持保守策略，不建议恢复高风险群聊能力。
* NapCat偶发 Kick Offline / WebSocket 抖动
  * 仍可能影响消息链路与响应时延。
  * 需要继续观察 reply、发送超时和 WebSocket 抖动。
* SQLite锁竞争
  * 已缓解，但在高压刷新和 bot 并发场景下仍要留意。

## 当前推荐的最小安全配置

这是一版“先保稳定、再谈恢复”的基线。后续如果要放开能力，建议先从这里逐项回滚。

* 群聊只对白名单群开放
  * `GROUP_CHAT_ENABLED=true`
  * `GROUP_CHAT_WHITELIST=524118799`
  * `GROUP_CHAT_RATE_LIMIT_PER_MINUTE=1`
  * `GROUP_CHAT_MAX_REPLY_CHARS=180`
  * `GROUP_CHAT_TO_ME_FALLBACK_ENABLED=false`
* 保留低风险查询，关闭高风险写入
  * `BOT_HELP_IMAGE_ENABLED=false`
  * `BOT_LOCK_UPLOAD_ENABLED=false`
  * `BOT_SUBMIT_SCORE_ENABLED=false`
  * `MY_SCORE_LOCK_REFRESH_ON_QUERY=false`
  * `MY_SCORE_PLAYER_LOCK_REFRESH_ON_QUERY=false`
* Discord token 不再放在主 `.env`
  * 放在独立的 `赛事中台/.env.bot.secret`
  * `scripts/run_private_qq_bot.py`、`scripts/sync_discord_game_review.py`、`services/discord_lock_refresh_service.py` 都已改为读取该文件
  * `.gitignore` 已忽略 `.env.bot.secret`

## 当前已归档的问题

* 特殊计分逻辑边界漏洞
* 测试赛长期残留导致数据库脏数据
* Codex Windows app 旧入口失效：更新后任务栏/应用注册异常导致 `Error launching app`，备份 `C:\Users\oracl\.codex` 后重置恢复，历史对话未丢
* QQ 私聊 bot 启动依赖缺失：启动脚本依赖的 Python 环境缺少 `nonebot`，已统一安装依赖并修正启动入口

这些问题已归档，后续若出现新证据再考虑从 archive 提回 history。

## 当前维护节奏建议

* 先保稳定，再谈扩功能。
* 新功能优先私聊轻命令验证，群聊保持保守。
* 需要回归验证时，先看 [tests/README.md](../tests/README.md)，再补新的 JSON 案例。
* 发现事故后先更新 `bug_history.md`，不要急着写进 archive。
* 只有确认已稳定并连续观察未复现，才迁移到 `bug_archive.md`。

## 写文档时怎么写

### 新增问题

把它写进 `bug_history.md`，并补齐这些字段：

* 模块
* 风险等级
* 现象
* 触发条件
* 根因
* 修复
* 当前状态
* 相关文件
* 备注

如果是高风险问题，再加上：

### 复盘

* 问题扩大原因
* 当时排查过程
* 未来预防措施

### 归档问题

当问题已解决、连续观察未复现、无需继续关注时：

* 从 `bug_history.md` 移除
* 迁移到 `bug_archive.md`
* 保留简洁记录，不再保留详细复盘

## 你接手后的第一判断

如果你现在是新对话的 Agent，最先判断的是：

* 这个问题是“现在还在发生”，还是“已经结束”
* 它属于高风险、观察中，还是已归档
* 是否应该更新现有条目，而不是新增一条重复记录

## 最后提醒

这个项目历史记录的目标不是“记得越多越好”，而是：

* 当前问题能被持续盯住
* 已解决问题能被可靠归档
* 后续 Agent 能快速接手，不用重新猜历史
