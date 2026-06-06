# 计分器

这是一套独立的 2048 赛事计分与看板工具，适合用来做比赛中的成绩预览、排行榜导出和主办方侧的赛事查看。

## 它做什么

- 拉取或整理比赛窗口内的成绩数据
- 计算选手侧展示结果和总排名
- 导出 HTML 看板与文本结果
- 给主办方提供一个独立的赛事管理入口

## 主要入口

- `tournament_common.py`：公共计分、导出和抓取逻辑
- `player_timed_scoring.py`：选手侧看板生成脚本
- `organizer_timed_scoring.py`：主办方侧看板生成脚本
- `organizer_event_hub.py`：主办方事件管理入口

## 运行方式

从仓库根目录运行：

```bash
python 计分器/player_timed_scoring.py
python 计分器/organizer_timed_scoring.py
python 计分器/organizer_event_hub.py
```

## 输出文件

- HTML 看板
- 导出的文本或图片结果
- 运行日志和状态文件

这些运行产物已经在仓库根 `.gitignore` 中忽略。

## 注意事项

- 默认会复用仓库里的业务代码和数据库配置。
- 不要把真实账号、密钥或私密比赛资料写进这里。
- 如果你只想看代码逻辑，可以直接从 `tournament_common.py` 和两个 timed scoring 脚本开始。
