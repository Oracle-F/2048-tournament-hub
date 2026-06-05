# tests 目录约定

这份文件补充项目级约定，专门约束测试体系、快照样本和回放样本的维护方式。

## 默认原则

- 默认只使用 `data/testing.db`，不要碰正式数据。
- 不要并行跑多个会重建 `testing.db` 的入口。
- 新发现的重要 Bug，先修复，再补对应回归。
- 优先复用业务实现，不要在测试里重写业务规则。

## 维护顺序

优先补这几类回归：

1. `tests/cases`
2. `tests/bot_cases`
3. `tests/score_cases`
4. `tests/ranking_cases`
5. `tests/tournament_cases`
6. `tests/api_snapshots`
7. `tests/real_tournaments`（若仓库未随附样本，可视为可选）

## 运行规则

- `tests/run_all.py` 是总入口。
- `--fast` 只跳过最重的 1000 人模拟。
- 真实赛事快照和 API 快照属于长期维护资产，能复用就别重造。
- 新增案例时，优先把最小复现做成稳定 JSON 样本。

## 回归要求

- 规则回归要能稳定复现，不要依赖偶发数据。
- 真实赛事回放要尽量贴近原始记录。
- API 快照兼容性问题要优先用现有业务代码修，不要在测试层硬凑。
- 模拟压力验证只做必要覆盖，不要为了跑分而改业务口径。
