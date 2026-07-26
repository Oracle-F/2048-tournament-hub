from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import TestCase, main


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CALCULATOR_DIR = WORKSPACE_ROOT / "计分器"
TARGET = CALCULATOR_DIR / "organizer_timed_scoring.py"
if str(CALCULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(CALCULATOR_DIR))

SPEC = importlib.util.spec_from_file_location("organizer_timed_scoring_under_test", TARGET)
organizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(organizer)


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row):
        self.row = row

    def execute(self, _sql, _params):
        return _Cursor(self.row)


class OrganizerTimedScoringTests(TestCase):
    def test_historical_api_string_ids_are_scored(self):
        snapshot = WORKSPACE_ROOT / "赛事中台" / "tests" / "api_snapshots" / "player_profile_response_2x4.json"
        games = json.loads(snapshot.read_text(encoding="utf-8"))["games"]
        start_time = datetime.fromisoformat("2026-05-28T00:00:00+00:00")
        end_time = datetime.fromisoformat("2026-05-30T00:00:00+00:00")

        result = organizer.score_games("alice", "Alice", "2x4", games, start_time, end_time)

        self.assertEqual(result.all_window_game_count, 2)
        self.assertEqual(result.scoring_game_count, 1)
        self.assertGreater(result.total_points, 0)
        self.assertEqual([game.game_id for game in result.games], ["g2402"])

    def test_nested_runtime_script_keeps_running_after_event_end(self):
        runtime_script = WORKSPACE_ROOT / "赛事中台" / "计分器" / "organizer_timed_scoring.py"
        source = runtime_script.read_text(encoding="utf-8")

        self.assertIn("while True:", source)
        self.assertIn("仍持续按输入的比赛时间范围同步；程序不会自动退出。", source)
        self.assertNotIn("export_expired_event_results", source)

    def test_loading_directly_created_event_supports_naive_stored_times(self):
        connection = _Connection(
            {
                "event_code": "TIMED_001",
                "event_name": "历史限时赛",
                "start_time": "2026-07-01 10:00:00",
                "end_time": "2026-07-01 11:00:00",
                "seal_time": None,
                "status": "ready",
                "competition_type": "timed_scoring",
                "variant_code": "2x4",
            }
        )

        row, event_info = organizer.load_timed_event_from_hub(connection, "TIMED_001")

        self.assertEqual(row["event_code"], "TIMED_001")
        self.assertEqual(event_info.start_time.tzinfo, organizer.LOCAL_TIMEZONE)
        self.assertEqual(event_info.end_time - event_info.start_time, timedelta(hours=1))


if __name__ == "__main__":
    main()