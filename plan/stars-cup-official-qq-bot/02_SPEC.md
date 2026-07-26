# Function specification

## Data contracts
- `BotAddress`: `transport`, `conversation_kind`, `conversation_id`, `user_id`; all IDs are opaque strings.
- `BotAttachment`: `kind`, `name`, `content_type`, optional `url`, optional `local_path`, optional `platform_file_id`.
- `BotInboundMessage`: address, text, attachments, message ID, event ID, reference ID, mention flag, received time.
- `BotReply`: text, ordered attachments, optional reference ID; no CQ or SDK objects.
- `SendReceipt`: transport, target, platform message ID, sent time, status, error code, retryable flag.
- `StarsCupQuery`: kind `overview|team|player|self`, normalized selector.
- `DailyRunResult`: run ID, snapshot path/hash, ordered image paths/hashes, per-image delivery receipts.

## normalize_legacy_reply
Function: `normalize_legacy_reply`
Purpose: Convert the existing string/CQ return value into `BotReply`.
Location: `services/bot_transport.py`
Inputs: Existing reply value returned by `handle_private_message` or `handle_group_message`.
Outputs: `BotReply` or `None`.
Called By: Business dispatch wrapper.
Calls: Path URI parser and contract constructors only.
Logic: 1. Return `None` for `None`. 2. Convert the value to text. 3. Extract each existing CQ image marker in source order. 4. Convert file URI to an image attachment. 5. Remove only extracted markers from text. 6. Return normalized text plus attachments.
Errors: Reject malformed file URIs with a non-retryable contract error.
Side Effects: None.
Notes: Temporary compatibility bridge; no new service may emit CQ markers.

## dispatch_business_message
Function: `dispatch_business_message`
Purpose: Invoke existing business handlers without exposing transport SDK types.
Location: `services/bot_transport.py`
Inputs: SQLite connection and `BotInboundMessage`.
Outputs: `BotReply` or `None`.
Called By: OneBot and official QQ inbound adapters.
Calls: `handle_private_message`, `handle_group_message`, `normalize_legacy_reply`.
Logic: 1. Map transport to `bot_platform`. 2. Convert attachments to existing neutral segment dictionaries. 3. Route by conversation kind. 4. Pass opaque IDs unchanged. 5. Normalize the returned reply.
Errors: Propagate business errors; never convert transport errors into business errors.
Side Effects: Existing business handler side effects only.
Notes: Preserve existing handler signatures and direct tests.

## onebot_event_to_inbound
Function: `onebot_event_to_inbound`
Purpose: Convert a OneBot v11 event into `BotInboundMessage`.
Location: `bot_private_qq/onebot_transport.py`
Inputs: OneBot bot instance, message event and extracted plain text.
Outputs: `BotInboundMessage`.
Called By: `bot_private_qq/app.py`.
Calls: Existing file hydration and @/reply detection helpers.
Logic: 1. Classify private/group. 2. Hydrate file segments. 3. derive opaque IDs. 4. Preserve message/reference IDs. 5. Normalize mention text. 6. Build the DTO.
Errors: File hydration failure retains metadata and omits inaccessible path; event shape errors are non-retryable.
Side Effects: May call OneBot file lookup APIs.
Notes: OneBot imports must remain in `bot_private_qq/`.

## send_onebot_reply
Function: `send_onebot_reply`
Purpose: Send a `BotReply` through the current OneBot matcher.
Location: `bot_private_qq/onebot_transport.py`
Inputs: Matcher, inbound DTO and reply.
Outputs: `SendReceipt`.
Called By: `bot_private_qq/app.py`.
Calls: OneBot message segment constructors and matcher send/finish.
Logic: 1. Build reference/@ prefix for group replies. 2. Append text when non-empty. 3. Append attachments in order. 4. Send once. 5. Classify timeout, connection and action errors.
Errors: Return retryability classification; preserve current silent handling for known unstable transport errors.
Side Effects: Sends one QQ message through NapCat.
Notes: Current OneBot behavior is the regression oracle.

## parse_stars_cup_query
Function: `parse_stars_cup_query`
Purpose: Parse the dedicated group command without colliding with generic Verse syntax.
Location: `services/stars_cup_bot_service.py`
Inputs: Raw normalized group text.
Outputs: `StarsCupQuery` or `None`.
Called By: `handle_stars_cup_group_query`.
Calls: String normalization only.
Logic: 1. Accept only `/群星杯` or full-width slash equivalent. 2. Empty selector means overview. 3. `我` means self. 4. Single A-F letter means team. 5. Any other single selector means player. 6. More than one selector returns a deterministic syntax error.
Errors: Invalid selector returns a user-facing parse error.
Side Effects: None.
Notes: Command matching precedes generic dashboard and Verse matching.

## load_latest_stars_cup_snapshot
Function: `load_latest_stars_cup_snapshot`
Purpose: Load only the last fully validated export.
Location: `services/stars_cup_bot_service.py`
Inputs: Latest pointer path and current time.
Outputs: Validated snapshot plus paths, hash and age metadata.
Called By: Group query and daily delivery.
Calls: JSON reader, SHA-256 calculator, `validate_snapshot`.
Logic: 1. Read pointer. 2. Resolve paths under the configured export root. 3. Verify snapshot and two image files exist. 4. Verify stored hashes. 5. Validate snapshot. 6. Compute age without rejecting old data.
Errors: Missing/invalid pointer returns a typed unavailable error; hash mismatch returns corruption error.
Side Effects: None.
Notes: Never falls back to a partially written timestamp directory.

## build_stars_cup_query_reply
Function: `build_stars_cup_query_reply`
Purpose: Render a compact text answer from the immutable snapshot.
Location: `services/stars_cup_bot_service.py`
Inputs: Validated snapshot, `StarsCupQuery`, optional bound Verse account.
Outputs: Plain text.
Called By: `handle_stars_cup_group_query`.
Calls: Snapshot lookup helpers only.
Logic: 1. Resolve self to bound account. 2. Match player case-insensitively against Verse values. 3. Reject ambiguous matches. 4. Select overview/team/player fields. 5. Include data cutoff and stale warning. 6. Never expose rating or non-snapshot identity data.
Errors: Missing binding, not found and ambiguity return distinct user-facing messages.
Side Effects: None.
Notes: No network and no database writes.

## handle_stars_cup_group_query
Function: `handle_stars_cup_group_query`
Purpose: Integrate dedicated read-only querying with existing group controls.
Location: `services/bot_private_service.py`
Inputs: Connection, bot platform/user ID, group ID and normalized text.
Outputs: Plain string or `None`.
Called By: `handle_group_message`.
Calls: Parser, latest snapshot loader, existing binding lookup, reply builder.
Logic: 1. Parse command. 2. Return `None` when not dedicated. 3. Load binding only for self. 4. Load snapshot. 5. Return compact reply or deterministic unavailable message.
Errors: Log internal detail; return safe text without path or stack trace.
Side Effects: Debug logging only.
Notes: Run after whitelist/mention/limit checks and before the generic binding gate.

## run_stars_cup_daily_export
Function: `run_stars_cup_daily_export`
Purpose: Produce one validated immutable daily artifact set.
Location: `services/stars_cup_daily_service.py`
Inputs: Run time, roster/cache/background/output paths, worker count and full-refresh flag.
Outputs: `DailyRunResult` with no delivery receipts.
Called By: Daily CLI and tests.
Calls: `query_live_scores`, `export_rank_images`, image verification, hash writer.
Logic: 1. Derive Asia/Singapore run ID. 2. Acquire single-run lock. 3. Reuse an already successful same run ID. 4. Query into current live cache. 5. Export into a new timestamp directory. 6. Validate JSON and both PNGs. 7. Hash all artifacts. 8. Atomically replace latest pointer. 9. Record export success.
Errors: Preserve previous cache/latest on query or export failure; return non-zero failure state.
Side Effects: Verse reads and writes under cache/output/state paths.
Notes: Never opens the production SQLite database in the default live-source mode.

## deliver_stars_cup_daily_images
Function: `deliver_stars_cup_daily_images`
Purpose: Send each validated daily image once through a selected transport.
Location: `services/stars_cup_daily_service.py`
Inputs: `DailyRunResult`, transport, group address, delivery state path and dry-run flag.
Outputs: `DailyRunResult` with per-image receipts.
Called By: Daily CLI.
Calls: Latest snapshot loader, transport proactive group send, state writer.
Logic: 1. Verify artifact hashes again. 2. Load per-run/per-image state. 3. Skip successful images. 4. In dry-run record no success and send nothing. 5. Send total then detail image. 6. Persist each successful receipt atomically before the next image. 7. Stop on non-retryable failure. 8. Return partial state on retryable failure.
Errors: Reject invalid artifacts and target; classify quota, opt-out, permission, audit and network failures.
Side Effects: Optional group sends and JSON delivery-state writes.
Notes: Two image messages consume two active-message units.

## official_event_to_inbound
Function: `official_event_to_inbound`
Purpose: Convert official C2C or group @ events into the neutral contract.
Location: `bot_official_qq/transport.py`
Inputs: Official SDK event object.
Outputs: `BotInboundMessage`.
Called By: Official bot event callbacks.
Calls: Official attachment extraction and contract constructors.
Logic: 1. Accept only C2C and group @ message events. 2. Map OpenIDs as opaque IDs. 3. Preserve message/event/reference IDs. 4. Mark group events as mentioned. 5. Convert attachments without downloading them.
Errors: Unsupported event returns `None`; malformed supported event raises a non-retryable mapping error.
Side Effects: None.
Notes: No numeric QQ assumptions.

## send_official_reply
Function: `send_official_reply`
Purpose: Send a passive reply through the official QQ API.
Location: `bot_official_qq/transport.py`
Inputs: Official client, inbound DTO and `BotReply`.
Outputs: Ordered `SendReceipt` values.
Called By: Official bot callbacks.
Calls: Scene-specific upload and message endpoints.
Logic: 1. Select C2C/group endpoint. 2. Send text with inbound message ID and next sequence. 3. Upload each attachment in the same scene. 4. Send each as `msg_type=7` with incremented sequence. 5. Include reference only when present. 6. Record every response.
Errors: Do not retry expired reply, rejected content, opt-out or permission errors; bounded retry only network/5xx/quota responses with server delay.
Side Effects: Official QQ API calls.
Notes: Complete before the 60-minute C2C or 5-minute group reply window.

## send_official_proactive_group
Function: `send_official_proactive_group`
Purpose: Send one scheduled image without a triggering message.
Location: `bot_official_qq/transport.py`
Inputs: Official client, group OpenID and one image attachment.
Outputs: `SendReceipt`.
Called By: `deliver_stars_cup_daily_images`.
Calls: Group upload endpoint and group message endpoint.
Logic: 1. Validate allowed target. 2. Upload to group scope. 3. Send `msg_type=7` without message/event ID. 4. Persist returned message ID in receipt. 5. Classify response.
Errors: Opt-out, non-membership, permission, audit and content errors are non-retryable for the run; quota and transient server errors are retryable.
Side Effects: One proactive QQ group message.
Notes: Never uses upload-time direct send because that weakens per-image receipt control.

## daily_cli_main
Function: `main`
Purpose: Expose one non-daemon invocation for manual and external scheduling.
Location: `scripts/run_stars_cup_daily.py`
Inputs: Paths, run time, workers, full refresh, transport, target group, dry-run and send flags.
Outputs: JSON summary to stdout and process exit code.
Called By: Operator, test harness or external scheduler.
Calls: Daily export and optional delivery functions.
Logic: 1. Default to dry-run and no send. 2. Validate mutually dependent send arguments. 3. Run export. 4. Run delivery only when explicitly enabled. 5. Print paths, hashes and statuses. 6. Return 0 only when requested stages succeed.
Errors: Print concise stderr error and non-zero code; never print secrets or OpenIDs.
Side Effects: Delegated export and optional send.
Notes: Enabling system scheduling remains outside this function.
