# Function specification

## Data contracts
- `OfficialRelationshipUpdate`: `event_type`, `subject_kind`, `subject_id`, `status`, `observed_at`; IDs remain opaque in memory.
- `OfficialTargetState`: schema version, target SHA-256, status `unknown|joined|receivable|rejected|removed`, last event type and timestamp; no raw OpenID.
- `PlatformReadiness`: `intent_permission`, `application_review`, `target_relationship`, `proactive_messages`, `rich_media`, `real_receipt`; every field is `UNKNOWN|CONFIRMED|DENIED` plus local evidence.
- Existing `BotInboundMessage`, `BotReply`, `SendReceipt` and `DailyRunResult` contracts remain unchanged.

## official_relationship_event_to_update
Function: `official_relationship_event_to_update`
Purpose: Convert one official relationship event into a transport-local state update without invoking business handlers.
Location: `bot_official_qq/relationship_state.py`
Inputs: Official event type string, SDK event object and observation time.
Outputs: `OfficialRelationshipUpdate` or `None` for unsupported events.
Called By: `OfficialQQEventRunner.handle_relationship`.
Calls: Attribute readers and local time normalizer only.
Logic: 1. Accept the six current callbacks: group add/delete/receive/reject and C2C receive/reject. 2. Read `group_openid` for group events or `openid` for C2C events. 3. Reject empty subject IDs. 4. Map group add to `joined`, group receive to `receivable`, group reject to `rejected`, group delete to `removed`, C2C receive to `receivable` and C2C reject to `rejected`. 5. Return the opaque update without logging it.
Errors: Unsupported events return `None`; supported malformed events raise `BotContractError`.
Side Effects: None.
Notes: Initial callback set must include SDK 1.2.1 names `on_group_add_robot`, `on_group_del_robot`, `on_group_msg_reject`, `on_group_msg_receive`, `on_c2c_msg_reject`, `on_c2c_msg_receive`; friend add/delete may be added in a later batch.

## load_official_target_state
Function: `load_official_target_state`
Purpose: Read the last durable relationship state for only the configured Stars Cup target group.
Location: `bot_official_qq/relationship_state.py`
Inputs: State JSON path and configured target group OpenID.
Outputs: `OfficialTargetState` with `unknown` as the safe default.
Called By: Scheduler pre-send gate and offline preflight.
Calls: SHA-256 helper and JSON reader.
Logic: 1. Hash the configured target with SHA-256. 2. Return `unknown` when the state file is absent. 3. Validate schema, target hash, status and timestamp. 4. Return the state only when its hash matches the configured target.
Errors: Corrupt, wrong-target or unsupported-schema state raises a typed non-retryable state error.
Side Effects: Reads one local JSON file.
Notes: Never return, persist or log the raw target; the file belongs under `data/tmp/stars_cup_bot/`.

## observe_official_relationship_event
Function: `observe_official_relationship_event`
Purpose: Persist relationship changes for the configured target and emit a redacted operational record for other relationship events.
Location: `bot_official_qq/relationship_state.py`
Inputs: `OfficialRelationshipUpdate`, configured target group OpenID and state JSON path.
Outputs: `OfficialTargetState` when the update belongs to the configured group, otherwise `None`.
Called By: `OfficialQQEventRunner.handle_relationship`.
Calls: Target hashing, state validation and atomic JSON writer.
Logic: 1. Compare raw IDs only in memory. 2. For the configured group, write schema, target SHA-256, mapped status, event type and timestamp via temp-file replace. 3. For non-target group or C2C events, do not persist a target state. 4. Return enough redacted status for structured logging.
Errors: File write/replace errors propagate and must not start or stop a send by themselves.
Side Effects: Atomically updates one target-state JSON file and emits no business/database writes.
Notes: `rejected` and `removed` are blocking; `unknown`, `joined` and `receivable` are non-blocking because absence of a recent event is not proof of denial.

## official_target_send_allowed
Function: `official_target_send_allowed`
Purpose: Prevent proactive delivery after an observed reject or robot removal.
Location: `bot_official_qq/relationship_state.py`
Inputs: `OfficialTargetState`.
Outputs: Boolean and stable reason code.
Called By: `OfficialStarsCupDailyScheduler.tick`.
Calls: None.
Logic: 1. Return false with `relationship_rejected` for `rejected`. 2. Return false with `robot_removed` for `removed`. 3. Return true with `relationship_unknown`/`joined`/`receivable` for the other states. 4. Never convert `unknown` into confirmed permission.
Errors: Invalid status raises a typed non-retryable state error.
Side Effects: None.
Notes: Real-send authorization remains an independent process/config gate even when this function returns true.

## classify_scheduler_exception
Function: `classify_scheduler_exception`
Purpose: Stop permanent export/config/artifact failures from retrying forever while preserving bounded retries for transient failures.
Location: `bot_official_qq/scheduler.py`
Inputs: Raised exception.
Outputs: Scheduler status `retryable_failure|terminal_failure` and stable error code.
Called By: `OfficialStarsCupDailyScheduler.tick`.
Calls: Exception cause-chain walker.
Logic: 1. Honor `BotTransportError.retryable`. 2. Treat daily/delivery lock contention as retryable. 3. Treat timeout, connection and OS/network errors anywhere in the cause chain as retryable. 4. Treat contract, relationship-state, configuration, artifact/hash/JSON and validation errors as terminal. 5. Treat unknown exceptions as terminal with `unexpected_exception`.
Errors: Never raises for an exception object.
Side Effects: None.
Notes: Classification order is significant; tests must cover wrapped network errors and unknown exceptions.

## reconcile_stars_cup_scheduler_state
Function: `reconcile_stars_cup_scheduler_state`
Purpose: Derive the current-day scheduler status from durable run and per-image delivery records after process restart.
Location: `bot_official_qq/scheduler.py`
Inputs: Local date/time, state root and SHA-256 of configured group target.
Outputs: `not_started|sent|retryable_failure|terminal_failure|unknown_delivery` plus run ID and retry timestamp when present.
Called By: First due `OfficialStarsCupDailyScheduler.tick` after construction.
Calls: Existing JSON state readers and delivery-entry validator.
Logic: 1. Resolve the current local run ID. 2. If both images are durably `sent`, mark the date complete without export/send. 3. If either image is `unknown` or terminal, mark the date terminal without export/send. 4. If a retryable entry exists, honor its durable next-attempt timestamp or permit one immediate retry when absent. 5. Otherwise continue normal idempotent export/delivery. 6. Reject mismatched target hashes.
Errors: Corrupt or mismatched state is terminal and produces a redacted operator error.
Side Effects: Reads run/delivery JSON and updates only in-memory scheduler fields.
Notes: Existing immutable export reuse and per-image send skipping remain the second defense against duplicates.

## build_platform_readiness_summary
Function: `build_platform_readiness_summary`
Purpose: Separate locally verified readiness from platform state that offline code cannot know.
Location: `bot_official_qq/runtime.py`
Inputs: Validated runtime config and optional `OfficialTargetState`.
Outputs: Redacted `PlatformReadiness` mapping.
Called By: `preflight_official_qq_bot`.
Calls: Target-state loader and local config predicates.
Logic: 1. Mark local SDK/intent declaration separately from actual intent permission. 2. Set review, intent permission, proactive permission and real receipt to `UNKNOWN` offline. 3. Report target relationship only from an observed durable event, otherwise `UNKNOWN`. 4. Never elevate `joined` to active-message permission. 5. Include no raw AppID, secret, OpenID or filesystem path.
Errors: Invalid local target state makes preflight fail with a stable code; absent state is not an error.
Side Effects: None.
Notes: The word `passed` may describe SDK/artifact checks only, never platform approval.

## handle_relationship
Function: `OfficialQQEventRunner.handle_relationship`
Purpose: Route official relationship callbacks without opening SQLite or invoking business logic.
Location: `bot_official_qq/runtime.py`
Inputs: Event type and SDK event object.
Outputs: Redacted observed status or `None`.
Called By: Generated `StarsCupOfficialQQClient` lifecycle callbacks.
Calls: `official_relationship_event_to_update`, `observe_official_relationship_event`.
Logic: 1. Normalize the event. 2. Update only the configured target state. 3. Log event type, subject kind, target-match boolean and status. 4. Never log raw IDs. 5. Return without touching the normal event runner database path.
Errors: Log typed mapping/state errors with type and stable code; let callback error policy record unexpected failures.
Side Effects: Optional target-state JSON update and redacted log.
Notes: Message-create callbacks continue using short-lived SQLite connections exactly as now.
