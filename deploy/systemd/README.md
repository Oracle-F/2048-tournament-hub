# 官方 QQ Bot 部署准备

本目录只提供未安装的 systemd 与环境变量模板。下面的步骤用于准备部署
目录和执行离线检查；在获得明确授权前，不填写真实凭据或 OpenID，不安装、
enable、start 服务，也不连接真实 QQ。

## Git 与外部输入边界

从 GitHub 克隆当前分支只能得到代码、依赖清单和模板。以下正式运行输入
不由 Git 管理，必须由运维人员从已确认的可信副本单独放到部署主机：

| 输入 | 默认部署位置 | 用途 |
| --- | --- | --- |
| 正式 SQLite | `/opt/2048-event/赛事中台/data/tournament_hub.sqlite3` | 绑定、报名、查分等业务数据 |
| 群星杯正式名单 | `/opt/2048-event/赛事中台/config/annual_4x4_2026_roster.json` | 固定 6 队 72 人及比赛时间窗 |
| 榜图背景 | `/opt/2048-event/比赛导出/设计草图/统榜_共享背景_群星杯与尚竞之勇_v8_1920x1080.png` | 每日两张 1920×1080 榜图 |
| latest 指针与不可变榜图 | `/opt/2048-event/赛事中台/data/tmp/stars_cup_bot/` 与 `/opt/2048-event/比赛导出/正式榜图/群星杯_2026/` | 启动前哈希校验和查分/发送 |

不要用 `config/annual_4x4_2026_roster.template.json` 代替正式名单，也不要把
`data/testing.db` 当作生产数据库。正式名单、数据库、历史导出和真实环境文件
不得提交到 Git。

## 授权前可完成的准备

1. 将仓库部署到 `/opt/2048-event/赛事中台`，固定到已经审查的提交。
2. 在该目录创建虚拟环境，并安装 `requirements-official-qq.txt`；该文件
   已通过 `-r requirements-bot.txt` 包含共用 Bot 依赖。
3. 创建 `eventbot` 服务账号；让它可读正式名单和背景，可读写 SQLite 文件
   及其父目录，并可写 `data/tmp/` 和 `/opt/2048-event/比赛导出/`。
4. 单独传入上表中的正式输入。传输后核对来源、所有者、权限和文件哈希；
   不在终端记录或工单中粘贴数据库内容、OpenID 或凭据。
5. 保持环境副本中的 `OFFICIAL_QQ_BOT_ENABLED=false` 和
   `OFFICIAL_QQ_STARS_CUP_SCHEDULE_ENABLED=false`。

此时可以运行现有离线测试，并用一次性环境值检查全部本地输入。这个动作
不要求启用标志、AppID、AppSecret 或 OpenID，不导入 SDK，不读取 SQLite
内容，也不创建探针文件：

```bash
OFFICIAL_QQ_DATABASE_PATH=/opt/2048-event/赛事中台/data/tournament_hub.sqlite3 \
OFFICIAL_QQ_STARS_CUP_SCHEDULE_ENABLED=true \
OFFICIAL_QQ_STARS_CUP_RELATIONSHIP_STATE_PATH=/opt/2048-event/赛事中台/data/tmp/stars_cup_bot/official_target_relationship.json \
./.venv/bin/python scripts/run_official_qq_bot.py --check-local
```

`local_ready` 只证明输出中已标为 `checked` 的本地文件和目录就绪，不证明
latest 快照、SDK、凭据或平台权限。默认关闭的环境模板仍不能直接执行官方
`--preflight`；该入口会按设计要求启用标志、数据库和凭据齐全。

## 首次 latest 快照 bootstrap

启用群星杯调度的 `--preflight` 会要求 latest 指针、快照及两张榜图已经存在
且哈希一致。全新部署不能靠首次网关启动生成它们，因为 systemd
`ExecStartPre` 会先阻止缺少产物的服务进入网关。

在确认正式名单、比赛时间窗、背景和 Verse 查询窗口后，先以 `eventbot`
身份单独运行：

```bash
./.venv/bin/python scripts/run_stars_cup_daily.py
```

该命令会查询 Verse，写入暂存缓存、不可变导出和 latest 指针，但不会登录
QQ、启动调度器或发送 QQ 消息。它不是纯只读命令；运行前仍需确认比赛输入，
并保留上一份有效 latest 和不可变导出作为回滚依据。

## 离线启动门

只有在获准的私有 `/etc/2048-event/official-qq-bot.env` 中填入应用测试凭据
和目标 OpenID 后，才按阶段打开标志并执行：

```bash
./.venv/bin/python scripts/run_official_qq_bot.py --check-config
./.venv/bin/python scripts/run_official_qq_bot.py --preflight
```

`--preflight` 会构造并关闭 SDK Client，检查本地权限、Intent 声明、latest
快照、两张图片哈希和当日持久状态，但不会调用网关 `run`。其中
`platform_readiness` 的 `UNKNOWN` 只能由获准的平台检查或真实 canary 消除。

## 授权停止点与回滚

在用户明确授权前，到离线 `--preflight` 为止，不执行：

- `scripts/run_official_qq_bot.py --start`；
- systemd unit 的 install、enable 或 start；
- 真实群 @、C2C、主动图片发送；
- 官方平台审核、权限、Intent、白名单或群配置变更。

获准后仍应按“被动群 @ 文字 → C2C → 总榜图 → 分榜图 → 定时发送”的顺序
逐项 canary。任一项出现审核拒绝、权限拒绝、关系拒收或未知回执时停止推进。
回滚只停止官方入口及其内置调度器，继续保留 OneBot 路径、SQLite、latest
指针、不可变榜图和逐图投递状态。
