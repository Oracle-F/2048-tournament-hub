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
| Real QQ permissions exist | no platform access/authorization | no external evidence | UNKNOWN |
| Target bot is in group and accepts active messages | no relationship callback/state or platform evidence | config only proves a string is present | UNKNOWN |
| Real platform accepts both images | no authorized send | fake receipts only | UNKNOWN |

## Current test evidence

- Recorded baseline: 12 basic tests, Bot 47/47 and fast suite `TOTAL PASS`.
- Latest implementation validation: 185 unittest cases, `pip check`, Bot 47/47
  and actual installed-SDK offline preflight with current snapshot/images.
- Tests use local fakes and `data/testing.db`; they do not prove platform review,
  permission, group membership, audit acceptance, production networking or rate tier.

## Source-derived findings

1. `public_messages=True` subscribes one intent that also contains group
   add/delete and proactive receive/reject events; the client currently handles
   only C2C message-create and group-at message-create.
2. SDK 1.2.1 models group management with `group_openid` and C2C management with
   `openid`; no numeric QQ conversion is valid.
3. Scheduler catch-all exceptions currently become retryable, even for corrupt
   artifacts or bad configuration.
4. Durable per-image state prevents most duplicate sends after restart, but the
   scheduler does not explicitly report or reconcile terminal/unknown state before
   invoking the idempotent export/delivery services.
5. `赛事`, `我的报名`, `我的成绩`, `我的档案` and pagination are reachable through
   the generic dispatcher but do not yet have named official-event end-to-end evidence.

## Safety conclusion

- Safe to continue offline with the ordered batch in `05_TODO.md`.
- Not safe to claim the official Bot is usable on real QQ.
- Not authorized to start the gateway, send a canary, change platform settings,
  install/enable systemd or populate credentials/OpenIDs.
- No reason to alter event_hub, scoring, Verse cache, snapshot schema, renderer,
  database schema or OneBot rollback code.

## First implementation handoff

Implement scheduler exception classification first. It is a pure, local change
with no SDK, database, network, config or state mutation. Add failing tests for:

1. direct retryable/non-retryable `BotTransportError`;
2. `StarsCupDailyLocked` and `StarsCupDeliveryLocked`;
3. a `StarsCupDailyError` caused by `TimeoutError`/`ConnectionError`;
4. deterministic contract/artifact errors;
5. an unknown exception defaulting to terminal.

Only after that commit passes should relationship-state code be introduced.
