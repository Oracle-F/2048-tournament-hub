# Official QQ compatibility status

This file distinguishes three different claims:

1. **Business covered**: the existing handler has a deterministic regression
   test.
2. **Official route covered**: an official C2C/group event reaches that
   business handler and a fake official API receives the reply.
3. **Real restored**: the behavior has passed an authorized QQ sandbox or
   allowlisted-group canary.

Only the third state means a legacy feature is restored in real QQ.  No row is
marked real-restored before that evidence exists.

| Capability | Business evidence | Official-route evidence | Current status | Remaining gate |
| --- | --- | --- | --- | --- |
| 群星杯总榜、队伍、玩家、我的查分 | `test_stars_cup_bot_service` | real snapshot group event in `test_official_qq_app` | Offline complete | one authorized group @ canary |
| 每日生成两张榜图 | `test_stars_cup_daily_service` and fast suite | scheduler/preflight tests | Offline complete | authorized Verse schedule and visual check |
| 每日主动发送两张榜图 | idempotent delivery-state tests | official proactive media and scheduler tests | Offline complete | one allowlisted-group proactive canary, then three scheduled runs |
| 私聊帮助文字/图片 | Bot cases and help-image tests | official C2C help image test | Offline complete | one authorized C2C canary |
| 私聊绑定、解绑 | binding guardrail Bot cases | full official C2C bind/PIN/unbind fixture | Offline complete | real OpenID creates a separate `qq_official` binding; bind/unbind canary |
| Verse 公共查分 | Verse service and Bot cases | official C2C dispatch with offline service response | Offline complete | authorized C2C response and live Verse latency check |
| 报名、取消报名 | existing business regression cases | full official C2C registration/cancellation fixture | Offline complete | authorized future test-event canary |
| 限时预约、查看、取消 | timed-reservation service | full official C2C reservation/view/cancellation fixture | Offline complete | authorized future test-event canary |
| 个人看板 | personal-dashboard Bot cases | official C2C edit plus allowlisted group query fixture | Offline complete | authorized C2C edit plus allowlisted group query canary |
| 成绩提交 | flow-toggle and business tests | disabled gate plus explicitly enabled pending-submission fixture | Offline complete and disabled by default | explicit decision to enable, then isolated test-event canary |
| `floor` / `finish` 回放上传 | business flow tests plus bounded URL materialization tests | full bound-session official event fixture through early/final prefix approval | Offline complete | authorized small-file canary |
| 管理员命令 | existing admin business gates | official denial plus injected-authorized read-only help fixture | Offline complete | add approved official OpenID to private admin config and run read-only canary |
| 普通群命令 | group gates/rate-limit Bot cases | official group event and reply tests | Partial | approved group OpenID, whitelist and group @ canary |
| 回复、引用、图片和文件发送 | neutral and official transport tests | `msg_id`, `msg_seq`, reference and upload tests | Offline complete | real platform receipt/audit evidence |
| NapCat/OneBot watchdog | OneBot-specific tests | intentionally none | Retained only for rollback | never reuse for official runtime |

## Offline safety now enforced

- Official runtime needs both `OFFICIAL_QQ_BOT_ENABLED=true` and `--start`.
- Production additionally needs
  `OFFICIAL_QQ_BOT_PRODUCTION_CONFIRMED=true`; sandbox is the default.
- The daily scheduler has its own disabled-by-default switch and group target.
- `--preflight` constructs and closes the real SDK Client, validates the
  `1<<25` intent and API facade, and optionally validates the current Stars Cup
  snapshot and both image hashes without login or network.
- Remote inbound attachments use a 25 MiB default cap, write to a unique
  partial file, and become visible only after a successful atomic replace.
- OneBot remains installed and no OneBot lifecycle/watchdog code is shared
  with the official runtime.

## Next offline batch

Add official-event fixtures for remaining low-risk read-only views such as
event lists, personal records and score-history pagination.  Keep score
submission disabled by default and use only temporary test data.

## Real validation order

1. Sandbox/allowlisted group @ `/群星杯` passive text only.
2. C2C help and bind/unbind against a disposable test identity.
3. One proactive total-rank image, then one detail image.
4. Enable the daily scheduler only after both media receipts are confirmed.
5. Observe three scheduled deliveries while OneBot remains the rollback path.
6. Restore the remaining partial rows one tested batch at a time.
