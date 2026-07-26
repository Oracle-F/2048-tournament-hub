# Hard rules

## ALLOWED TO MODIFY
- `bot_official_qq/relationship_state.py` as one focused new transport-local module.
- `bot_official_qq/runtime.py` for lifecycle callbacks and redacted preflight evidence.
- `bot_official_qq/scheduler.py` for relationship gating, reconciliation and exception classification.
- `.env.bot.example` only for disabled-by-default local state-path configuration if needed.
- `docs/README.md` for offline/real evidence wording and rollback operations.
- Focused tests in `tests/test_official_qq_relationship_state.py`, `tests/test_official_qq_runtime.py`, `tests/test_official_qq_scheduler.py` and `tests/test_official_qq_app.py`.
- This `plan/stars-cup-official-qq-bot/` package.

## FORBIDDEN
- Database schema, bootstrap data, migrations and production database.
- `event_hub.py` behavior/public signatures; scoring, settlement or Verse cache algorithm.
- `services/match_rank_image_service.py` snapshot schema, calculation, layout or public signatures.
- Formal roster, historical exports, `.env`, `.env.bot.secret`, runtime logs and real credentials.
- OneBot/NapCat/NoneBot dependencies, lifecycle, watchdog, QR/startup scripts and process entrypoint.
- Existing `services/bot_private_service.py` signatures or command semantics in this remaining work package.
- Real QQ login/send, platform configuration, deployment, service install/enable/start or timer enablement without authorization.
- Broad formatting, path migration, permission changes, bulk deletion or unrelated dirty files.

## Required preservation
- OneBot remains a runnable rollback path and its 47 Bot cases stay green.
- Official and OneBot IDs remain separate under `bot_platform=qq_official` and `qq`.
- C2C/group message callbacks keep one short-lived SQLite connection per event.
- Relationship callbacks never open SQLite or invoke business handlers.
- Existing immutable run/latest pointer and per-image delivery records remain authoritative.
- `unknown` platform state is never represented as approval.
- No raw OpenID, AppID, secret, absolute artifact path or message content in new state/log summaries.

## Dependency rules
- Keep `qq-botpy==1.2.1` in `requirements-official-qq.txt`; do not add a scheduler or persistence package.
- SDK event/API types stay inside `bot_official_qq/`.
- Relationship state uses stdlib JSON, SHA-256 and atomic replace under the existing temp state root.
- Credentials come from process environment only.

## Testing rules
- Run tests sequentially and use only `data/testing.db`; never run DB-rebuilding suites in parallel.
- All official transport tests use local SDK-shaped fakes; no gateway, HTTP, upload or send.
- Freeze Asia/Singapore times for scheduler/reconciliation tests.
- Prove reject/remove blocks before export and send; receive/add clears only the relationship block.
- Prove corrupt/mismatched state is terminal and never exposes target IDs.
- Prove wrapped network exceptions retry and unknown/config/artifact exceptions terminate.
- Prove restart reconciliation never resends an image with a durable `sent` receipt.
- After focused tests, run 12 basic, Bot 47/47, full unittest and `tests/run_all.py --fast` sequentially.
- Real canary and visual receipt inspection require a separate explicit user authorization.

## Commit rules
- Stage only reviewed files from the allowed list.
- Before commit, inspect `git status --short`, `git diff --check` and the staged diff.
- Never stage existing untracked roster, DB, images, bug notes or handoff documents.
- Push only the current feature branch; do not merge, rebase or force-push.
