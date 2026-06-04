# 业务地图

## 1. 项目核心目标

- 建赛
- 报名
- 提交
- 审核
- 结算
- 导出
- 锁局
- 评分
- Verse 查询

## 2. 主办方工作台

主办方工作台主要入口：

- `计分器/organizer_event_hub.py`

它负责：

- 建赛
- 报名管理
- 成绩模块
- 排名查看
- 结算
- 手动刷新 Verse 成绩

## 3. QQ bot 入口

QQ 侧主要入口：

- `bot_private_qq/app.py`
- `scripts/run_private_qq_bot.py`

它负责：

- 私聊命令
- 群聊低风险查询
- bot 私聊绑定
- bot 私聊提交/查询
- 低风险消息链路处理

## 4. 数据目录

- `data/testing.db`：测试数据库
- `data/evidence/`：回放证据
- `data/bot_help_images/`：help 图
- `data/discord_review_files/`：Discord 回放文件
- `data/switch_backups/`：切号备份

## 5. 维护文档

- `docs/bug_history.md`
- `docs/bug_archive.md`
- `docs/incident_timeline.md`
- `docs/agent_handoff.md`
- `docs/maintenance_rules.md`

## 6. 回归测试目录

- `tests/cases/`：历史 Bug 案例
- `tests/score_cases/`：计分口径案例
- `tests/ranking_cases/`：榜单与排序案例
- `tests/tournament_cases/`：赛事、报名、结算案例
- `tests/bot_cases/`：bot 消息解析和回归案例
- `tests/real_tournaments/`：真实赛事回放快照
- `tests/api_snapshots/`：API 快照

## 7. 业务说明顺序

如果要快速理解系统，建议依次看：

1. `docs/README.md`
2. `docs/agent_handoff.md`
3. `tests/README.md`
4. `docs/business_map.md`
5. `docs/index_map.md`
6. `docs/项目现状总结_供新对话.txt`

## 8. 新增业务文档时的原则

- 一个文档只负责一个职责。
- 当前状态和历史归档分开。
- 规则说明和操作清单分开。
- 能用索引串起来的，不要重复写在多个地方。
