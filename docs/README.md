# 文档总入口

这里是 `赛事中台` 项目的文档总入口，主要面向长期维护和 Agent 交接。

## 先看哪些

如果你刚接手这个项目，建议按下面顺序阅读：

1. [agent_handoff.md](agent_handoff.md)
2. [maintenance_rules.md](maintenance_rules.md)
3. [bug_history.md](bug_history.md)
4. [bug_archive.md](bug_archive.md)
5. [incident_timeline.md](incident_timeline.md)
6. [index_map.md](index_map.md)
7. [business_map.md](business_map.md)
8. `项目现状总结_供新对话.txt`

## 当前维护文档

### 问题与归档

* [bug_history.md](bug_history.md)
  * 只保留当前仍值得持续关注的问题。
* [bug_archive.md](bug_archive.md)
  * 保存已解决、已归档、无需继续重点关注的问题。
* 最近已归档：QQ 私聊 bot 启动依赖缺失
  * 启动脚本依赖的 Python 环境缺少 `nonebot`，已统一安装依赖并修正启动入口。
* 当前最小安全配置
  * 群聊只对白名单群 `524118799` 开放，限流 `1` 次/分钟，回复长度 `180` 字符。
  * `submit/floor/finish` 相关高风险写入口继续关闭，仅保留低风险查询与报名能力。
  * Discord token 已移出主 `.env`，改放到独立的 `.env.bot.secret`。

### 维护规则与交接

* [maintenance_rules.md](maintenance_rules.md)
  * 规定什么时候写、写到哪里、什么时候归档、怎么更新。
* [agent_handoff.md](agent_handoff.md)
  * 给新接手 Agent 的快速说明。

### 事故时间线

* [incident_timeline.md](incident_timeline.md)
  * 事故索引和拆分规则。
* [incident_timeline/2026-05.md](incident_timeline/2026-05.md)
  * 2026-05 月度事故时间线样板。
* [incident_timeline/2026-06.md](incident_timeline/2026-06.md)
  * 2026-06 月度事故时间线模板。
* [index_map.md](index_map.md)
  * 文档地图与阅读路径。

## 业务类文档入口

* [business_map.md](business_map.md)
  * 业务文档分类、阅读顺序和新增规则。

## 未来扩展约定

以后如果要新增别的维护类文档，建议优先挂到这里，并按主题分类：

* 稳定性类
* 数据与结算类
* Bot 与消息链路类
* 风控与账号类
* 事故复盘类
* 维护交接类
* 回归测试与案例库类

新增文档时尽量保持以下原则：

* 一个文档只负责一种职责。
* 索引文件保持短，细节分散到专门文件。
* 当前状态、归档历史、维护规则分开存放。
* 新 Agent 先看入口，再看规则，再看细节。

## 维护建议

* 如果某个文档开始明显变长，先考虑拆分，不要无限追加。
* 如果某类问题开始频繁出现，优先补到 `bug_history.md` 或对应的事故时间线里。
* 如果问题已经彻底结束，优先迁移到 `bug_archive.md`。
* 如果新增的是一类新的维护主题，先在这里加入口，再决定是否需要单独子文档。
* 如果新增的是回归测试或历史 Bug 案例，先看 [tests/README.md](../tests/README.md)，再决定要不要补到这里。
