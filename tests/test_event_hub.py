from __future__ import annotations

import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import Mock, patch
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import event_hub  # noqa: E402
from event_hub import (  # noqa: E402
    LiveFetchError,
    _interactive_menu,
    _load_cached_live_records,
    _load_live_cache,
    _query_live_records_from_roster,
    _select_live_query_window,
    roster_from_xlsx,
)
from services.match_rank_image_service import _build_roster_player, _roster_account_aliases  # noqa: E402


def _write_minimal_xlsx(path: Path, header: list[str] | None = None) -> None:
    header = header or ["第{}档".format(index) for index in range(1, 13)]
    teams = [
        ["{}队".format(code)] + ["{}_{}".format(code, index) for index in range(1, 13)]
        for code in "ABCDEF"
    ]
    teams[0][1] = "XLB"
    values = ["第二届群星杯抽签分队结果"] + header + [value for row in teams for value in row]
    indexes = {value: index for index, value in enumerate(values)}

    shared_strings = "".join(
        "<si><t>{}</t></si>".format(escape(value)) for value in values
    )
    rows = []

    def cell(column: int, row: int, value: str) -> str:
        ref = ""
        number = column
        while number:
            number, remainder = divmod(number - 1, 26)
            ref = chr(ord("A") + remainder) + ref
        return '<c r="{}{}" t="s"><v>{}</v></c>'.format(ref, row, indexes[value])

    rows.append('<row r="1">{}</row>'.format(cell(1, 1, values[0])))
    rows.append(
        '<row r="2">{}</row>'.format("".join(cell(index + 2, 2, value) for index, value in enumerate(header)))
    )
    for row_index, row in enumerate(teams, 3):
        rows.append(
            '<row r="{}">{}</row>'.format(
                row_index,
                "".join(cell(column, row_index, value) for column, value in enumerate(row, 1)),
            )
        )

    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{}</sst>'.format(shared_strings),
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{}</sheetData></worksheet>'.format("".join(rows)),
        )


def _small_live_roster() -> dict:
    return {
        "competition": {
            "code": "annual_4x4_2026",
            "start_time": "2026-07-27 00:00:00+08:00",
            "end_time": "2026-08-24T00:00:00+08:00",
        },
        "teams": [
            {
                "code": "A",
                "players": [{"username": "FastUser", "verse": "FastUser"}],
            }
        ],
    }


def _normalized_record(record_id: str, ended_at: str) -> dict:
    return {
        "record_id": record_id,
        "score": 123456,
        "board_sum": 30,
        "started_at": ended_at,
        "ended_at": ended_at,
        "source": "verse_live",
    }


class EventHubRosterTests(TestCase):
    def test_interactive_menu_can_exit_without_arguments(self):
        output = StringIO()
        with patch("builtins.input", side_effect=["0"]), redirect_stdout(output):
            self.assertEqual(_interactive_menu(), 0)
        self.assertIn("群星杯赛事 Hub", output.getvalue())
        self.assertIn("已退出", output.getvalue())

    def test_interactive_export_uses_fixed_paths_without_extra_prompts(self):
        output = StringIO()
        with patch("builtins.input", side_effect=["4", "0"]), patch(
            "event_hub.export_rank_images",
            return_value={"total": "总榜.png", "detail": "六队明细.png", "snapshot": "榜图数据.json"},
        ) as export, redirect_stdout(output):
            self.assertEqual(_interactive_menu(), 0)

        export.assert_called_once_with(
            event_hub.DEFAULT_ROSTER,
            None,
            None,
            event_hub.DEFAULT_BACKGROUND,
            None,
        )

    def test_interactive_query_only_updates_cache(self):
        output = StringIO()
        summary = {
            "cache": "live-cache.json",
            "mode": "recent_24h",
            "range_start": "2026-08-01T00:00:00+08:00",
            "range_end": "2026-08-02T00:00:00+08:00",
        }
        with patch("builtins.input", side_effect=["3", "0"]), patch(
            "event_hub.query_live_scores",
            return_value=summary,
        ) as query, patch("event_hub.export_rank_images") as export, redirect_stdout(output):
            self.assertEqual(_interactive_menu(), 0)

        query.assert_called_once_with(event_hub.DEFAULT_ROSTER)
        export.assert_not_called()

    def test_live_export_uses_saved_cache_without_network(self):
        roster = _small_live_roster()
        records = {"fastuser": [_normalized_record("game-1", "2026-08-02T12:00:00+08:00")]}
        cache = {"last_successful_query_at": "2026-08-02T18:00:00+08:00"}
        rendered = {"total": "总榜.png", "detail": "六队明细.png", "snapshot": "榜图数据.json"}
        with patch("event_hub._load_json", return_value=roster), patch(
            "event_hub._load_cached_live_records",
            return_value=(records, cache),
        ), patch(
            "event_hub.build_snapshot_from_roster_records",
            return_value={"snapshot": True},
        ) as build, patch(
            "event_hub.render_match_rank_images",
            return_value=rendered,
        ), patch(
            "event_hub._fetch_json_fast",
            side_effect=AssertionError("export must not access Verse"),
        ):
            result = event_hub.export_rank_images(
                Path("roster.json"),
                None,
                Path("output"),
                Path("background.png"),
                None,
            )

        self.assertEqual(result, rendered)
        self.assertEqual(
            build.call_args.args[0]["as_of"],
            "2026-08-02T18:00:00+08:00",
        )

    def test_http_429_honors_retry_after_and_rebuilds_connection(self):
        limited = Mock(status_code=429, headers={"Retry-After": "0.5"})
        success = Mock(status_code=200, headers={})
        success.json.return_value = {"totalGames": 0, "games": []}
        session = Mock()
        session.get.side_effect = [limited, success]
        with patch("event_hub._http_session", return_value=session), patch(
            "event_hub._reset_http_session",
        ) as reset, patch("event_hub.sleep") as wait:
            payload = event_hub._fetch_json_fast("https://example.test/user")

        self.assertEqual(payload["games"], [])
        self.assertEqual(session.get.call_count, 2)
        reset.assert_called_once_with()
        wait.assert_called_once_with(0.5)

    def test_non_retryable_http_error_fails_immediately(self):
        response = Mock(status_code=404, headers={})
        session = Mock()
        session.get.return_value = response
        with patch("event_hub._http_session", return_value=session), self.assertRaisesRegex(
            LiveFetchError,
            "HTTP 404",
        ):
            event_hub._fetch_json_fast("https://example.test/user")

        self.assertEqual(session.get.call_count, 1)

    def test_live_reader_uses_verse_games_and_skips_xlb(self):
        with tempfile.TemporaryDirectory(prefix="event-hub-roster-") as directory:
            source = Path(directory) / "名单.xlsx"
            _write_minimal_xlsx(source)
            roster = roster_from_xlsx(source)

        game = {
            "id": "game-1",
            "score": 123456,
            "started_at": "2026-07-27T01:00:00+08:00",
            "played_at": "2026-07-27T01:05:00+08:00",
            "board": [[2, 4], [8, 16]],
        }
        payload = {"totalGames": 1, "games": [game]}
        now = event_hub._parse_event_datetime("2026-07-27T02:00:00+08:00")
        with tempfile.TemporaryDirectory(prefix="event-hub-cache-") as directory, patch(
            "event_hub._fetch_json_fast",
            return_value=payload,
        ) as fetch:
            records, query = _query_live_records_from_roster(
                roster,
                cache_path=Path(directory) / "live-cache.json",
                now=now,
            )

        self.assertNotIn("xlb", records)
        self.assertEqual(records["a_2"][0]["score"], 123456)
        self.assertEqual(records["a_2"][0]["board_sum"], 30)
        self.assertEqual(fetch.call_count, 71)
        self.assertEqual(query["mode"], "full")

    def test_repeated_api_page_fails_instead_of_querying_forever(self):
        games = [
            {
                "id": "game-{}".format(index),
                "score": 1000 + index,
                "played_at": "2026-07-27T01:{:02d}:00+08:00".format(index),
                "board": [[2, 4], [8, 16]],
            }
            for index in range(50)
        ]
        payload = {"totalGames": 100, "games": games}
        with patch("event_hub._fetch_json_fast", return_value=payload) as fetch, self.assertRaisesRegex(
            LiveFetchError,
            "避免无限查询",
        ):
            event_hub._fetch_games_for_window_fast(
                "RepeatUser",
                event_hub._parse_event_datetime("2026-07-27T00:00:00+08:00"),
                event_hub._parse_event_datetime("2026-07-28T00:00:00+08:00"),
            )
        self.assertEqual(fetch.call_count, 2)

    def test_missing_start_is_retained_as_manual_audit_candidate(self):
        game = {
            "id": "missing-start",
            "score": 123456,
            "played_at": "2026-07-27T01:00:00+08:00",
            "board": [[2, 4], [8, 16]],
        }

        record = event_hub._verse_game_record(game)

        self.assertIsNotNone(record)
        self.assertEqual(record["started_at"], game["played_at"])
        self.assertEqual(record["ended_at"], game["played_at"])

    def test_live_record_window_is_half_open_at_end(self):
        start = event_hub._parse_event_datetime("2026-07-27T00:00:00+08:00")
        end = event_hub._parse_event_datetime("2026-08-24T00:00:00+08:00")
        accepted = _normalized_record("before-end", "2026-08-23T23:59:59.999999+08:00")
        rejected = _normalized_record("at-end", "2026-08-24T00:00:00+08:00")

        merged = event_hub._merge_live_records(
            [],
            [accepted, rejected],
            start,
            end,
        )

        self.assertEqual([record["record_id"] for record in merged], ["before-end"])

    def test_in_window_game_with_broken_board_fails_instead_of_silently_disappearing(self):
        game = {
            "id": "broken-game",
            "score": 123456,
            "played_at": "2026-07-27T01:00:00+08:00",
            "board": None,
        }
        with patch("event_hub._fetch_games_for_window_fast", return_value=[game]), self.assertRaisesRegex(
            LiveFetchError,
            "无法解析",
        ):
            event_hub._fetch_one_live_player_records(
                "BrokenUser",
                event_hub._parse_event_datetime("2026-07-27T00:00:00+08:00"),
                event_hub._parse_event_datetime("2026-07-28T00:00:00+08:00"),
            )

    def test_game_with_start_after_end_is_rejected(self):
        game = {
            "id": "reversed-time",
            "score": 123456,
            "started_at": "2026-07-27T02:00:00+08:00",
            "played_at": "2026-07-27T01:00:00+08:00",
            "board": [[2, 4], [8, 16]],
        }
        with patch(
            "event_hub._fetch_json_fast",
            return_value={"totalGames": 1, "games": [game]},
        ), self.assertRaisesRegex(LiveFetchError, "开始时间晚于结束时间"):
            event_hub._fetch_games_for_window_fast(
                "BrokenTimeUser",
                event_hub._parse_event_datetime("2026-07-27T00:00:00+08:00"),
                event_hub._parse_event_datetime("2026-07-28T00:00:00+08:00"),
            )

    def test_query_window_uses_23_5_hour_threshold(self):
        event_start = event_hub._parse_event_datetime("2026-07-27T00:00:00+08:00")
        event_end = event_hub._parse_event_datetime("2026-08-24T00:00:00+08:00")
        cache = {"last_successful_query_at": "2026-08-02T12:00:00+08:00"}

        start, end, mode = _select_live_query_window(
            cache,
            event_start,
            event_end,
            now=event_hub._parse_event_datetime("2026-08-02T18:00:00+08:00"),
        )
        self.assertEqual(mode, "recent_24h")
        self.assertEqual(start.isoformat(), "2026-08-01T18:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-08-02T18:00:00+08:00")

        start, _, mode = _select_live_query_window(
            cache,
            event_start,
            event_end,
            now=event_hub._parse_event_datetime("2026-08-03T11:30:00+08:00"),
        )
        self.assertEqual(mode, "recent_24h")
        self.assertEqual(start.isoformat(), "2026-08-02T11:30:00+08:00")

        start, _, mode = _select_live_query_window(
            cache,
            event_start,
            event_end,
            now=event_hub._parse_event_datetime("2026-08-03T11:30:01+08:00"),
        )
        self.assertEqual(mode, "catch_up")
        self.assertEqual(start.isoformat(), "2026-08-02T11:30:00+08:00")

        start, end, mode = _select_live_query_window(
            cache,
            event_start,
            event_end,
            now=event_hub._parse_event_datetime("2026-08-04T18:00:00+08:00"),
        )
        self.assertEqual(mode, "catch_up")
        self.assertEqual(start.isoformat(), "2026-08-02T11:30:00+08:00")
        self.assertEqual(end.isoformat(), "2026-08-04T18:00:00+08:00")

        start, _, mode = _select_live_query_window(
            cache,
            event_start,
            event_end,
            full=True,
            now=event_hub._parse_event_datetime("2026-08-04T18:00:00+08:00"),
        )
        self.assertEqual(mode, "full")
        self.assertEqual(start, event_start)

        with self.assertRaisesRegex(ValueError, "检查系统时间"):
            _select_live_query_window(
                {"last_successful_query_at": "2026-08-05T00:00:00+08:00"},
                event_start,
                event_end,
                now=event_hub._parse_event_datetime("2026-08-04T18:00:00+08:00"),
            )

    def test_live_cache_records_each_query_range_and_merges_records(self):
        roster = _small_live_roster()
        first_record = _normalized_record("game-1", "2026-08-02T12:00:00+08:00")
        second_record = _normalized_record("game-2", "2026-08-02T17:00:00+08:00")
        with tempfile.TemporaryDirectory(prefix="event-hub-cache-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            first_now = event_hub._parse_event_datetime("2026-08-02T12:00:00+08:00")
            with patch(
                "event_hub._fetch_one_live_player_records",
                return_value=[first_record],
            ) as fetch:
                records, query = _query_live_records_from_roster(
                    roster,
                    cache_path=cache_path,
                    workers=1,
                    now=first_now,
                )
            self.assertEqual(query["mode"], "full")
            self.assertEqual(fetch.call_args.args[1].isoformat(), "2026-07-27T00:00:00+08:00")
            self.assertEqual([item["record_id"] for item in records["fastuser"]], ["game-1"])

            second_now = event_hub._parse_event_datetime("2026-08-02T18:00:00+08:00")
            with patch(
                "event_hub._fetch_one_live_player_records",
                return_value=[first_record, second_record],
            ) as fetch:
                records, query = _query_live_records_from_roster(
                    roster,
                    cache_path=cache_path,
                    workers=1,
                    now=second_now,
                )

            self.assertEqual(query["mode"], "recent_24h")
            self.assertEqual(fetch.call_args.args[1].isoformat(), "2026-08-01T18:00:00+08:00")
            self.assertEqual(
                [item["record_id"] for item in records["fastuser"]],
                ["game-1", "game-2"],
            )
            saved = event_hub.json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["query_history"]), 2)
            self.assertEqual(
                saved["query_history"][-1]["range_start"],
                "2026-08-01T18:00:00+08:00",
            )
            self.assertEqual(
                saved["last_successful_query_at"],
                "2026-08-02T18:00:00+08:00",
            )

    def test_excluded_live_records_stay_filtered_on_later_queries(self):
        roster = _small_live_roster()
        first_record = _normalized_record("game-1", "2026-08-02T12:00:00+08:00")
        second_record = _normalized_record("game-2", "2026-08-02T17:00:00+08:00")
        with tempfile.TemporaryDirectory(prefix="event-hub-exclusions-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            cache = event_hub._empty_live_cache(roster["competition"])
            cache["last_successful_query_at"] = "2026-08-02T12:00:00+08:00"
            cache["excluded_records"] = [
                {
                    "record_id": "game-1",
                    "username": "FastUser",
                    "score": 123456,
                    "reason": "started_before_event",
                }
            ]
            cache["players"] = {
                "fastuser": {"username": "FastUser", "records": [first_record]}
            }
            cache_path.write_text(event_hub.json.dumps(cache), encoding="utf-8")

            with patch(
                "event_hub._fetch_one_live_player_records",
                return_value=[first_record, second_record],
            ):
                records, _query = _query_live_records_from_roster(
                    roster,
                    cache_path=cache_path,
                    workers=1,
                    now=event_hub._parse_event_datetime("2026-08-02T18:00:00+08:00"),
                )

            self.assertEqual([item["record_id"] for item in records["fastuser"]], ["game-2"])
            saved = event_hub.json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [entry["record_id"] for entry in saved["excluded_records"]],
                ["game-1"],
            )

    def test_excluded_records_are_normalized_and_persisted(self):
        roster = _small_live_roster()
        first_record = _normalized_record("game-1", "2026-08-02T12:00:00+08:00")
        second_record = _normalized_record("game-2", "2026-08-02T17:00:00+08:00")
        with tempfile.TemporaryDirectory(prefix="event-hub-exclusions-normalize-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            cache = event_hub._empty_live_cache(roster["competition"])
            cache["last_successful_query_at"] = "2026-08-02T12:00:00+08:00"
            cache["excluded_records"] = [
                {"record_id": "game-1", "reason": "started_before_event"},
                {"record_id": "game-1", "reason": "duplicate"},
                {"reason": "missing-id"},
                None,
                {"record_id": "game-2", "reason": "unverified-start"},
                {"record_id": "game-2", "reason": "duplicate"},
            ]
            cache["players"] = {
                "fastuser": {"username": "FastUser", "records": [first_record]}
            }
            cache_path.write_text(event_hub.json.dumps(cache), encoding="utf-8")

            with patch(
                "event_hub._fetch_one_live_player_records",
                return_value=[first_record, second_record],
            ):
                records, _query = _query_live_records_from_roster(
                    roster,
                    cache_path=cache_path,
                    workers=1,
                    now=event_hub._parse_event_datetime("2026-08-02T18:00:00+08:00"),
                )

            self.assertEqual(records["fastuser"], [])
            saved = event_hub.json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["excluded_records"],
                [
                    {"record_id": "game-1", "reason": "started_before_event"},
                    {"record_id": "game-2", "reason": "unverified-start"},
                ],
            )

    def test_excluded_live_records_are_filtered_when_loading_export_cache(self):
        roster = _small_live_roster()
        cache = event_hub._empty_live_cache(roster["competition"])
        cache["last_successful_query_at"] = "2026-08-02T18:00:00+08:00"
        cache["excluded_records"] = [{"record_id": "game-1"}]
        cache["players"] = {
            "fastuser": {
                "username": "FastUser",
                "records": [
                    _normalized_record("game-1", "2026-08-02T12:00:00+08:00"),
                    _normalized_record("game-2", "2026-08-02T17:00:00+08:00"),
                ],
            }
        }
        with tempfile.TemporaryDirectory(prefix="event-hub-export-exclusions-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            cache_path.write_text(event_hub.json.dumps(cache), encoding="utf-8")
            records, _cache = _load_cached_live_records(roster, cache_path)

        self.assertEqual([item["record_id"] for item in records["fastuser"]], ["game-2"])

    def test_failed_refresh_does_not_replace_last_complete_cache(self):
        roster = _small_live_roster()
        record = _normalized_record("game-1", "2026-08-02T12:00:00+08:00")
        with tempfile.TemporaryDirectory(prefix="event-hub-cache-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            with patch("event_hub._fetch_one_live_player_records", return_value=[record]):
                _query_live_records_from_roster(
                    roster,
                    cache_path=cache_path,
                    workers=1,
                    now=event_hub._parse_event_datetime("2026-08-02T12:00:00+08:00"),
                )
            previous = cache_path.read_bytes()

            with patch(
                "event_hub._fetch_one_live_player_records",
                side_effect=LiveFetchError("temporary failure"),
            ) as fetch, patch(
                "event_hub._retry_delay",
                return_value=0,
            ), self.assertRaisesRegex(LiveFetchError, "旧缓存未改动"):
                _query_live_records_from_roster(
                    roster,
                    cache_path=cache_path,
                    workers=1,
                    now=event_hub._parse_event_datetime("2026-08-02T18:00:00+08:00"),
                )

            self.assertEqual(cache_path.read_bytes(), previous)
            self.assertEqual(
                fetch.call_count,
                1 + event_hub.LIVE_FAILED_PLAYER_RETRY_ROUNDS,
            )
            report_path = cache_path.with_name(cache_path.stem + "_last_failure.json")
            report = event_hub.json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["failures"][0]["username"], "FastUser")

    def test_failed_player_is_retried_separately_and_can_recover(self):
        roster = _small_live_roster()
        record = _normalized_record("game-1", "2026-08-02T12:00:00+08:00")
        with tempfile.TemporaryDirectory(prefix="event-hub-cache-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            failure_path = cache_path.with_name(cache_path.stem + "_last_failure.json")
            failure_path.write_text("stale", encoding="utf-8")
            with patch(
                "event_hub._fetch_one_live_player_records",
                side_effect=[LiveFetchError("temporary failure"), [record]],
            ) as fetch, patch("event_hub._retry_delay", return_value=0):
                records, query = _query_live_records_from_roster(
                    roster,
                    cache_path=cache_path,
                    workers=8,
                    now=event_hub._parse_event_datetime("2026-08-02T12:00:00+08:00"),
                )

            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(query["player_count"], 1)
            self.assertEqual(records["fastuser"][0]["record_id"], "game-1")
            self.assertTrue(cache_path.exists())
            self.assertFalse(failure_path.exists())

    def test_malformed_player_cache_is_rejected_before_export(self):
        roster = _small_live_roster()
        competition = roster["competition"]
        cache = event_hub._empty_live_cache(competition)
        cache["last_successful_query_at"] = "2026-08-02T18:00:00+08:00"
        cache["players"] = {"fastuser": {"username": "FastUser", "records": "broken"}}
        with tempfile.TemporaryDirectory(prefix="event-hub-cache-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            cache_path.write_text(event_hub.json.dumps(cache), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "缺少或损坏"):
                _load_cached_live_records(roster, cache_path)

    def test_incompatible_v3_cache_is_not_reused_by_schema2_reader(self):
        roster = _small_live_roster()
        cache = event_hub._empty_live_cache(roster["competition"])
        cache["schema_version"] = 3
        cache["last_successful_query_at"] = "2026-08-02T18:00:00+08:00"
        cache["players"] = {
            "fastuser": {
                "username": "FastUser",
                "records": [_normalized_record("strict-v3", "2026-08-02T12:00:00+08:00")],
            }
        }
        with tempfile.TemporaryDirectory(prefix="event-hub-cache-v3-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            cache_path.write_text(event_hub.json.dumps(cache), encoding="utf-8")

            loaded = event_hub._load_live_cache(cache_path, roster["competition"])

        self.assertEqual(loaded["schema_version"], event_hub.LIVE_CACHE_SCHEMA_VERSION)
        self.assertEqual(loaded["last_successful_query_at"], "")
        self.assertEqual(loaded["players"], {})

    def test_xlsx_import_requires_six_teams_and_one_player_per_tier(self):
        with tempfile.TemporaryDirectory(prefix="event-hub-roster-") as directory:
            source = Path(directory) / "名单.xlsx"
            _write_minimal_xlsx(source)

            roster = roster_from_xlsx(source)

        self.assertEqual(roster["competition"]["code"], "annual_4x4_2026")
        self.assertEqual(len(roster["teams"]), 6)
        self.assertEqual(sum(len(team["players"]) for team in roster["teams"]), 72)
        self.assertEqual(
            [player["tier"] for player in roster["teams"][0]["players"]],
            list(range(1, 13)),
        )
        self.assertTrue(roster["teams"][0]["players"][0]["manual_only"])
        self.assertEqual(roster["teams"][1]["players"][5]["verse"], "B_6")

    def test_manual_only_player_skips_database_aliases_and_uses_manual_records(self):
        with tempfile.TemporaryDirectory(prefix="event-hub-roster-") as directory:
            source = Path(directory) / "名单.xlsx"
            _write_minimal_xlsx(source)
            roster = roster_from_xlsx(source)

        self.assertNotIn("xlb", _roster_account_aliases(roster))
        player = dict(roster["teams"][0]["players"][0])
        player["manual_records"] = [
            {"score": 100, "board_sum": 10},
            {"score": 300, "board_sum": 30},
            {"score": 200, "board_sum": 20},
            {"score": 50, "board_sum": 999},
        ]
        result = _build_roster_player(player, {}, None, None, 3, player["manual_records"])

        self.assertEqual(result["top_scores"], [300, 200, 100])
        self.assertEqual(result["total_board_sum"], 60)

    def test_reimport_preserves_manual_supplements_and_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="event-hub-roster-") as directory:
            source = Path(directory) / "名单.xlsx"
            output = Path(directory) / "roster.json"
            _write_minimal_xlsx(source)
            event_hub.import_roster(source, output)
            event_hub.supplement_manual_record(output, "XLB", 640, 190)

            with self.assertRaisesRegex(ValueError, "相同的人工成绩"):
                event_hub.supplement_manual_record(output, "XLB", 640, 190)

            roster, _ = event_hub.import_roster(source, output, force=True)

        xlb = roster["teams"][0]["players"][0]
        self.assertEqual(len(xlb["manual_records"]), 1)
        self.assertEqual(xlb["manual_records"][0]["score"], 640)

    def test_reimport_preserves_approved_screenshot_for_regular_player_and_deduplicates_source_id(self):
        with tempfile.TemporaryDirectory(prefix="event-hub-roster-screenshot-") as directory:
            source = Path(directory) / "名单.xlsx"
            output = Path(directory) / "roster.json"
            _write_minimal_xlsx(source)
            event_hub.import_roster(source, output)
            roster = event_hub._load_json(output)
            regular = roster["teams"][0]["players"][1]
            regular["manual_records"] = [
                {
                    "score": 100,
                    "board_sum": 10,
                    "source": "organizer_approved_screenshot",
                    "source_record_id": "annual_4x4_2026:screenshot:A_2:abc",
                    "evidence_sha256": "abc",
                },
                {
                    "score": 101,
                    "board_sum": 11,
                    "source": "organizer_approved_screenshot",
                    "source_record_id": "annual_4x4_2026:screenshot:A_2:abc",
                    "evidence_sha256": "abc",
                },
            ]
            event_hub.write_roster(roster, output, force=True)

            refreshed, _ = event_hub.import_roster(source, output, force=True)

        regular = refreshed["teams"][0]["players"][1]
        self.assertEqual(len(regular["manual_records"]), 1)
        self.assertEqual(regular["manual_records"][0]["source_record_id"], "annual_4x4_2026:screenshot:A_2:abc")
        self.assertEqual(regular["manual_records"][0]["score"], 100)

    def test_manual_supplement_rejects_invalid_values_and_times(self):
        with tempfile.TemporaryDirectory(prefix="event-hub-roster-") as directory:
            source = Path(directory) / "名单.xlsx"
            output = Path(directory) / "roster.json"
            _write_minimal_xlsx(source)
            event_hub.import_roster(source, output)

            with self.assertRaisesRegex(ValueError, "盘面和必须大于 0"):
                event_hub.supplement_manual_record(output, "XLB", 100, 0)
            with self.assertRaisesRegex(ValueError, "不在比赛时间内"):
                event_hub.supplement_manual_record(
                    output,
                    "XLB",
                    100,
                    10,
                    ended_at="2026-07-26T23:59:00+08:00",
                )

    def test_manual_record_at_exact_end_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="event-hub-roster-boundary-") as directory:
            source = Path(directory) / "名单.xlsx"
            output = Path(directory) / "roster.json"
            _write_minimal_xlsx(source)
            event_hub.import_roster(source, output)

            with self.assertRaisesRegex(ValueError, "不在比赛时间内"):
                event_hub.supplement_manual_record(
                    output,
                    "XLB",
                    100,
                    10,
                    ended_at="2026-08-24T00:00:00+08:00",
                )

            event_hub.supplement_manual_record(
                output,
                "XLB",
                101,
                11,
                ended_at="2026-08-23T23:59:59.999999+08:00",
            )
            saved = event_hub.json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["teams"][0]["players"][0]["manual_records"][-1]["score"], 101)

    def test_end_only_cache_identity_migration_keeps_history_players_and_exclusions(self):
        roster = _small_live_roster()
        old_cache = event_hub._empty_live_cache(roster["competition"])
        old_cache["end_time"] = "2026-08-24T23:59:59+08:00"
        old_cache["last_successful_query_at"] = "2026-08-23T20:00:00+08:00"
        old_cache["query_history"] = [{"mode": "full", "range_end": "2026-08-23T20:00:00+08:00"}]
        old_cache["excluded_records"] = [{"record_id": "qumark-1751892", "reason": "started_before_event"}]
        old_cache["players"] = {
            "fastuser": {
                "username": "FastUser",
                "records": [_normalized_record("game-1", "2026-08-23T23:59:59.999999+08:00")],
            }
        }
        with tempfile.TemporaryDirectory(prefix="event-hub-cache-boundary-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            cache_path.write_text(event_hub.json.dumps(old_cache), encoding="utf-8")
            loaded = _load_live_cache(
                cache_path,
                {
                    **roster["competition"],
                    "end_time": "2026-08-24T00:00:00+08:00",
                },
            )

        self.assertEqual(loaded["end_time"], "2026-08-24T00:00:00+08:00")
        self.assertEqual(loaded["players"], old_cache["players"])
        self.assertEqual(loaded["query_history"], old_cache["query_history"])
        self.assertEqual(loaded["excluded_records"], old_cache["excluded_records"])

    def test_end_only_cache_migration_refuses_record_at_new_end(self):
        roster = _small_live_roster()
        old_cache = event_hub._empty_live_cache(roster["competition"])
        old_cache["end_time"] = "2026-08-24T23:59:59+08:00"
        old_cache["players"] = {
            "fastuser": {
                "username": "FastUser",
                "records": [_normalized_record("game-1", "2026-08-24T00:00:00+08:00")],
            }
        }
        with tempfile.TemporaryDirectory(prefix="event-hub-cache-boundary-reject-") as directory:
            cache_path = Path(directory) / "live-cache.json"
            cache_path.write_text(event_hub.json.dumps(old_cache), encoding="utf-8")
            loaded = _load_live_cache(
                cache_path,
                {
                    **roster["competition"],
                    "end_time": "2026-08-24T00:00:00+08:00",
                },
            )

        self.assertEqual(loaded["end_time"], "2026-08-24T00:00:00+08:00")
        self.assertEqual(loaded["players"], {})

    def test_xlsx_import_rejects_non_sequential_tiers(self):
        with tempfile.TemporaryDirectory(prefix="event-hub-roster-") as directory:
            source = Path(directory) / "名单.xlsx"
            headers = ["第{}档".format(index) for index in range(1, 13)]
            headers[1] = "第1档"
            _write_minimal_xlsx(source, headers)

            with self.assertRaisesRegex(ValueError, "第 1–12 档顺序"):
                roster_from_xlsx(source)


if __name__ == "__main__":
    main()
