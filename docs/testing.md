# 测试说明

本文档描述项目当前的测试体系和默认运行约定。

## 默认约定

- 测试默认只使用 `data/testing.db`。
- 不要把测试入口切到正式数据。
- 不要并行运行多个会重建 `testing.db` 的入口。

## 主要入口

- 总入口：`python tests/run_all.py`
- 快速模式：`python tests/run_all.py --fast`
- 单项案例：`python tests/scripts/run_cases.py`
- 模拟器：`python tests/simulate_tournament.py`

## 测试目录

- `tests/cases/`：历史 Bug 与基础回归案例
- `tests/score_cases/`：计分逻辑相关案例
- `tests/ranking_cases/`：排名与榜单相关案例
- `tests/tournament_cases/`：赛事结算与赛程相关案例
- `tests/bot_cases/`：Bot 消息解析与分流相关案例
- `tests/api_snapshots/`：第三方接口快照
- `tests/real_tournaments/`：真实赛事回放样本目录，公开版可能不随仓库提供样本文件

## 新问题回归

发现重要 Bug 时，按下面顺序处理：

1. 先修复代码
2. 再补对应的 `tests/cases/` 或 `tests/bot_cases/` 案例
3. 如有需要，再补快照或回放样本

## 快照与回放

- 真实赛事快照和 API 快照是长期资产。
- 复用现有业务逻辑，优先保证解析和兼容，而不是重写计分、排名或结算逻辑。
- 如果公开版没有某些样本，相关测试应当能安全跳过。

## 建议

- 修完测试后，优先跑总入口验证整体稳定性。
- 只有在确认不是最重的模拟压力路径时，才使用 `--fast` 作为快速检查。
