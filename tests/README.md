# 测试目录

`tests/` 下保存项目的回归、快照、回放与模拟相关资源。

## 入口

- 总入口：`python tests/run_all.py`
- 快速模式：`python tests/run_all.py --fast`
- 单项案例：`python tests/scripts/run_cases.py`
- 模拟器：`python tests/simulate_tournament.py`

## 详细说明

- [测试说明](../docs/testing.md)

## 目录索引

- `cases/`：历史 Bug 案例库
- `score_cases/`：计分逻辑相关案例
- `ranking_cases/`：排名与榜单相关案例
- `tournament_cases/`：赛事结算与赛程相关案例
- `bot_cases/`：Bot 消息解析与分流相关案例
- `real_tournaments/`：真实赛事回放样本目录，公开版可能不随仓库一起提供样本文件
- `api_snapshots/`：第三方接口快照，可离线验证解析兼容性
- `scripts/`：测试加载器和辅助脚本
