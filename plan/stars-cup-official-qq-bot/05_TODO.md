# Ordered implementation checklist

## Completed and verified
- [x] Normalize Fedora/Windows path expectations and reach Bot 47/47.
- [x] Add neutral transport DTOs and preserve `services/bot_private_service.py`.
- [x] Route OneBot through its adapter while retaining NoneBot/NapCat process behavior.
- [x] Add snapshot-backed 群星杯 overview/team/player/self group query.
- [x] Add immutable daily export, staged cache, atomic latest and per-image delivery state.
- [x] Add official C2C/group event adapter, passive text/media reply and proactive group image send.
- [x] Pin Tencent `qq-botpy==1.2.1` separately and isolate SDK route differences.
- [x] Add 2026 local-file chunk upload and guarded WebSocket runtime.
- [x] Add ready-gated in-process daily scheduler and offline SDK/artifact preflight.
- [x] Add official-route fixtures for help, bind, registration, reservation, dashboard, Verse, score gate, admin and replay flows.
- [x] Pass 12 basic tests, Bot 47/47, full unittest 185, fast suite, `pip check` and real-SDK offline preflight.
- [x] Push implementation baseline `23fd1e7` to `origin/codex/stars-cup-bot-prep-20260727`.

## Next implementation batch — execute strictly in order
- [ ] 1. Add failing scheduler tests for permanent, transient, wrapped-network and unknown exceptions.
- [ ] 2. Implement `classify_scheduler_exception` only; replace catch-all retry behavior and rerun scheduler tests.
- [ ] 3. Add pure relationship-event mapping tests using SDK 1.2.1 field names.
- [ ] 4. Implement `relationship_state.py` with atomic, hashed target state and no runtime wiring.
- [ ] 5. Add runtime callback tests for the six relationship events; prove no SQLite/business dispatch and no raw ID leakage.
- [ ] 6. Wire callbacks and expose relationship state in preflight while leaving platform capabilities `UNKNOWN`.
- [ ] 7. Add scheduler gate tests for rejected/removed, joined/receivable and unknown states.
- [ ] 8. Wire the target-state gate before export/send.
- [ ] 9. Add restart fixtures for both-images-sent, partial sent, retryable, terminal, unknown, corrupt and wrong-target delivery state.
- [ ] 10. Implement durable scheduler reconciliation without changing delivery JSON schema unless tests prove necessary.
- [ ] 11. Add official C2C end-to-end fixtures for `赛事`, `我的报名`, `我的成绩`, `我的档案` and `更多`.
- [ ] 12. Update `.env.bot.example`/`docs/README.md` only for actual new state/report behavior.
- [ ] 13. Run focused official tests, 12 basic, Bot 47/47, full unittest and fast suite sequentially.
- [ ] 14. Inspect diff, commit only allowed files and push the feature branch.

## First minimum change
- Files: `tests/test_official_qq_scheduler.py`, then `bot_official_qq/scheduler.py`.
- Change: add and implement the pure `classify_scheduler_exception` helper; do not touch runtime, transport, business services or data.
- Proof: existing four scheduler tests plus new classification tests all pass; full worktree diff contains only those two files.
- Rollback: revert that isolated commit; no state/schema/config migration exists.

## External authorization gate
- [ ] User confirms official application, permissions, review status, test identity/group and allowed time window.
- [ ] Inspect platform settings or record them as user-supplied evidence; do not infer from preflight.
- [ ] Run one passive group-@ text canary with scheduler disabled.
- [ ] Run one C2C help/bind canary.
- [ ] Run one total-image proactive canary, then one detail-image canary.
- [ ] Enable the scheduler only after both image receipts/audit outcomes are confirmed.
- [ ] Observe three scheduled deliveries while OneBot remains active as rollback.

## Later cleanup gate
- [ ] Restore remaining partial legacy rows in small batches.
- [ ] Decide whether score submission may be enabled for an isolated test event.
- [ ] Remove no CQ/OneBot compatibility code until official canaries are accepted and the user starts a separate cleanup phase.
