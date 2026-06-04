# 赛事中台

2048 赛事全流程中台，覆盖建赛、报名、提交、审核、结算、导出、锁局、评分与 Verse 查询。

## 入口

- [docs/README.md](docs/README.md): 文档总入口
- [tests/README.md](tests/README.md): 测试总入口
- [docs/agent_handoff.md](docs/agent_handoff.md): 新接手说明

## 当前维护原则

- 测试默认只用 `data/testing.db`
- 不并行跑会重建 `testing.db` 的入口
- 真实赛事快照、API 快照和历史案例优先复用
- 发现重要 Bug 先修复，再补回归样本

## 主要目录

- `services/`: 业务逻辑
- `bot_private_qq/`: 私聊与群聊 Bot 入口
- `tests/`: 回归、快照、回放与模拟体系
- `docs/`: 维护文档、事故记录、历史问题库
- `data/`: 测试库、帮助图、证据与运行数据
