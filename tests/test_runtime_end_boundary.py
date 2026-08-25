from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(WORKSPACE_ROOT / "计分器") not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT / "计分器"))

import services.bot_private_service as bot_private_service  # noqa: E402
import services.discord_lock_refresh_service as discord_lock_refresh_service  # noqa: E402
import services.settlement_service as settlement_service  # noqa: E402
import services.timed_reservation_service as timed_reservation_service  # noqa: E402


class _FixedDateTime(datetime):
    current = datetime(2026, 8, 24, tzinfo=timezone(timedelta(hours=8)))

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return _Cursor(self.rows) if "SELECT" in sql else _Cursor([])


class RuntimeEndBoundaryTests(TestCase):
    def test_organizer_active_guards_end_at_exact_boundary(self):
        import importlib.util

        target = WORKSPACE_ROOT / "计分器" / "organizer_timed_scoring.py"
        spec = importlib.util.spec_from_file_location("runtime_organizer_boundary", target)
        organizer = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(organizer)
        start = _FixedDateTime.current - timedelta(hours=1)
        end = _FixedDateTime.current
        event = SimpleNamespace(start_time=start, end_time=end)

        with patch.object(organizer, "now_local", return_value=end):
            self.assertFalse(organizer.event_info_is_active_or_soon(event))
        with patch.object(organizer, "now_local", return_value=end - timedelta(microseconds=1)):
            self.assertTrue(organizer.event_info_is_active_or_soon(event))

        state = {"start_time": start.isoformat(), "end_time": end.isoformat()}
        with patch.object(organizer, "now_local", return_value=end):
            self.assertFalse(organizer.state_is_active_or_soon(state))
        with patch.object(organizer, "now_local", return_value=end - timedelta(microseconds=1)):
            self.assertTrue(organizer.state_is_active_or_soon(state))

    def test_event_hub_runtime_status_ends_at_exact_boundary(self):
        import importlib.util

        target = WORKSPACE_ROOT / "计分器" / "organizer_event_hub.py"
        spec = importlib.util.spec_from_file_location("runtime_event_hub_boundary", target)
        event_hub = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(event_hub)
        row = {
            "start_time": "2026-08-23T00:00:00+08:00",
            "end_time": "2026-08-24T00:00:00+08:00",
        }
        with patch.object(event_hub, "datetime", _FixedDateTime):
            self.assertEqual(event_hub.get_event_runtime_status(row), "已完赛")
            _FixedDateTime.current -= timedelta(microseconds=1)
            try:
                self.assertEqual(event_hub.get_event_runtime_status(row), "进行中")
            finally:
                _FixedDateTime.current += timedelta(microseconds=1)

    def test_generated_html_uses_exclusive_end_clock(self):
        for relative in (
            "计分器/organizer_timed_scoring.py",
            "计分器/player_timed_scoring.py",
            "赛事中台/计分器/organizer_timed_scoring.py",
            "赛事中台/计分器/player_timed_scoring.py",
        ):
            source = (WORKSPACE_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("now < endMs", source, relative)
            self.assertNotIn("now <= endMs", source, relative)

    def test_event_hub_refresh_window_rejects_exact_end(self):
        for relative in ("计分器/organizer_event_hub.py", "赛事中台/计分器/organizer_event_hub.py"):
            source = (WORKSPACE_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("candidate_time >= end_dt", source, relative)
            self.assertNotIn("candidate_time > end_dt", source, relative)

    def test_bot_runtime_status_ends_at_exact_boundary(self):
        row = {"status": "active", "start_time": "2026-08-23T00:00:00+08:00", "end_time": "2026-08-24T00:00:00+08:00"}
        with patch.object(bot_private_service, "datetime", _FixedDateTime):
            self.assertEqual(bot_private_service._runtime_status_label(row), "已结束")
            _FixedDateTime.current -= timedelta(microseconds=1)
            try:
                self.assertEqual(bot_private_service._runtime_status_label(row), "进行中")
            finally:
                _FixedDateTime.current += timedelta(microseconds=1)

    def test_discord_window_rejects_exact_end_and_accepts_microsecond_before(self):
        start = datetime.fromisoformat("2026-08-23T00:00:00+08:00")
        end = datetime.fromisoformat("2026-08-24T00:00:00+08:00")
        self.assertFalse(discord_lock_refresh_service._in_event_window(end, start, end))
        self.assertTrue(
            discord_lock_refresh_service._in_event_window(
                end - timedelta(microseconds=1), start, end
            )
        )

    def test_settlement_window_rejects_exact_end_for_start_and_end(self):
        start = datetime.fromisoformat("2026-08-23T00:00:00+08:00")
        end = datetime.fromisoformat("2026-08-24T00:00:00+08:00")
        exact = {"started_at": "2026-08-24T00:00:00+08:00", "ended_at": "2026-08-24T00:00:00+08:00"}
        before = {
            "started_at": "2026-08-23T23:59:59+08:00",
            "ended_at": "2026-08-23T23:59:59.999999+08:00",
        }
        self.assertFalse(settlement_service.is_record_within_event_window(exact, start, end))
        self.assertTrue(settlement_service.is_record_within_event_window(before, start, end))

    def test_reservation_settlement_rejects_game_at_reserved_end(self):
        reserved_start = "2026-08-23T23:00:00+08:00"
        reserved_end = "2026-08-24T00:00:00+08:00"
        row = {
            "id": 1,
            "event_id": 2,
            "player_id": 3,
            "status": "reserved",
            "settlement_payload_json": "{}",
            "reserved_start_time": reserved_start,
            "reserved_end_time": reserved_end,
            "event_code": "TIMED_BOUNDARY",
            "platform_id": 4,
            "variant_code": "2x4",
        }
        games = [
            {"id": "at-end", "started_at": "2026-08-23T23:59:59+08:00", "played_at": reserved_end},
            {
                "id": "before-end",
                "started_at": "2026-08-23T23:59:58+08:00",
                "played_at": "2026-08-23T23:59:59.999999+08:00",
            },
        ]
        connection = _Connection([row])
        with patch.object(timed_reservation_service, "now_local", return_value=_FixedDateTime.current), patch.object(
            timed_reservation_service, "_lookup_username", return_value="alice"
        ), patch.object(timed_reservation_service, "fetch_recent_games", return_value=games), patch.object(
            timed_reservation_service, "_extract_game_score", return_value=100
        ), patch.object(timed_reservation_service, "_score_timed_game", return_value=(10, 0)), patch.object(
            timed_reservation_service,
            "add_manual_score",
            return_value={"performance_record_id": 10},
        ), patch.object(timed_reservation_service, "settle_event"):
            result = timed_reservation_service.settle_due_reservations(connection)

        self.assertEqual(result["settled_count"], 1)
        updates = [params for sql, params in connection.executed if "UPDATE event_attempt_records" in sql]
        self.assertEqual(len(updates), 1)
        payload_updates = [params for sql, params in connection.executed if "UPDATE timed_event_reservations" in sql]
        self.assertEqual(len(payload_updates), 1)
        payload = json.loads(payload_updates[0][1])
        self.assertEqual(payload["scoring_game_count"], 1)


if __name__ == "__main__":
    main()
