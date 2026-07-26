# 开发者指南

本文档面向维护者，记录项目的通用开发约定和改动原则。

## 工作原则

1. 稳定性优先
2. 测试覆盖优先
3. 数据兼容性优先

## 修改行为时

- 尽量保持现有架构稳定，不主动扩大模块边界。
- 优先做最小改动，避免无关重构。
- 如果涉及数据结构或持久化格式，先说明影响范围，再决定是否扩展。
- 新增功能时，必须说明：
  - 不做的后果
  - 实现复杂度
  - 优先级

## 代码与配置

- 保持业务逻辑优先复用，能沿用现有实现时不要重写。
- 涉及外部配置、密钥或本地环境变量时，只保留本地示例，不要提交真实值。
- 如果某次变更会影响测试、回放或快照，先确认兼容路径，再改实现。
- Bot 连接状态会写到 `data/tmp/bot_connection_state.json`，`scripts/watch_bot_connection.py` 可作为外部 watchdog 轮询该状态并按任务名重启 NapCat。
- 如果需要观察 NapCat 控制台输出，可用 `scripts/run_napcat_with_log.ps1` 将 `napcat.bat` 输出落到 `data/tmp/napcat_runtime.log`；`-ConsoleLike` 可保留可视终端，同时通过 `Tee-Object` 写入 runtime log，供 watchdog 识别 `KickedOffLine` / 登录失效 / 风控下线 这类硬告警。普通 `账号状态变更为离线` 只更新状态，不直接弹窗。
- `scripts/watch_bot_connection.py` 会在日志命中硬告警时压住自动重启，优先提醒人工处理；只有普通离线且满足重启条件时才会重启 NapCat。
- `BOT_SUBMIT_SCORE_ENABLED` 默认应保持 `false`；只有确认现场需要直提交流程时才临时开启，避免成绩入口绕过审核或误触发高风险提交。
- `BOT_PRIVATE_DEBUG_LOG_ENABLED` 和 `BOT_GROUP_DEBUG_LOG_ENABLED` 默认都应保持 `false`；只有现场排查对应入口时才临时开启，避免长期记录私聊原文、用户 ID 或群调试轨迹。
- 旧的 `C:\napcat\napcat_watch.ps1` 只负责端口探活，属于 legacy；新 watcher 已经接管自动重启和持续异常提醒，后续维护优先改新链路。
- 如果要把 NapCat 启动也纳入新链路，可用 `scripts/setup_napcat_logged_autostart.ps1` 注册带日志的启动任务，再让 `scripts/setup_bot_watchdog.ps1` 接管健康检查。
- 当前开机入口是 `scripts/start_eventscore_stack.ps1` 对应的 Startup 快捷方式；它会先起 bot，再直接调用 `scripts/run_napcat_with_log.ps1 -ConsoleLike -HideLauncherWindow`，让正常开机尽量静默。不要同时保留 `EventScore-NapCat-Autostart` / `EventScore-QQBot-Autostart` 这类旧登录任务，否则会形成双启动源。
- `start_eventscore_stack.ps1` 和 `run_napcat_with_log.ps1` 都会检查 `NapCatWinBootMain` 是否已运行；默认发现现有进程就跳过，只有明确传入 `-ForceLaunch` 才允许绕过底层单实例门禁。
- 如果需要手动观察 NapCat 控制台，直接运行 `scripts/run_napcat_with_log.ps1 -ConsoleLike`；自动启动链路默认保持隐藏。
- `eventscore_napcat_launch.ps1` 仍然保留给旧的任务入口或手工复用；如果继续通过它启动，记得在需要静默时传 `-HideLauncherWindow`。
- `scripts/setup_bot_watchdog.ps1` 在本机有 `pythonw.exe` 时会优先用它注册 watchdog，这样 watchdog 本身不应闪窗，只在真异常时通过 popup-script 弹窗。
- `eventscore_napcat_watchdog.ps1` 是 watchdog 的短入口，任务计划程序里可直接指向它，再由它调用 `scripts/watch_bot_connection.py`。
- `scripts/setup_napcat_logged_autostart.ps1` 会优先自动寻找 `C:\napcat\NapCat.44498.Shell\napcat.bat`，找不到再尝试 `C:\napcat\bootmain\napcat.bat`；如果这两个路径都不存在，再手动传 `-NapCatBatPath`。

## 服务器迁移前检查

- 当前服务器准备状态（2026-07-11）：已创建 Fedora Server 虚拟机 Oracle_F，当前配置为 2 vCPU、3 GiB 内存、15 GiB 磁盘。该规格可以用于安装系统和做第一轮 Bot 部署验证；正式长期运行建议把内存调整到 4 GiB、磁盘调整到 20 GiB，至少为日志、SQLite 数据和回滚留出余量。
- 当前部署取向暂定为：Bot 服务优先使用 Podman 管理，代码、Python 依赖和运行进程与宿主机隔离；不把 NapCat/QQ 桌面运行时未经验证地打进 Fedora 容器。NapCat 在服务器上的运行方式仍需先确认目标环境是否支持其实际运行时；如果必须依赖 Windows 图形环境，应将 NapCat 与 Bot 的 OneBot 连接视为两个独立部署单元。
- 不建议为了迁移而改成 RPM 安装包。RPM 更适合已经确定宿主机原生服务边界、升级和卸载流程的长期发行版集成；当前项目仍处于迁移验证阶段，Podman 的可回滚、隔离和目录挂载更符合低风险目标。除非目标服务器明确要求原生 RPM，否则先不增加 RPM 打包维护面。
- 资源基线：Bot 单独运行建议至少 2 GiB 内存、1 vCPU；推荐 4 GiB 内存、2 vCPU。磁盘至少预留 5 GiB，推荐 10 GiB；如果保留较多日志、备份和审计文件，预留 20 GiB。资源不足时优先减少日志和备份保留量，不要通过提高 watchdog 重启频率来掩盖资源问题。
- 必须持久化并纳入备份的内容：data/tournament_hub.sqlite3、data/tmp/ 中的运行日志和连接状态、.env.bot.secret 或服务器上的等价 secret 文件，以及后续实际配置的 Bot/NapCat 状态目录。容器销毁或重建后，SQLite、登录态、状态文件和日志不能依赖容器可写层。
- 服务器初次部署顺序：先只启动 Bot 和 OneBot 连接探活，确认依赖导入、端口、状态文件和日志可写；再验证 NapCat 登录态与连接稳定性；最后才逐步开启白名单群聊。迁移首轮保持 GROUP_CHAT_ENABLED=false，不要直接放开大群。


- 在服务器上优先准备独立 Python 环境，并确认 `fastapi`、`nonebot`、`nonebot-adapter-onebot` 等 Bot 依赖可导入；不要依赖本机 `run_bot.bat` 里的个人目录 fallback。
- 迁移 `.env.bot.example` 后再按服务器实际值写入本地 secret/env，不要提交真实 token、QQ 登录密码或 webhook 凭据。
- 保持 `NAPCAT_RUNTIME_LOG_PATH`、`BOT_CONNECTION_STATE_PATH` 可写；watchdog 的异常判断依赖这两个文件。
- 如果服务器没有图形终端，NapCat 启动应走纯日志模式；如果仍需要可视窗口，保留 `-ConsoleLike`，确保输出继续 tee 到 runtime log。
- 正式开放群聊前确认 `GROUP_CHAT_ENABLED`、`GROUP_CHAT_WHITELIST`、群限流和长回复重定向阈值，避免迁移后误入大群刷屏。

### 迁移完成判定

- Bot 进程可由 Podman 启停，重建容器后 SQLite、secret、日志和连接状态仍然存在。
- OneBot WebSocket/HTTP 连接可稳定建立，普通离线不会导致频繁重启，KickedOffLine、登录失效和风控下线会转为人工处理。
- 服务器重启后只产生一个 NapCat/QQ 实例或一个明确的 NapCat 运行单元，不存在重复登录源。
- 在私聊和自测群完成低频消息验证后，再按白名单逐步恢复群聊；提交成绩和调试日志仍保持默认关闭。

## 推荐落地顺序

1. 注册 NapCat 带日志启动任务：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/setup_napcat_logged_autostart.ps1
   ```

2. 注册连接 watchdog：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/setup_bot_watchdog.ps1 -NapCatTaskName "EventScore-NapCat-Autostart"
   ```

3. 如仍保留旧的 `C:\napcat\napcat_watch.ps1` 任务，可以先停用，避免和新链路重复提醒。

## 文档分层

- 主页 `README.md` 只放对外介绍和入口链接。
- 维护说明、测试细节和操作约定分别放到专门文档中。
