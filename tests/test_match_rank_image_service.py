from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.match_rank_image_service import (  # noqa: E402
    _build_roster_player,
    _build_snapshot_from_records,
    _date_only,
    _datetime_to_minute,
    _payload_board_sum,
    _team_completion_rate,
    _team_rating_from_players,
    _teams_in_rank_order,
    _valid_game_count,
    build_sample_snapshot,
    build_snapshot_from_roster_records,
    render_match_rank_images,
    validate_snapshot,
)


class MatchRankImageServiceTests(TestCase):
    def test_sample_snapshot_matches_fixed_two_image_contract(self):
        snapshot = validate_snapshot(build_sample_snapshot())

        self.assertEqual(len(snapshot["teams"]), 6)
        self.assertTrue(all(len(team["players"]) == 12 for team in snapshot["teams"]))
        self.assertEqual(len(snapshot["single_game_top"]), 20)
        self.assertEqual(len(snapshot["board_sum_top"]), 15)
        self.assertNotIn("rating_top", snapshot)
        self.assertTrue(all("verse" in row for row in snapshot["single_game_top"] + snapshot["board_sum_top"]))
        self.assertTrue(all("display_name" not in player and "rating" not in player for team in snapshot["teams"] for player in team["players"]))
        self.assertTrue(all("tier_rank" in player for team in snapshot["teams"] for player in team["players"]))
        self.assertTrue(all("team_rating" not in team for team in snapshot["teams"]))
        self.assertEqual(snapshot["individuals"], [])
        self.assertAlmostEqual(_team_completion_rate(snapshot["teams"][1], 3), 91.6666666667)
        self.assertAlmostEqual(_team_completion_rate(snapshot["teams"][2], 3), 83.3333333333)
        self.assertAlmostEqual(_team_completion_rate({"players": [{"top_scores": [100]}]}, 3), 2.7777777778)

    def test_team_visual_slots_stay_in_abcdef_order_while_ranks_recalculate(self):
        snapshot = validate_snapshot(build_sample_snapshot())

        self.assertEqual([team["code"] for team in snapshot["teams"]], list("ABCDEF"))
        self.assertEqual(
            {team["code"]: team["rank"] for team in snapshot["teams"]},
            {"A": 4, "B": 2, "C": 3, "D": 5, "E": 6, "F": 1},
        )
        self.assertEqual(
            [team["code"] for team in _teams_in_rank_order(snapshot)],
            ["F", "B", "C", "A", "D", "E"],
        )
        self.assertEqual(
            [team["rank"] for team in _teams_in_rank_order(snapshot)],
            [1, 2, 3, 4, 5, 6],
        )

    def test_players_are_ranked_across_teams_within_the_same_tier(self):
        snapshot = build_sample_snapshot()
        normalized = validate_snapshot(snapshot)

        self.assertEqual(
            [team["players"][0]["tier_rank"] for team in normalized["teams"]],
            [5, 3, 2, 6, 4, 1],
        )
        self.assertEqual(
            [team["players"][11]["tier_rank"] for team in normalized["teams"]],
            [1, None, None, 2, None, None],
        )

        snapshot["teams"][0]["players"][0]["total_board_sum"] = snapshot["teams"][1]["players"][0]["total_board_sum"]
        normalized = validate_snapshot(snapshot)
        self.assertEqual(normalized["teams"][0]["players"][0]["tier_rank"], 3)
        self.assertEqual(normalized["teams"][1]["players"][0]["tier_rank"], 3)
        self.assertEqual(normalized["teams"][2]["players"][0]["tier_rank"], 2)

    def test_team_order_totals_and_ranks_are_rebuilt_from_player_results(self):
        snapshot = build_sample_snapshot()
        snapshot["teams"].reverse()
        for team in snapshot["teams"]:
            team["rank"] = 1
            team["total_board_sum"] = 999999999

        normalized = validate_snapshot(snapshot)

        self.assertEqual([team["code"] for team in normalized["teams"]], list("ABCDEF"))
        self.assertEqual(
            [team["total_board_sum"] for team in normalized["teams"]],
            [1520909, 1564804, 1542897, 1511094, 1487863, 1573514],
        )
        self.assertEqual(
            {team["code"]: team["rank"] for team in normalized["teams"]},
            {"A": 4, "B": 2, "C": 3, "D": 5, "E": 6, "F": 1},
        )

    def test_team_ties_share_a_rank_and_teams_without_results_are_unranked(self):
        snapshot = build_sample_snapshot()
        snapshot["teams"][1]["players"] = json.loads(
            json.dumps(snapshot["teams"][0]["players"])
        )
        for tier, player in enumerate(snapshot["teams"][1]["players"], 1):
            player["verse"] = "B_tie_{}".format(tier)
            player["display_name"] = "B并列{}".format(tier)
        for player in snapshot["teams"][4]["players"]:
            player["total_board_sum"] = None
            player["top_scores"] = []

        normalized = validate_snapshot(snapshot)
        by_code = {team["code"]: team for team in normalized["teams"]}

        self.assertEqual(by_code["A"]["rank"], 3)
        self.assertEqual(by_code["B"]["rank"], 3)
        self.assertIsNone(by_code["E"]["rank"])

    def test_legacy_ordered_snapshot_gets_tiers_but_mixed_missing_tiers_fail(self):
        snapshot = build_sample_snapshot()
        for team in snapshot["teams"]:
            for player in team["players"]:
                player.pop("tier")
        normalized = validate_snapshot(snapshot)
        self.assertEqual(
            [player["tier"] for player in normalized["teams"][0]["players"]],
            list(range(1, 13)),
        )

        snapshot = build_sample_snapshot()
        snapshot["teams"][0]["players"][0].pop("tier")
        with self.assertRaisesRegex(ValueError, "无效或缺失的档位"):
            validate_snapshot(snapshot)

    def test_renderer_writes_only_composites_and_snapshot(self):
        background_path = WORKSPACE_ROOT / "比赛导出" / "设计草图" / "统榜_共享背景_群星杯与尚竞之勇_v8_1920x1080.png"
        self.assertTrue(background_path.exists())

        with tempfile.TemporaryDirectory(prefix="match-rank-test-") as directory:
            paths = render_match_rank_images(build_sample_snapshot(), Path(directory), background_path)

            expected_names = {
                "总榜.png",
                "六队明细.png",
                "榜图数据.json",
            }
            self.assertEqual({Path(path).name for path in paths.values()}, expected_names)
            for key in ("total", "detail"):
                with Image.open(paths[key]) as image:
                    self.assertEqual(image.size, (1920, 1080))
                    self.assertEqual(image.mode, "RGB")
            exported = json.loads(Path(paths["snapshot"]).read_text(encoding="utf-8"))
            self.assertEqual(exported["event"]["title"], "群星杯 · 2026 4×4 团体赛")
            self.assertEqual(exported["teams"][0]["players"][0]["top_scores"][0], 833000)
            self.assertNotIn("display_name", exported["teams"][0]["players"][0])
            self.assertNotIn("rating", exported["teams"][0]["players"][0])
            self.assertNotIn("team_rating", exported["teams"][0])
            self.assertFalse(any(Path(directory).glob("*.tmp")))

    def test_detail_time_display_uses_dates_only(self):
        self.assertEqual(_date_only("2026-07-27 00:00:00+08:00"), "2026-07-27")
        self.assertEqual(_date_only("2026-08-24T23:59:59+08:00"), "2026-08-24")
        self.assertEqual(_datetime_to_minute("2026-07-27T12:34:56+08:00"), "2026-07-27 12:34")
        self.assertEqual(_datetime_to_minute("2026.07.27 12:34"), "2026-07-27 12:34")
        self.assertEqual(_datetime_to_minute("2026-07-27T04:34:56Z"), "2026-07-27 12:34")

    def test_database_payload_board_sum_supports_nested_terminal_boards(self):
        self.assertEqual(_payload_board_sum({"board": [[2, 4], [8, 16]]}), 30)
        self.assertEqual(_payload_board_sum({"terminal_board": [[0, 2], [4, 8]]}), 14)
        self.assertIsNone(_payload_board_sum({"board": [[2, "broken"]]}))

    def test_valid_game_count_uses_all_window_games_not_only_top_three(self):
        snapshot = validate_snapshot(build_sample_snapshot())
        top_three_count = sum(
            len(player["top_scores"])
            for team in snapshot["teams"]
            for player in team["players"]
        )
        original_count = _valid_game_count(snapshot)
        original_player_count = snapshot["teams"][0]["players"][0]["games_seen"]
        snapshot["teams"][0]["players"][0]["games_seen"] = 10

        self.assertEqual(
            _valid_game_count(snapshot),
            original_count - original_player_count + 10,
        )
        self.assertGreater(_valid_game_count(snapshot), top_three_count)

    def test_duplicate_players_are_rejected_before_rendering(self):
        snapshot = build_sample_snapshot()
        snapshot["teams"][1]["players"][0]["username"] = snapshot["teams"][0]["players"][0]["display_name"]

        with self.assertRaisesRegex(ValueError, "选手重复"):
            validate_snapshot(snapshot)

    def test_individual_group_feeds_top_lists_but_not_detail_teams(self):
        snapshot = build_sample_snapshot()
        snapshot["individuals"] = [
            {
                "display_name": "个人选手",
                "username": "solo_01",
                "top_scores": [999999, 888888, 777777],
                "total_board_sum": 9999999,
                "rating": 2300,
            }
        ]
        snapshot["single_game_top"] = []
        snapshot["board_sum_top"] = []

        normalized = validate_snapshot(snapshot)

        self.assertEqual(normalized["single_game_top"][0]["group"], "个人组")
        self.assertEqual(normalized["board_sum_top"][0]["group"], "个人组")
        self.assertEqual(sum(len(team["players"]) for team in normalized["teams"]), 72)

    def test_competition_rating_selects_by_score_and_averages_board_sum(self):
        records = {
            "player": [
                {"player_id": 1, "score": 100, "board_sum": 10, "started_at": "2026-07-27 01:00:00", "ended_at": "2026-07-27 01:10:00"},
                {"player_id": 1, "score": 300, "board_sum": 30, "started_at": "2026-07-27 02:00:00", "ended_at": "2026-07-27 02:10:00"},
                {"player_id": 1, "score": 200, "board_sum": 20, "started_at": "2026-07-27 03:00:00", "ended_at": "2026-07-27 03:10:00"},
                {"player_id": 1, "score": 50, "board_sum": 1000, "started_at": "2026-07-27 04:00:00", "ended_at": "2026-07-27 04:10:00"},
                {"player_id": 1, "score": 999, "board_sum": 1, "started_at": None, "ended_at": "2026-07-27 05:10:00"},
            ]
        }

        player = _build_roster_player(
            {"username": "player"},
            records,
            start=None,
            end=None,
            required_games=3,
        )

        expected = math.log2((30 + 20 + 10) / 3) * 562.5 - 6000
        self.assertEqual(player["top_scores"], [300, 200, 100])
        self.assertEqual(player["games_seen"], 4)
        self.assertEqual(player["_all_scores"], [300, 200, 100, 50])
        self.assertAlmostEqual(player["rating"], expected)

    def test_missing_board_sum_in_a_score_selected_game_does_not_publish_partial_total(self):
        records = {
            "player": [
                {"score": 300, "board_sum": 30, "started_at": "2026-07-27 01:00:00", "ended_at": "2026-07-27 01:10:00"},
                {"score": 200, "board_sum": None, "started_at": "2026-07-27 02:00:00", "ended_at": "2026-07-27 02:10:00"},
                {"score": 100, "board_sum": 10, "started_at": "2026-07-27 03:00:00", "ended_at": "2026-07-27 03:10:00"},
            ]
        }

        player = _build_roster_player(
            {"username": "player"},
            records,
            start=None,
            end=None,
            required_games=3,
        )

        self.assertIsNone(player["total_board_sum"])
        self.assertFalse(player["board_sum_complete"])
        with self.assertRaisesRegex(ValueError, "缺少盘面数据"):
            _build_snapshot_from_records(
                records,
                {
                    "required_games": 3,
                    "teams": [{"code": "A", "players": [{"username": "player"}]}],
                    "individuals": [],
                },
                start=None,
                end=None,
                event_info={},
            )

    def test_global_single_game_top_uses_all_games_not_only_each_players_top_three(self):
        roster = {
            "competition": {
                "code": "annual_4x4_2026",
                "title": "群星杯",
                "start_time": "2026-07-27T00:00:00+08:00",
                "end_time": "2026-08-24T23:59:59+08:00",
            },
            "required_games": 3,
            "teams": [
                {
                    "code": code,
                    "name": "{}队".format(code),
                    "players": [
                        {
                            "number": "{:02d}".format(tier),
                            "tier": tier,
                            "username": "{}_{}".format(code, tier),
                            "verse": "{}_{}".format(code, tier),
                        }
                        for tier in range(1, 13)
                    ],
                }
                for code in "ABCDEF"
            ],
            "individuals": [],
        }
        records = {
            "a_1": [
                {
                    "score": score,
                    "board_sum": score // 10,
                    "started_at": "2026-07-27T01:00:00+08:00",
                    "ended_at": "2026-07-27T01:{:02d}:00+08:00".format(index),
                }
                for index, score in enumerate((1000, 900, 800, 700), 1)
            ]
        }

        snapshot = build_snapshot_from_roster_records(roster, records)

        self.assertEqual(
            [row["score"] for row in snapshot["single_game_top"]],
            [1000, 900, 800, 700],
        )
        self.assertEqual(snapshot["teams"][0]["players"][0]["top_scores"], [1000, 900, 800])

    def test_team_rating_helper_averages_only_complete_internal_values(self):
        players = [{"rating": 1900}, {"rating": 2000}]
        self.assertEqual(_team_rating_from_players(players), 1950)
        players[0]["rating"] = None
        self.assertIsNone(_team_rating_from_players(players))


if __name__ == "__main__":
    main()
