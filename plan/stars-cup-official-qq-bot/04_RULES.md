# Hard rules

## ALLOWED TO MODIFY
- `services/bot_transport.py`
- `bot_private_qq/app.py`
- `bot_private_qq/onebot_transport.py`
- `bot_official_qq/__init__.py`
- `bot_official_qq/app.py`
- `bot_official_qq/transport.py`
- `services/stars_cup_bot_service.py`
- `services/stars_cup_daily_service.py`
- `services/bot_private_service.py`
- `scripts/run_stars_cup_daily.py`
- `.env.bot.example`
- `requirements-bot.txt`
- `docs/README.md`
- New focused files under `tests/` and `tests/bot_cases/`
- The four existing cross-platform path cases and their case-runner path normalization only

## FORBIDDEN
- Database schema, bootstrap data and migrations.
- `event_hub.py` behavior or public signatures.
- `services/match_rank_image_service.py` calculation, schema, layout or public signatures.
- Scoring, settlement, registration, Verse cache algorithm, snapshot schema and renderer.
- Formal roster, production database, `.env`, `.env.bot.secret`, logs and existing exports.
- NapCat watchdog/startup scripts during official adapter work.
- Renaming existing public handler APIs or deleting OneBot dependencies before canary acceptance.
- Real QQ login, message send, platform configuration, deployment or timer enablement without authorization.
- Broad line-ending normalization, file moves, bulk deletion or unrelated cleanup.

## Required preservation
- Existing `handle_private_message` and `handle_group_message` signatures and direct string behavior.
- Existing OneBot group whitelist, mention, flow, rate-limit and concurrency behavior.
- Existing `event_hub.query_live_scores` and `event_hub.export_rank_images` contracts.
- Timestamped immutable export directories and previous valid latest pointer on failure.
- `bot_account_bindings` compatibility; official IDs use `bot_platform=qq_official`.
- OneBot rollback entrypoint remains runnable throughout migration.

## Dependency rules
- Reuse current stdlib, Pillow, event_hub and service logic.
- No in-process scheduler dependency; provide a single-run command.
- Official runtime may add only Tencent's official Python SDK after version verification.
- SDK objects and API error types stay inside adapter directories.
- Credentials come only from environment variables and never appear in logs or JSON state.

## Testing rules
- Use only `data/testing.db`; never run database-rebuilding suites in parallel.
- All transport tests use fakes and local fixtures.
- Daily tests use temporary directories and frozen Asia/Singapore times.
- Verify both generated PNGs visually before any real-send authorization.
- A send test must prove partial success resumes without resending the first image.
