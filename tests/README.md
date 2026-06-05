# 基础测试体系

这套测试体系使用最轻量的方式维护：

* 测试案例统一使用 JSON
* 不引入 pytest 或其他重框架
* 所有测试固定使用 `data/testing.db`
* 正式数据库路径和测试数据库路径通过配置分离
* 未来发现的重要 Bug，修复后可以直接补成新的案例

## 目录结构

* `cases/`：历史 Bug 案例库
* `score_cases/`：计分逻辑相关案例
* `ranking_cases/`：排名与榜单相关案例
* `tournament_cases/`：赛事结算与赛程相关案例
* `bot_cases/`：Bot 消息解析与分流相关案例
* `real_tournaments/`：已结束赛事的真实快照，可用于结算回放
* `api_snapshots/`：第三方接口快照，可离线验证解析兼容性，支持按变体拆分
* `scripts/`：测试加载器和辅助脚本
* `run_all.py`：一键运行全部测试
* `simulate_tournament.py`：赛事模拟器

## 运行方式

在项目根目录下运行：

```bash
python tests/scripts/run_cases.py
```

只跑部分案例可以加过滤参数：

```bash
python tests/scripts/run_cases.py --pattern ranking
```

跑完整测试套件：

```bash
python tests/run_all.py
```

这个入口会依次跑历史 Bug、计分、排名、赛事、真实赛事回放、API 快照和模拟器。

快速跑一版完整套件，跳过最重的 1000 人模拟：

```bash
python tests/run_all.py --fast
```

跑比赛模拟器：

```bash
python tests/simulate_tournament.py
```

快速跑模拟器：

```bash
python tests/simulate_tournament.py --fast
```

导出真实赛事快照：

```bash
python tests/scripts/export_tournament_case.py 9
```

回放真实赛事快照：

```bash
python tests/scripts/replay_tournament.py
```

运行 API 快照自检：

```bash
python tests/scripts/api_snapshot_runner.py
```

## 案例格式

每个 JSON 案例都应包含：

* `name`
* `description`
* `input`
* `expected`

### `input` 建议字段

* `target`：要执行的目标
* `args`：目标函数参数
* `seed_sql`：建库后要执行的测试数据 SQL

### `expected`

* 直接写目标返回的 JSON 结果
* loader 会做精确比对

## 数据库隔离

测试加载器会：

* 使用固定的 `data/testing.db`
* 初始化 schema
* 执行基础 bootstrap
* 再执行案例自己的 `seed_sql`

正式数据库不会被测试直接写入。

当前第一版统一使用 `data/testing.db` 作为固定测试库，所以不要同时并行运行多个测试入口；顺序执行 `run_all.py`、`run_cases.py` 和 `simulate_tournament.py` 没问题。

真实赛事快照和 API 快照也默认走测试环境：

* 真实赛事快照从正式库导出，落到 `tests/real_tournaments/`
* 回放时会重建赛事、导入原始成绩、生成排名、计算积分并执行结算
* API 快照放在 `tests/api_snapshots/`
* 启用 API 快照时通过 `VERSE_API_SNAPSHOT_DIR` 显式指定目录，不影响默认线上调用
* 当前 API 快照文件名支持按 `variant` 和 `username` 选择更具体的样本，通用文件仍会作为兜底

## 新增历史 Bug 案例

未来如果发现了重要 Bug，建议按下面的方式沉淀：

1. 先把 Bug 的最小复现步骤整理成一个 JSON 案例。
2. 按问题类型放到 `cases/` 或对应目录。
3. 修复代码后保留这个案例，作为回归测试。

这样案例库会逐渐变成项目的“历史 Bug 回放集”。
