# 计分器

这是一套独立的 2048 赛事计分与看板原型，保留在仓库里，方便后续复用和维护。

## 组成

- `tournament_common.py`：公共计分、导出和抓取逻辑
- `player_timed_scoring.py`：选手侧看板生成脚本
- `organizer_timed_scoring.py`：主办方侧看板生成脚本
- `organizer_event_hub.py`：主办方事件管理入口

## 运行注意

- 默认会复用仓库里的业务代码和数据库配置。
- 运行时生成的 HTML、日志和状态文件已在仓库根 `.gitignore` 中忽略。
- 不要把真实账号、密钥或私密比赛资料写进这里。
