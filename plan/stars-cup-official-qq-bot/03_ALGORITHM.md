# Algorithms

## Transport-neutral dispatch
1. Adapter validates a supported event and maps it to `BotInboundMessage`.
2. Dispatch opens the existing configured database connection.
3. Group traffic passes existing enabled, whitelist, mention, per-user, global and concurrency checks.
4. Dedicated群星杯 command is resolved before generic binding/dashboard/Verse routing.
5. Existing service return values pass through the legacy reply normalizer.
6. Adapter renders `BotReply` into its own SDK/API structures and sends.

Edge cases:
- Empty text with attachments remains a valid inbound message.
- Official OpenIDs never compare with or overwrite OneBot numeric IDs.
- Malformed legacy CQ produces an internal contract error, not a raw text leak.
- Transport send errors never roll back committed business database work.

## Snapshot-backed群星杯 query
1. Parse only the dedicated command prefix.
2. Resolve `self` using `bot_platform + opaque user ID`; other selectors need no binding.
3. Load the latest pointer, verify every path is inside the export root and verify hashes.
4. Validate snapshot through the existing image-service validator.
5. Match team code exactly; match Verse player name case-insensitively.
6. Build a compact reply with score fields already present in the snapshot.
7. Append `as_of`; append stale warning when age exceeds configured threshold.

Failure cases:
- No latest pointer: tell the user the daily data is not ready.
- Corrupt pointer/artifact: tell the user the last export is unavailable and log exact reason.
- Duplicate case-insensitive player match: require exact Verse spelling.
- Bound user absent from roster: report not enrolled; do not fall back to global Verse query.

## Daily export state machine
States: `not_started → querying → queried → rendering → validated → published`.

1. Derive one run ID from configured timezone and scheduled cutoff.
2. Acquire a lock keyed by competition code; reject concurrent execution.
3. If run state is `published` and hashes still verify, return the existing result.
4. Run existing incremental query; it already preserves old cache on incomplete roster fetch.
5. Render from the successful cache into a new run directory.
6. Open both PNGs, require expected dimensions, and validate snapshot JSON.
7. Compute hashes and write the completed run record.
8. Atomically replace latest pointer only after all validations pass.
9. Release lock in every terminal state.

Failure cases:
- Query partial failure: retain old live cache and latest pointer.
- Render/validation failure: retain output directory for diagnosis but never publish it.
- Process crash before pointer replace: next run ignores the incomplete directory.
- Existing valid run: reuse artifacts rather than create divergent same-day output.

## Per-image delivery state machine
States per image: `pending → sending → sent` or `pending → failed_retryable|failed_terminal`.

1. Verify current artifact hash against the published run.
2. Skip `sent`.
3. Dry-run emits intended action without changing state to `sent`.
4. Mark `sending` with attempt time.
5. Upload to the exact destination scene and send one image.
6. Atomically store receipt and mark `sent`.
7. Continue to the second image only after the first receipt is durable.

Edge cases:
- Crash after platform success before receipt: mark `unknown`; require manual reconciliation, never blind retry.
- First image sent, second failed: retry only the second.
- Group opted out, bot removed, permission denied or audit rejected: terminal for that run and alert operator.
- HTTP quota/transient failure: retain retryable state and honor bounded backoff.
- A later run never changes receipts of an earlier immutable run.

## Rollout and rollback
1. Fix cross-platform test expectations.
2. Add contract and fake-adapter tests with no runtime wiring.
3. Route OneBot through the neutral bridge; compare all existing cases.
4. Add群星杯 snapshot query and daily dry-run.
5. Exercise repeated dry-runs and forced partial failures.
6. Add official adapter behind disabled configuration.
7. After user supplies permissions, test in sandbox/allowlisted group.
8. Enable one transport at a time; OneBot remains rollback path.

Rollback:
- Disable official runtime and scheduled send flags.
- Continue OneBot entrypoint unchanged at process boundary.
- Keep the last valid latest pointer and immutable outputs.
- Do not revert database, cache, scoring, or snapshot code because no schema/data migration occurs.
