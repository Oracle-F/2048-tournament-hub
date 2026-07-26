# Official QQ compatibility status

Evidence levels:

1. **Business covered**: existing handler/service has deterministic regression evidence.
2. **Official route covered**: SDK-shaped event reaches real business code and fake official API.
3. **Real restored**: authorized QQ sandbox/allowlisted-group canary succeeded.

No offline result can satisfy level 3.

| Capability | Business evidence | Official-route evidence | Status | Remaining gate |
| --- | --- | --- | --- | --- |
| 群星杯总榜/队伍/玩家/我的查分 | snapshot service tests | real snapshot group-event fixture | Offline complete | authorized group-@ canary |
| 每日生成两张榜图 | daily service + fast suite | scheduler/preflight artifacts | Offline complete | approved roster/time and live Verse window |
| 每日主动发送两图 | delivery idempotency tests | proactive media + scheduler tests | Offline complete | relationship state work, then two-image canary and three scheduled days |
| C2C 帮助文字/图片 | Bot/help tests | official help-image fixture | Offline complete | authorized C2C canary |
| 绑定/解绑 | binding guardrails | full official PIN flow | Offline complete | real `qq_official` identity canary |
| Verse 公共查分 | service/Bot cases | official C2C service fixture | Offline complete | live latency/response canary |
| 报名/取消报名 | business cases | full official fixture | Offline complete | isolated test-event canary |
| 限时预约/查看/取消 | service tests | full official fixture | Offline complete | isolated test-event canary |
| 个人看板编辑/群查 | Bot cases | official C2C + group fixture | Offline complete | C2C/group canary |
| 成绩提交 | toggle/business tests | disabled gate + enabled fixture | Offline complete, disabled | explicit enablement decision |
| `floor`/`finish` 回放 | flow/download tests | complete official attachment fixture | Offline complete | authorized small-file canary |
| 管理员命令 | admin gate tests | denial + injected read-only help fixture | Offline complete | approved official OpenID and read-only canary |
| `赛事` | existing business cases | dedicated official C2C fixture | Offline complete | authorized C2C canary |
| `我的报名` | existing business cases | bound official C2C fixture | Offline complete | authorized C2C canary |
| `我的成绩` + `更多` | business pagination cases | 11-event two-page official C2C fixture | Offline complete | authorized C2C canary |
| `我的档案` | existing business cases | bound official C2C fixture | Offline complete | authorized C2C canary |
| 关系事件 | transport-local state only | eight callbacks, redacted state and pre-send gate | Offline complete | observe real relationship events |
| 回复/引用/图片/文件 | neutral contracts | msg_id/msg_seq/reference/upload tests | Offline complete | real receipt/audit evidence |
| NapCat/OneBot watchdog | OneBot tests | intentionally none | Retained rollback | never share with official runtime |

## Official ability mapping

| Official area | Verified first-party rule | Current handling |
| --- | --- | --- |
| Event transport | WebSocket and Webhook are supported | WebSocket runtime implemented; no webhook deployment |
| Intent | C2C/group messages and relationship events use `GROUP_AND_C2C_EVENT (1<<25)`; special intent needs permission | local value verified; actual grant `UNKNOWN` |
| Passive C2C | trigger window 60 minutes, up to 4 replies | reply context and part limit enforced offline |
| Passive group | trigger window 5 minutes, up to 5 replies | reply context and part limit enforced offline |
| Proactive group | verified bot 60 qpm, one relationship 20 qpm, 1000/day/group; unverified bot 30 qpm | two-image workflow is well below limits; real tier/permission `UNKNOWN` |
| Duplicate replies | same `msg_id` may be reused with distinct `msg_seq` | ordered text/attachment sequence implemented |
| Rich media | upload in exact C2C/group scene, then send `msg_type=7`; `file_info` expires | URL/public upload and local chunk flow implemented offline |
| Opt-out/relationship | user/group can reject proactive messages; add/remove/receive/reject events are emitted | eight callbacks persist redacted target state and block rejected/removed sends |
| Review/deployment | platform review/permission and a continuously connected runtime are external | no platform/deployment mutation performed |

First-party sources:
- [QQ message overview](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)
- [QQ event subscriptions](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)
- [Tencent Python SDK guide](https://bot.q.qq.com/wiki/develop/pythonsdk/)
- [Tencent botpy repository](https://github.com/tencent-connect/botpy)
- [qq-botpy v1.2.1 release](https://github.com/tencent-connect/botpy/releases/tag/v1.2.1)

## Offline safety enforced
- Official runtime requires enabled config plus explicit `--start`; production adds a second confirmation flag.
- Daily scheduling has a separate disabled-by-default flag and target.
- SDK preflight creates/closes the real client but never calls `run`.
- Snapshot and both image hashes can be checked without login/network.
- Preflight reports current-day durable delivery reconciliation and rejects invalid state without exposing the target or filesystem paths.
- The uninstalled systemd example gates `--start` behind the same offline preflight.
- Local inbound files are bounded and atomically published.
- OneBot entrypoint/lifecycle remains independent and runnable.

## Known evidence gaps
- Platform review, actual Intent/rich-media permission and target relationship remain
  `UNKNOWN` until observed from an authorized runtime/platform inspection.
- No passive or proactive path has a real QQ receipt or audit result.
- Deployment host, service account, writable state paths and sustained WebSocket
  connectivity remain unverified.

## Real validation order
1. Group-@ `/群星杯` passive text with schedule disabled.
2. C2C help and bind/unbind using a disposable test identity.
3. One proactive total image, inspect receipt/audit, then one detail image.
4. Enable scheduler only after relationship and media evidence are confirmed.
5. Observe three daily deliveries with OneBot kept available.
6. Restore remaining partial rows in small tested batches.
