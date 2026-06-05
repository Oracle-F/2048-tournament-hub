# 文档地图

本文档不是业务说明书，而是给后续 Agent 和维护者看的“快速定位图”。

它解决两个问题：

* 我现在要处理什么，应该先看哪些文档
* 新文档以后应该挂到哪里，怎么保持体系不乱

## 先看入口

如果你刚接手项目，优先顺序建议是：

1. [README.md](README.md)
2. [agent_handoff.md](agent_handoff.md)
3. [maintenance_rules.md](maintenance_rules.md)
4. [bug_history.md](bug_history.md)
5. [bug_archive.md](bug_archive.md)
6. [incident_timeline.md](incident_timeline.md)
7. [business_map.md](business_map.md)
8. 最近已归档事故：QQ 私聊 bot 启动依赖缺失
   * 启动脚本依赖的 Python 环境缺少 `nonebot`，已统一安装依赖并修正启动入口。
9. 当前最小安全配置
   * 群聊白名单仅保留 `524118799`，并收紧到 `1` 次/分钟限流。
   * `submit/floor/finish` 继续关闭，Discord token 已移入 `.env.bot.secret`。

## 按问题类型找文档

### 现在有事故或风险要处理

* 先看 `bug_history.md`
* 如果是某月的事故细节，再看 `incident_timeline.md` 和对应月度文件
* 如果是已经结束的问题，再看 `bug_archive.md`

### 要判断怎么写、怎么归档

* 看 `maintenance_rules.md`
* 这里定义了什么时候写 history、什么时候写 archive、什么时候只更新不新增

### 要快速接手当前状态

* 看 `agent_handoff.md`
* 再补读 `bug_history.md`
* 如果要看业务脉络，再查 `business_map.md`
* 最后查 `项目现状总结_供新对话.txt`

### 要看事故的时间脉络

* 看 `incident_timeline.md`
* 再进入对应月度文件，例如 `incident_timeline/2026-05.md`

### 要看业务设计或操作流程

* 先看 `business_map.md`
* 再按需要进入具体业务文档

### 要看风控和应急

* 看 `QQ账号风控止损与低风险恢复SOP_2026-05-30.txt`
* 看 `QQ账号应急切换SOP_2026-05-27.txt`

## 文档分层

### 维护层

* `bug_history.md`
* `bug_archive.md`
* `maintenance_rules.md`
* `agent_handoff.md`
* `incident_timeline.md`
* `business_map.md`
* `incident_timeline/YYYY-MM.md`
* [tests/README.md](../tests/README.md)

### 业务层

* `项目现状总结_供新对话.txt`
* `设计说明.txt`
* `比赛模式扩展说明.txt`
* `rating与长期统榜说明.txt`
* `2048verse锁局评估.txt`
* `比赛操作清单.txt`
* `命令示例.txt`
* `QQ私聊bot接入说明.txt`
* `3x4模式接入清单.txt`
* `QQ账号风控止损与低风险恢复SOP_2026-05-30.txt`
* `QQ账号应急切换SOP_2026-05-27.txt`
* 成绩模板 CSV

## 新文档怎么挂

以后如果新增维护类文档，建议先问自己它属于哪一层：

* 事故和风险 -> `bug_history.md`、`bug_archive.md`、`incident_timeline.md`
* 写法和规则 -> `maintenance_rules.md`
* 新对话接手 -> `agent_handoff.md`
* 业务说明 -> `business_map.md`
* 回归测试和案例库 -> [tests/README.md](../tests/README.md)
* 新主题文档 -> 先挂到 `README.md`，再补到这里

## 维护原则

* 不让单文件无限膨胀。
* 不把不同职责混在一个文档里。
* 索引要短，细节要分层。
* 新 Agent 先看入口，再看地图，再看具体文件。
