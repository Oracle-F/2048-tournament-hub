# Completion audit — 2026-07-27

## Executive result

The transport boundary and the two priority Stars Cup workflows are implemented
and verified offline. The remaining work is not a second rewrite: it is a small
reliability batch around relationship events, scheduler failure semantics,
restart evidence and explicit official-route fixtures. Real QQ availability is
still unproven because no authorized login, send or platform inspection occurred.

## Evidence ledger

| Claim | Source inspected | Evidence | Verdict |
| --- | --- | --- | --- |
| OneBot remains isolated | `bot_private_qq/app.py`, `bot_private_qq/onebot_transport.py` | SDK/event/send/lifecycle code remains under OneBot adapter | Confirmed |
| Existing business is reused | `services/bot_transport.py`, `services/bot_private_service.py` | neutral dispatcher calls original private/group handlers | Confirmed |
| Official messages are isolated | `bot_official_qq/app.py`, `transport.py` | opaque IDs, DTO mapping, fake API receipts | Confirmed offline |
| Official runtime is guarded | `runtime.py`, `scripts/run_official_qq_bot.py` | enable flag, explicit start, sandbox default, production confirmation | Confirmed offline |
| SDK/API shape is current | `requirements-official-qq.txt`, `sdk_facade.py`, installed package | `qq-botpy==1.2.1`, public APIs and 2026 chunk routes | Confirmed offline |
| Stars Cup query avoids live fetch | `stars_cup_bot_service.py` | only validated latest snapshot is read | Confirmed |
| Daily export preserves last good | `stars_cup_daily_service.py` | staged cache, immutable output, validation before atomic latest | Confirmed |
| Partial image delivery resumes | daily service tests/state | per-image hash/status and skip-sent logic | Confirmed offline |
| Scheduler starts only after ready | `runtime.py`, scheduler tests | one guarded task, cancelled on client close | Confirmed offline |
| Scheduler task does not fail silently | runtime supervisor test | exception/return restart with redacted log and bounded delay | Confirmed offline |
| Service startup rejects invalid daily state | preflight tests, systemd example | current-day reconciliation plus `ExecStartPre` before `--start` | Confirmed offline |
| Unexpected preflight failures are redacted | entrypoint test | error type only, no exception text/path/traceback | Confirmed offline |
| systemd template parses | Fedora `systemd-analyze verify` | only expected missing placeholder executable warnings | Confirmed as template |
| Local storage is ready before gateway start | permission fixtures and actual preflight | DB file/parent, inputs, cache/state and export checks; no probe writes or path output | Confirmed locally |
| systemd environment handoff is safe | entrypoint/template test | official-only variables, blank secrets, sandbox on, runtime/schedule off | Confirmed as template |
| Runtime evidence is journal-visible by default | entrypoint logging tests | start-only forced INFO handler; DEBUG/invalid values blocked before network | Confirmed offline |
| Clean gateway exit is recoverable | unit template test and static parse | `Restart=always`, 15-second delay, 5 starts per 5 minutes | Confirmed as template |
| Real QQ permissions exist | no platform access/authorization | no external evidence | UNKNOWN |
| Target bot is in group and accepts active messages | relationship callbacks/state exist, but no authorized runtime event was observed | offline code cannot prove current platform state | UNKNOWN |
| Real platform accepts both images | no authorized send | fake receipts only | UNKNOWN |

## Current test evidence

- Recorded historical baseline: 12 basic tests, Bot 47/47 and fast suite `TOTAL PASS`.
- Latest implementation validation: the same basic command now runs 15/15,
  full unittest runs 222/222, plus `pip check`, Bot 47/47, fast `TOTAL PASS`
  and actual installed-SDK offline preflight with current snapshot/images.
- Tests use local fakes and `data/testing.db`; they do not prove platform review,
  permission, group membership, audit acceptance, production networking or rate tier.

## Source-derived findings

1. `public_messages=True` now routes all eight group/C2C/friend relationship callbacks
   separately from message-create events and business/database handling.
2. SDK 1.2.1 fields `group_openid` and `openid` are mapped without numeric QQ
   conversion; only the configured target group's SHA-256 and state persist.
3. Scheduler exceptions now distinguish transient network/lock failures from
   permanent configuration, contract, artifact and unknown failures.
4. Scheduler startup explicitly reconciles current-day durable delivery state
   before export/send and blocks blind retries for terminal or unknown states.
5. `赛事`, `我的报名`, `我的成绩`, `我的档案` and true two-page pagination now
   have named official C2C end-to-end evidence.
6. Offline preflight now reports the same current-day reconciliation, and the
   uninstalled systemd template refuses to start the gateway when it fails.
7. The runtime restarts a scheduler task that unexpectedly raises or returns;
   cancellation remains immediate and error text is not written to logs.
8. The CLI converts unexpected offline preflight failures into redacted JSON
   instead of allowing a traceback to reach the operator terminal or journal.
9. Preflight now rejects unreadable/unwritable local runtime storage before
   login; the current Fedora checkout passes every database and daily-job item.
10. A dedicated systemd environment example matches the unit path and keeps
    credentials blank plus gateway/scheduler flags disabled by default.
11. Real start installs a deterministic INFO+ stderr handler for systemd
    capture; offline modes remain clean and DEBUG is rejected before network.
12. The unit now restarts an unexpectedly clean SDK-loop exit as well as a
    failure, while `StartLimit*` bounds repeated automatic starts.

## Safety conclusion

- The ordered offline batch in `05_TODO.md` is complete and regression-tested.
- Not safe to claim the official Bot is usable on real QQ.
- Not authorized to start the gateway, send a canary, change platform settings,
  install/enable systemd or populate credentials/OpenIDs.
- No reason to alter event_hub, scoring, Verse cache, snapshot schema, renderer,
  database schema or OneBot rollback code.

## First implementation handoff

The implementation batch, full sequential regression and actual-SDK offline
preflight are complete. Remaining actions require explicit authorization or
product choices: inspect official platform state, run passive/proactive
canaries, decide whether score submission may be enabled, and only later
consider removing legacy OneBot compatibility.
