from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase, main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from services import verse_query_service as verse  # noqa: E402


class VerseQueryServicePerformanceTests(TestCase):
    def test_24_full_board_uses_score_window(self):
        spec = verse._score_focused_spec("24满盘")

        self.assertEqual(2882, spec["score_floor"])
        self.assertEqual(3066, spec["score_ceiling"])
        self.assertNotIn("full_scan", spec)

    def test_24_second_tier_full_board_uses_probability_window(self):
        spec = verse._score_focused_spec("24满盘2")

        self.assertEqual(5126, spec["score_floor"])
        self.assertEqual(5327, spec["score_ceiling"])
        self.assertNotIn("full_scan", spec)

    def test_full_board_probability_windows_follow_normal_model(self):
        expectations = {
            ("2x4", 1): (2882, 3066),
            ("2x4", 2): (5126, 5327),
            ("2x4", 3): (6130, 6320),
            ("2x4", 4): (6579, 6742),
            ("3x3", 1): (6838, 7097),
            ("3x3", 2): (11843, 12127),
            ("3x3", 3): (14104, 14372),
            ("3x3", 4): (15121, 15352),
            ("3x3", 5): (15583, 15760),
        }

        for (variant_code, level), expected_window in expectations.items():
            with self.subTest(variant_code=variant_code, level=level):
                self.assertEqual(expected_window, verse._full_board_score_window(variant_code, level))

    def test_32k_ratio_metrics_use_single_pass_counts(self):
        games = [
            {"board_values": [32768], "max_tile": 32768},
            {"board_values": [32768, 16384], "max_tile": 32768},
            {"board_values": [32768, 16384, 8192, 4096], "max_tile": 32768},
            {"board_values": [16384, 8192], "max_tile": 16384},
        ]

        metrics = verse._calc_32k_ratio_metrics(games)

        self.assertEqual(3, metrics["count_32k_plus"])
        self.assertEqual(2, metrics["numerator"])
        self.assertEqual(3, metrics["denominator"])
        self.assertAlmostEqual(2 / 3, metrics["ratio"])
        self.assertFalse(metrics["eligible"])

    def test_four_x_four_score_floors_follow_user_thresholds(self):
        expectations = {
            "8ks": (8192, 87000),
            "16ks": (16384, 195000),
            "32ks": (32768, 430000),
            "65ks": (65536, 831000),
            "8/16": (16384, 195000),
            "16/32": (32768, 650000),
            "24/32": (32768, 745000),
            "28/32": (32768, 797000),
            "30/32": (32768, 821000),
            "31/32": (32768, 831000),
            "f512": (32768, 831000),
            "f256": (32768, 831000),
            "f128": (32768, 831000),
        }

        for token, (expected_min_tile, expected_floor) in expectations.items():
            with self.subTest(token=token):
                spec = verse._score_focused_spec(token)
                self.assertEqual(expected_floor, spec["score_floor"])
                self.assertEqual(expected_min_tile, spec["min_required_tile"])
                self.assertFalse(spec["allow_stale_cache"])

    def test_24_over_32_score_query_disables_stale_cache(self):
        spec = verse._score_focused_spec("24/32")

        self.assertEqual(745000, spec["score_floor"])
        self.assertEqual(32768, spec["min_required_tile"])
        self.assertFalse(spec["allow_stale_cache"])

    def test_three_x_four_score_floors_follow_user_thresholds(self):
        expectations = {
            "4ks": (4096, 37000),
            "2/4": (4096, 61000),
            "3/4": (4096, 71500),
        }

        for token, (expected_min_tile, expected_floor) in expectations.items():
            with self.subTest(token=token):
                spec = verse._score_focused_spec(token)
                self.assertEqual(expected_floor, spec["score_floor"])
                self.assertEqual(expected_min_tile, spec["min_required_tile"])
                self.assertFalse(spec["allow_stale_cache"])

    def test_two_x_four_and_three_x_three_score_floors_follow_user_thresholds(self):
        expectations = {
            "512s": (512, 3200),
            "768s": (512, 5300),
            "1024s": (1024, 7500),
            "1536s": (1024, 12200),
        }

        for token, (expected_min_tile, expected_floor) in expectations.items():
            with self.subTest(token=token):
                spec = verse._score_focused_spec(token)
                self.assertEqual(expected_floor, spec["score_floor"])
                self.assertEqual(expected_min_tile, spec["min_required_tile"])
                self.assertFalse(spec["allow_stale_cache"])

    def test_recent_pb20_fast_path_uses_prepared_top_five(self):
        games = [
            {"score": 1000, "ended_at": "2026-06-03T10:05:00+08:00", "id": "a"},
            {"score": 950, "ended_at": "2026-06-03T10:04:00+08:00", "id": "b"},
            {"score": 940, "ended_at": "2026-06-03T10:03:00+08:00", "id": "c"},
            {"score": 930, "ended_at": "2026-06-03T10:02:00+08:00", "id": "d"},
            {"score": 920, "ended_at": "2026-06-03T10:01:00+08:00", "id": "e"},
        ]

        payload = verse._avg_recent_pb20_games(games, 1000)

        self.assertEqual(5, payload["count"])
        self.assertEqual(200.0, payload["threshold"])
        self.assertEqual(948.0, payload["avg_score"])

    def test_month_games_stops_scanning_page_after_boundary(self):
        original_fetch_user_page = verse._fetch_user_page
        original_month_range = verse._month_range_of_previous_month
        original_normalize = verse._normalize_live_game
        original_cache = dict(verse.VERSE_QUERY_HEAVY_QUERY_CACHE)
        normalize_calls = 0
        try:
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()
            verse._month_range_of_previous_month = lambda now: (
                verse.datetime(2026, 5, 1, tzinfo=verse.LOCAL_TIMEZONE),
                verse.datetime(2026, 6, 1, tzinfo=verse.LOCAL_TIMEZONE),
            )

            def fake_fetch_user_page(username, variant_code, page, *, sort="date", desc=True):
                self.assertEqual(sort, "date")
                self.assertTrue(desc)
                if page in {1, 2}:
                    return {
                        "totalGames": 150,
                        "games": [
                            {
                                "id": f"month-{page}-{index}",
                                "score": 1000 - index,
                                "played_at": "2026-05-20T10:00:00+08:00",
                                "board": [2, 2, 2, 2],
                            }
                            for index in range(50)
                        ],
                    }
                if page == 3:
                    return {
                        "totalGames": 150,
                        "games": [
                            {
                                "id": "boundary-old",
                                "score": 900,
                                "played_at": "2026-04-30T23:59:59+08:00",
                                "board": [2, 2, 2, 2],
                            }
                        ]
                        + [
                            {
                                "id": f"old-{index}",
                                "score": 899 - index,
                                "played_at": "2026-04-30T23:59:{:02d}+08:00".format(index % 60),
                                "board": [2, 2, 2, 2],
                            }
                            for index in range(49)
                        ],
                    }
                self.fail("month scan should stop before page 4")

            def counting_normalize(game_payload, fallback_id):
                nonlocal normalize_calls
                normalize_calls += 1
                if normalize_calls > 101:
                    self.fail("month scan should stop after first old record on page 3")
                return original_normalize(game_payload, fallback_id)

            verse._fetch_user_page = fake_fetch_user_page
            verse._normalize_live_game = counting_normalize
            games = verse._load_month_games("tester", "4x4")
        finally:
            verse._fetch_user_page = original_fetch_user_page
            verse._month_range_of_previous_month = original_month_range
            verse._normalize_live_game = original_normalize
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.update(original_cache)

        self.assertEqual(100, len(games))
        self.assertEqual(101, normalize_calls)

    def test_32k_ratio_uses_score_floor_and_keeps_scanning_past_high_no_32k_pages(self):
        spec = verse._score_focused_spec("32k综率")

        self.assertEqual(430000, spec["score_floor"])
        self.assertEqual(32768, spec["min_required_tile"])
        self.assertIs(verse._is_32k_plus_game, spec["predicate"])
        self.assertFalse(spec["allow_stale_cache"])

        original_fetch_user_page = verse._fetch_user_page
        original_cache = dict(verse.VERSE_QUERY_HEAVY_QUERY_CACHE)
        calls = []
        try:
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()

            def fake_fetch_user_page(username, variant_code, page, *, sort="date", desc=True):
                calls.append(page)
                self.assertEqual(sort, "score")
                self.assertTrue(desc)
                if page == 1:
                    return {
                        "totalGames": 150,
                        "games": [
                            {
                                "id": f"page-1-{index}",
                                "score": 440000 - index,
                                "ended_at": "2026-06-03T10:00:00+08:00",
                                "board": [16384, 8192, 4096, 2048, 1024],
                            }
                            for index in range(50)
                        ],
                    }
                if page == 2:
                    return {
                        "totalGames": 150,
                        "games": [
                            {
                                "id": "page-2-hit",
                                "score": 439500,
                                "ended_at": "2026-06-03T10:01:00+08:00",
                                "board": [32768, 16384, 8192, 4096, 2048, 1024],
                            }
                        ]
                        + [
                            {
                                "id": f"page-2-{index}",
                                "score": 439499 - index,
                                "ended_at": "2026-06-03T10:01:01+08:00",
                                "board": [16384, 8192, 4096, 2048, 1024],
                            }
                            for index in range(49)
                        ],
                    }
                if page == 3:
                    return {
                        "totalGames": 150,
                        "games": [
                            {
                                "id": "page-3-low",
                                "score": 429999,
                                "ended_at": "2026-06-03T10:02:00+08:00",
                                "board": [32768, 16384, 8192, 4096, 2048, 1024],
                            }
                        ],
                    }
                self.fail("score floor should stop before page 4")

            verse._fetch_user_page = fake_fetch_user_page
            games = verse._load_score_focused_games(
                "tester",
                "4x4",
                predicate=verse._is_32k_plus_game,
                min_required_tile=32768,
                cache_key_hint="32k综率",
                score_floor=spec["score_floor"],
            )
        finally:
            verse._fetch_user_page = original_fetch_user_page
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.update(original_cache)

        self.assertEqual(["page-2-hit"], [game["id"] for game in games])
        self.assertEqual([1, 2, 3], calls)

    def test_high_risk_score_focused_queries_skip_stale_cache_when_partial(self):
        original_fetch_user_page = verse._fetch_user_page
        original_stale = verse._heavy_query_cache_get_stale
        original_cache = dict(verse.VERSE_QUERY_HEAVY_QUERY_CACHE)
        try:
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()

            def fake_fetch_user_page(username, variant_code, page, *, sort="date", desc=True):
                self.assertEqual(sort, "score")
                self.assertTrue(desc)
                if page == 1:
                    return {
                        "totalGames": 100,
                        "games": [
                            {
                                "id": f"page-1-{index}",
                                "score": 900000 - index,
                                "ended_at": "2026-06-03T10:00:00+08:00",
                                "board": [32768, 16384, 8192],
                            }
                            for index in range(50)
                        ],
                    }
                if page == 2:
                    return None
                self.fail("partial strict scan should not need page 3")

            verse._fetch_user_page = fake_fetch_user_page
            verse._heavy_query_cache_get_stale = lambda cache_key: self.fail("stale cache should not be used for strict score queries")

            result = verse._load_score_focused_games(
                "tester",
                "4x4",
                predicate=verse._is_32k_plus_game,
                min_required_tile=32768,
                cache_key_hint="32k综率",
                score_floor=430000,
                full_scan=True,
                allow_stale_cache=False,
            )
        finally:
            verse._fetch_user_page = original_fetch_user_page
            verse._heavy_query_cache_get_stale = original_stale
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.update(original_cache)

        self.assertIsNone(result)

    def test_wr_uses_leaderboard_rating_without_profile_lookup(self):
        original_load_rows = verse._load_live_leaderboard_rows
        original_load_profile = verse._load_live_rating_snapshot
        try:
            verse._load_live_leaderboard_rows = lambda *args, **kwargs: [
                {"rank": 1, "username": "winner", "score": 54321, "rating": 1800.0},
            ]
            verse._load_live_rating_snapshot = lambda *args, **kwargs: self.fail("WR should not call profile lookup when leaderboard already has rating")
            reply = verse._wr_reply("4x4")
        finally:
            verse._load_live_leaderboard_rows = original_load_rows
            verse._load_live_rating_snapshot = original_load_profile

        self.assertIn("4x4 WR", reply)
        self.assertIn("1800.0", reply)
        self.assertIn("winner", reply)

    def test_rawr_uses_leaderboard_rating_without_profile_lookup(self):
        original_load_rows = verse._load_live_leaderboard_rows
        original_load_profile = verse._load_live_rating_snapshot
        try:
            verse._load_live_leaderboard_rows = lambda *args, **kwargs: [
                {"rank": 1, "username": "alpha", "score": 40000, "rating": 1770.0},
                {"rank": 2, "username": "beta", "score": 50000, "rating": 1799.0},
                {"rank": 3, "username": "gamma", "score": 30000, "rating": 1765.0},
            ]
            verse._load_live_rating_snapshot = lambda *args, **kwargs: self.fail("raWR should not call profile lookup when leaderboard already has rating")
            reply = verse._rawr_reply("4x4")
        finally:
            verse._load_live_leaderboard_rows = original_load_rows
            verse._load_live_rating_snapshot = original_load_profile

        self.assertIn("4x4 raWR", reply)
        self.assertIn("beta", reply)
        self.assertIn("1799.0", reply)

    def test_score_focused_full_scan_keeps_later_matching_pages(self):
        original_fetch_user_page = verse._fetch_user_page
        original_cache = dict(verse.VERSE_QUERY_HEAVY_QUERY_CACHE)
        try:
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()

            def fake_fetch_user_page(username, variant_code, page, *, sort="date", desc=True):
                self.assertEqual(sort, "score")
                self.assertTrue(desc)
                if page == 1:
                    no_hit_games = [
                        {
                            "id": f"page-1-{index}",
                            "score": 900 - index,
                            "ended_at": "2026-06-03T10:00:00+08:00",
                            "board": [1, 1, 1],
                        }
                        for index in range(50)
                    ]
                    return {
                        "totalGames": 100,
                        "games": no_hit_games,
                    }
                if page == 2:
                    return {
                        "totalGames": 100,
                        "games": [
                            {
                                "id": "page-2",
                                "score": 800,
                                "ended_at": "2026-06-03T10:01:00+08:00",
                                "board": [256, 128, 64, 32, 16, 8, 4, 2],
                            }
                        ],
                    }
                self.fail("score floor should stop before page 3")

            verse._fetch_user_page = fake_fetch_user_page
            games = verse._load_score_focused_games(
                "tester",
                "2x4",
                predicate=lambda game: verse._count_full_board_level(game, "2x4") >= 1,
                min_required_tile=512,
                cache_key_hint="24满盘",
                full_scan=True,
            )
        finally:
            verse._fetch_user_page = original_fetch_user_page
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.update(original_cache)

        self.assertEqual([game["id"] for game in games], ["page-2"])
        self.assertEqual(1, len(games))

    def test_score_window_stops_after_floor_page(self):
        original_fetch_user_page = verse._fetch_user_page
        original_cache = dict(verse.VERSE_QUERY_HEAVY_QUERY_CACHE)
        score_floor, score_ceiling = verse._full_board_score_window("2x4", 1)
        calls = []
        try:
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()

            def fake_fetch_user_page(username, variant_code, page, *, sort="date", desc=True):
                calls.append(page)
                self.assertEqual(sort, "score")
                self.assertTrue(desc)
                if page == 1:
                    return {
                        "totalGames": 100,
                        "games": [
                            {
                                "id": f"page-1-{index}",
                                "score": 3000 - index,
                                "ended_at": "2026-06-03T10:00:00+08:00",
                                "board": [1, 1, 1],
                            }
                            for index in range(50)
                        ],
                    }
                if page == 2:
                    return {
                        "totalGames": 100,
                        "games": [
                            {
                                "id": "page-2-hit",
                                "score": 2882,
                                "ended_at": "2026-06-03T10:01:00+08:00",
                                "board": [256, 128, 64, 32, 16, 8, 4, 2],
                            }
                        ],
                    }
                return {"totalGames": 100, "games": []}

            verse._fetch_user_page = fake_fetch_user_page
            games = verse._load_score_focused_games(
                "tester",
                "2x4",
                predicate=lambda game: verse._count_full_board_level(game, "2x4") >= 1,
                min_required_tile=512,
                cache_key_hint="24满盘",
                score_floor=score_floor,
                score_ceiling=score_ceiling,
            )
        finally:
            verse._fetch_user_page = original_fetch_user_page
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.update(original_cache)

        self.assertEqual(["page-2-hit"], [game["id"] for game in games])
        self.assertEqual([1, 2], calls)

    def test_score_window_skips_pages_above_ceiling(self):
        original_fetch_user_page = verse._fetch_user_page
        original_cache = dict(verse.VERSE_QUERY_HEAVY_QUERY_CACHE)
        score_floor, score_ceiling = verse._full_board_score_window("2x4", 1)
        try:
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()

            def fake_fetch_user_page(username, variant_code, page, *, sort="date", desc=True):
                self.assertEqual(sort, "score")
                self.assertTrue(desc)
                if page == 1:
                    return {
                        "totalGames": 100,
                        "games": [
                            {
                                "id": f"page-1-{index}",
                                "score": 3200 - index,
                                "ended_at": "2026-06-03T10:00:00+08:00",
                                "board": [1, 1, 1],
                            }
                            for index in range(50)
                        ],
                    }
                if page == 2:
                    return {
                        "totalGames": 100,
                        "games": [
                            {
                                "id": "page-2-hit",
                                "score": 2990,
                                "ended_at": "2026-06-03T10:01:00+08:00",
                                "board": [256, 128, 64, 32, 16, 8, 4, 2],
                            }
                        ],
                    }
                if page == 3:
                    return {
                        "totalGames": 100,
                        "games": [
                            {
                                "id": "page-3",
                                "score": 2960,
                                "ended_at": "2026-06-03T10:02:00+08:00",
                                "board": [1, 1, 1],
                            }
                        ],
                    }
                self.fail("score window should stop before page 4")

            verse._fetch_user_page = fake_fetch_user_page
            games = verse._load_score_focused_games(
                "tester",
                "2x4",
                predicate=lambda game: verse._count_full_board_level(game, "2x4") >= 1,
                min_required_tile=512,
                cache_key_hint="24满盘",
                score_floor=score_floor,
                score_ceiling=score_ceiling,
            )
        finally:
            verse._fetch_user_page = original_fetch_user_page
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.clear()
            verse.VERSE_QUERY_HEAVY_QUERY_CACHE.update(original_cache)

        self.assertEqual([game["id"] for game in games], ["page-2-hit"])


if __name__ == "__main__":
    main()
