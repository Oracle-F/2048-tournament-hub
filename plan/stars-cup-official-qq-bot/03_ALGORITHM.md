# Algorithms

## Inbound transport-neutral dispatch
1. Adapter maps only C2C message-create or group-@ message-create into `BotInboundMessage`.
2. Official event runner opens one short-lived `data/testing.db`/configured SQLite connection.
3. Existing `handle_private_message` or `handle_group_message` enforces bindings, group whitelist, mention, rate limits, flows and permissions.
4. Legacy string/CQ output becomes `BotReply`; the selected transport renders SDK-native payloads.
5. Event deduplication occurs before business dispatch and send; duplicate message ID plus index produces no side effect.
6. Transport failure does not roll back already committed business state.

## Relationship-event state machine
States for configured target group: `unknown`, `joined`, `receivable`, `rejected`, `removed`.

Transitions:
- `GROUP_ADD_ROBOT`: any state → `joined`.
- `GROUP_MSG_RECEIVE`: any state → `receivable`.
- `GROUP_MSG_REJECT`: any state → `rejected`.
- `GROUP_DEL_ROBOT`: any state → `removed`.
- No observed event: retain durable state; absence never implies permission.

Processing:
1. Receive callback under the existing `GROUP_AND_C2C_EVENT` intent.
2. Extract `group_openid` or C2C `openid` using the SDK 1.2.1 event model.
3. Compare a group ID with the configured target only in memory.
4. Persist only the configured target's SHA-256, state, event type and timestamp by atomic replace.
5. Log only event type, subject kind, target-match and state.
6. Scheduler blocks `rejected`/`removed`; other states pass this safety gate but do not establish platform permission.

Failure cases:
- Empty ID: typed mapping error, no state change.
- Corrupt state: terminal preflight/scheduler finding; do not overwrite automatically.
- Non-target event: redacted log only.
- State write error: keep prior durable state; scheduler continues to use prior state and emits an operator error.

## Daily scheduler decision
1. Normalize current time to Asia/Singapore and return `not_due` before send time.
2. On first due tick after process start, reconcile current-day durable delivery JSON.
3. Complete delivery → set in-memory completed date and do nothing.
4. Terminal/unknown delivery → set completed date, report terminal and do not retry.
5. Retryable delivery → honor durable retry deadline; otherwise allow one immediate retry.
6. Load target relationship state; block before export/send on `rejected` or `removed`.
7. Run existing idempotent export in a worker thread.
8. Run existing per-image delivery; it verifies hashes and skips durable successes.
9. Classify receipts first; if an exception escapes, use deterministic exception classification.
10. Persist receipt state through the existing delivery service before scheduling another attempt.

Exception precedence:
1. `BotTransportError`: use its `retryable` flag and code.
2. Lock contention: retryable.
3. Timeout/connection/OS failure in the exception cause chain: retryable.
4. Contract, configuration, state, JSON, path, hash and image validation: terminal.
5. Unknown exception: terminal as `unexpected_exception`.

## Offline preflight evidence
1. Validate guarded local config without dotenv loading.
2. Construct/close actual `qq-botpy` client without calling `run`.
3. Require `public_messages` value `1<<25` and a compatible API facade.
4. When schedule is enabled, verify current snapshot and both image hashes.
5. Read target relationship state when present.
6. Emit two sections:
   - local evidence: SDK version, client constructed, intent value, artifact hashes;
   - platform readiness: review, actual intent grant, proactive permission, rich media permission and real receipts.
7. Every platform item stays `UNKNOWN` until an authorized canary or platform inspection provides evidence.
8. Redact AppID, secret, OpenID and absolute paths.

## Read-only legacy feature fixtures
1. Use a temporary copy or transaction against `data/testing.db`; never production data.
2. Feed a real-shaped `C2C_MESSAGE_CREATE` object into `process_official_event`.
3. Seed an official binding under `bot_platform=qq_official` when the command requires identity.
4. Cover `赛事`, `我的报名`, `我的成绩`, `我的档案`, then `更多`.
5. Assert the real business reply text and fake official API payload (`openid`, `msg_id`, increasing `msg_seq`).
6. Assert no network, no OneBot object and no raw OpenID in logs/state.
7. Roll back all fixture database changes and pending-flow globals.

## Real validation and rollback
1. User supplies explicit authorization, approved test identity/group, platform settings and time window.
2. Run offline config check and preflight; record remaining `UNKNOWN` values.
3. Start sandbox gateway with schedule disabled.
4. Perform one group-@ `/群星杯` passive-text canary.
5. Perform one C2C help and bind/unbind canary.
6. Send total image once, inspect receipt/audit, then detail image once.
7. Enable scheduler only after both manual media receipts are confirmed; observe three daily deliveries.
8. Any permission, reject, removal, audit or unknown receipt stops automatic retry.
9. Roll back by stopping official runtime/scheduler and continuing the unchanged OneBot entrypoint.
10. Preserve SQLite, Verse cache, immutable outputs, latest pointer and delivery evidence; no data rollback is required.
