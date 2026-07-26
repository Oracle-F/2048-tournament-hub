from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import bot_private_service as bot  # noqa: E402
from services.match_rank_image_service import build_sample_snapshot, validate_snapshot  # noqa: E402
from services.stars_cup_bot_service import (  # noqa: E402
    LoadedStarsCupSnapshot,
    StarsCupQuery,
    StarsCupQueryError,
    StarsCupSnapshotUnavailable,
    build_stars_cup_query_reply,
    load_latest_stars_cup_snapshot,
    parse_stars_cup_query,
)
from settings import LOCAL_TIMEZONE  # noqa: E402
from tests.scripts.testing_support import fresh_test_connection  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _loaded_snapshot(*, stale=False) -> LoadedStarsCupSnapshot:
    now = datetime(2026, 7, 27, 12, 30, tzinfo=LOCAL_TIMEZONE)
    return LoadedStarsCupSnapshot(
        snapshot=validate_snapshot(build_sample_snapshot()),
        snapshot_path=Path("/exports/榜图数据.json"),
        snapshot_sha256="a" * 64,
        image_paths={
            "total": Path("/exports/总榜.png"),
            "detail": Path("/exports/六队明细.png"),
        },
        image_sha256={"total": "b" * 64, "detail": "c" * 64},
        run_id="20260727_120000",
        published_at=now - timedelta(hours=40 if stale else 1),
        age_seconds=144000 if stale else 3600,
        stale=stale,
    )


class StarsCupQueryParserTests(TestCase):
    def test_parser_accepts_normalized_and_slash_forms(self):
        self.assertEqual(parse_stars_cup_query("群星杯"), StarsCupQuery("overview"))
        self.assertEqual(parse_stars_cup_query("/群星杯 A"), StarsCupQuery("team", "A"))
        self.assertEqual(parse_stars_cup_query("／群星杯 v_01"), StarsCupQuery("player", "v_01"))
        self.assertEqual(parse_stars_cup_query("群星杯 我"), StarsCupQuery("self"))
        self.assertIsNone(parse_stars_cup_query("群星杯外传"))
        self.assertIsNone(parse_stars_cup_query("44ra"))

    def test_parser_rejects_multiple_selectors(self):
        with self.assertRaisesRegex(StarsCupQueryError, "用法"):
            parse_stars_cup_query("群星杯 A 多余")


class StarsCupLatestPointerTests(TestCase):
    def test_latest_pointer_verifies_paths_hashes_and_staleness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_root = root / "exports"
            run_dir = export_root / "20260727_120000"
            run_dir.mkdir(parents=True)
            snapshot_path = run_dir / "榜图数据.json"
            total_path = run_dir / "总榜.png"
            detail_path = run_dir / "六队明细.png"
            snapshot_path.write_text(
                json.dumps(build_sample_snapshot(), ensure_ascii=False),
                encoding="utf-8",
            )
            total_path.write_bytes(b"total-image")
            detail_path.write_bytes(b"detail-image")
            pointer_path = root / "latest.json"
            pointer_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "20260727_120000",
                        "published_at": "2026-07-27T12:00:00+08:00",
                        "snapshot": {
                            "path": "20260727_120000/榜图数据.json",
                            "sha256": _sha256(snapshot_path),
                        },
                        "images": {
                            "total": {
                                "path": "20260727_120000/总榜.png",
                                "sha256": _sha256(total_path),
                            },
                            "detail": {
                                "path": "20260727_120000/六队明细.png",
                                "sha256": _sha256(detail_path),
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            loaded = load_latest_stars_cup_snapshot(
                pointer_path,
                export_root=export_root,
                current_time=datetime(2026, 7, 29, 1, 0, tzinfo=LOCAL_TIMEZONE),
                stale_after_seconds=36 * 60 * 60,
            )

        self.assertEqual(loaded.run_id, "20260727_120000")
        self.assertEqual(loaded.snapshot["teams"][0]["code"], "A")
        self.assertTrue(loaded.stale)
        self.assertEqual(loaded.age_seconds, 37 * 60 * 60)

    def test_pointer_rejects_hash_mismatch_and_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_root = root / "exports"
            export_root.mkdir()
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            pointer = root / "latest.json"
            pointer.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "published_at": "2026-07-27T12:00:00+08:00",
                        "snapshot": {"path": str(outside), "sha256": _sha256(outside)},
                        "images": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StarsCupSnapshotUnavailable, "outside"):
                load_latest_stars_cup_snapshot(pointer, export_root=export_root)

            inside = export_root / "榜图数据.json"
            inside.write_text("{}", encoding="utf-8")
            pointer.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "published_at": "2026-07-27T12:00:00+08:00",
                        "snapshot": {"path": "榜图数据.json", "sha256": "0" * 64},
                        "images": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(StarsCupSnapshotUnavailable, "hash mismatch"):
                load_latest_stars_cup_snapshot(pointer, export_root=export_root)


class StarsCupReplyTests(TestCase):
    def test_overview_team_player_and_self_replies_use_snapshot_only(self):
        loaded = _loaded_snapshot()

        overview = build_stars_cup_query_reply(loaded, StarsCupQuery("overview"))
        team = build_stars_cup_query_reply(loaded, StarsCupQuery("team", "A"))
        player = build_stars_cup_query_reply(loaded, StarsCupQuery("player", "v_01"))
        self_reply = build_stars_cup_query_reply(
            loaded,
            StarsCupQuery("self"),
            bound_verse_account="v_01",
        )

        self.assertIn("群星杯队伍榜", overview)
        self.assertIn("1. F队 凌云", overview)
        self.assertIn("A队 星河", team)
        self.assertIn("第4名", team)
        self.assertIn("队内前3", team)
        self.assertIn("v_01｜A队｜第1档", player)
        self.assertEqual(player, self_reply)
        self.assertNotIn("rating", overview + team + player)

    def test_missing_binding_player_and_stale_data_have_explicit_messages(self):
        loaded = _loaded_snapshot(stale=True)

        unbound = build_stars_cup_query_reply(loaded, StarsCupQuery("self"))
        missing = build_stars_cup_query_reply(
            loaded,
            StarsCupQuery("player", "not-in-roster"),
        )
        overview = build_stars_cup_query_reply(loaded, StarsCupQuery("overview"))

        self.assertIn("还没有绑定", unbound)
        self.assertIn("未找到玩家", missing)
        self.assertIn("数据可能已过期", overview)

    def test_ambiguous_player_name_requires_an_exact_unique_roster_entry(self):
        loaded = _loaded_snapshot()
        loaded.snapshot["teams"][1]["players"][0]["verse"] = "v_01"

        with self.assertRaisesRegex(StarsCupQueryError, "多个同名玩家"):
            build_stars_cup_query_reply(
                loaded,
                StarsCupQuery("player", "v_01"),
            )

    def test_group_handler_routes_stars_cup_before_binding_gate(self):
        loaded = _loaded_snapshot()
        original_enabled = bot.GROUP_CHAT_ENABLED
        original_whitelist = set(bot.GROUP_CHAT_WHITELIST)
        try:
            bot.GROUP_CHAT_ENABLED = True
            bot.GROUP_CHAT_WHITELIST.clear()
            bot.GROUP_CHAT_WHITELIST.add("group-1")
            bot.GROUP_RATE_LIMIT_STATE.clear()
            bot.GROUP_GLOBAL_RATE_LIMIT_STATE.clear()
            with fresh_test_connection() as connection, patch(
                "services.bot_private_service.load_latest_stars_cup_snapshot",
                return_value=loaded,
            ):
                reply = bot.handle_group_message(
                    connection,
                    bot_platform="qq",
                    bot_user_id="unbound-user",
                    group_id="group-1",
                    text="群星杯 v_01",
                    is_at_bot=True,
                )
        finally:
            bot.GROUP_CHAT_ENABLED = original_enabled
            bot.GROUP_CHAT_WHITELIST.clear()
            bot.GROUP_CHAT_WHITELIST.update(original_whitelist)
            bot.GROUP_RATE_LIMIT_STATE.clear()
            bot.GROUP_GLOBAL_RATE_LIMIT_STATE.clear()

        self.assertIn("v_01｜A队", reply)
        self.assertNotIn("还没有绑定", reply)

    def test_roster_team_names_that_already_include_code_do_not_duplicate_it(self):
        loaded = _loaded_snapshot()
        for team in loaded.snapshot["teams"]:
            team["name"] = "{}队".format(team["code"])
        loaded.snapshot["event"]["as_of"] = "2026-07-27T05:14:30.767856+08:00"

        overview = build_stars_cup_query_reply(
            loaded,
            StarsCupQuery("overview"),
        )
        team = build_stars_cup_query_reply(
            loaded,
            StarsCupQuery("team", "A"),
        )

        self.assertIn("1. F队 ", overview)
        self.assertNotIn("FF队", overview)
        self.assertIn("A队｜", team)
        self.assertNotIn("A队 A队", team)
        self.assertIn("截至 2026-07-27 05:14", overview)

    def test_team_leaders_exclude_players_without_a_counted_score(self):
        loaded = _loaded_snapshot()
        team = loaded.snapshot["teams"][0]
        for player in team["players"][2:]:
            player["total_board_sum"] = None

        reply = build_stars_cup_query_reply(
            loaded,
            StarsCupQuery("team", "A"),
        )

        leaders = reply.splitlines()[1]
        self.assertIn(team["players"][0]["verse"], leaders)
        self.assertIn(team["players"][1]["verse"], leaders)
        self.assertNotIn("—", leaders)


if __name__ == "__main__":
    main()
