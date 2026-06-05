from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .testing_support import PROJECT_ROOT, fresh_test_connection, set_test_database_environment


set_test_database_environment()

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import transaction
from importers.raw_score_file_parser import normalize_row as normalize_score_row
from services import bot_private_service as bot_private_module
from services import verse_query_service as verse_query_module
from services.bot_private_service import (
    _group_is_allowed_command,
    _acquire_group_reply_slot,
    _consume_group_global_rate_limit,
    _group_should_consume_rate_limit,
    _group_should_redirect_reply,
    _load_my_score_event_rows,
    _parse_score_input,
    _message_handler_error_reply,
    patch_onebot_reply_lookup,
    render_send_timeout_fallback_message,
    is_transport_unstable_error,
    handle_group_message,
    handle_private_message,
)
from services.event_admin_service import infer_rating_bucket_code
from services.registration_service import list_player_registrations
from services.settlement_service import build_ranked_results, compute_primary_value, metric_from_record, settle_event
from services.verse_query_service import _full_board_score_window


CASE_ROOT = PROJECT_ROOT / "tests"
SUITE_PATTERNS = {
    "bug": ["cases/**/*.json"],
    "score": ["score_cases/*.json"],
    "ranking": ["ranking_cases/*.json"],
    "tournament": ["tournament_cases/*.json"],
    "bot": ["bot_cases/*.json"],
}
SUITE_ORDER = ["bug", "score", "ranking", "tournament", "bot"]
SUITE_LABELS = {
    "bug": "Bug Cases",
    "score": "Score Tests",
    "ranking": "Ranking Tests",
    "tournament": "Tournament Tests",
    "bot": "Bot Tests",
}


def load_case_files(suite: str | None = None):
    if suite in (None, "all"):
        for item in SUITE_ORDER:
            yield from load_case_files(item)
        return
    patterns = SUITE_PATTERNS.get(suite)
    if patterns is None:
        raise ValueError("Unknown suite: {}".format(suite))
    for pattern in patterns:
        yield from sorted(CASE_ROOT.glob(pattern))


def load_cases_from_file(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and "cases" in raw:
        return list(raw["cases"] or [])
    return [raw]


def ensure_case_shape(case: dict, source_path: Path, index: int):
    missing = [field for field in ("name", "description", "input", "expected") if field not in case]
    if missing:
        raise ValueError(
            "Missing fields {} in {} case #{}".format(
                ", ".join(missing),
                source_path,
                index + 1,
            )
        )


def run_seed_sql(connection, seed_sql: list[str] | None):
    if not seed_sql:
        return
    with transaction(connection):
        for statement in seed_sql:
            connection.execute(statement)


def run_target(connection, case_input: dict[str, Any]):
    target = str(case_input.get("target") or "").strip()
    args = case_input.get("args") or {}

    if target == "score.metric_from_record":
        return {
            "value": metric_from_record(
                args["competition_type"],
                args["record"],
                ranking_metric=args.get("ranking_metric"),
            )
        }
    if target == "score.compute_primary_value":
        return {
            "value": compute_primary_value(
                args.get("selected_records") or [],
                args["aggregation_method"],
                args["competition_type"],
                args.get("aggregation_count"),
                args.get("missing_attempt_policy"),
                ranking_metric=args.get("ranking_metric"),
                weight_base=float(args.get("weight_base", 0.6)),
            )
        }
    if target == "score.normalize_row":
        return normalize_score_row(
            args["row"],
            int(args.get("index") or 1),
            str(args.get("default_source_prefix") or "case"),
        )
    if target == "score.full_board_score_window_summary":
        windows = {}
        for variant_code, levels in (("2x4", [1, 2, 3, 4]), ("3x3", [1, 2, 3, 4, 5])):
            windows[variant_code] = {}
            for level in levels:
                window = _full_board_score_window(variant_code, level)
                windows[variant_code][str(level)] = list(window) if window is not None else None
        score = int(args.get("score") or 14360)
        mean = 14596 - (0.4 * 895)
        sigma = 1.2 * (895 ** 0.5)
        from statistics import NormalDist
        cdf = NormalDist(mu=mean, sigma=sigma).cdf(score)
        return {
            "windows": windows,
            "probability_33_tier3_score_14360_upper_tail_ppm": int(round((1.0 - cdf) * 1_000_000)),
            "contains_14360_in_33_tier3": windows["3x3"]["3"][0] <= score <= windows["3x3"]["3"][1],
        }
    if target == "ranking.build_ranked_results":
        bundle = build_ranked_results(
            connection,
            args["event_code"],
            include_registered_without_records=bool(args.get("include_registered_without_records", False)),
        )
        ranked = bundle["ranked_results"]
        return {
            "event_code": bundle["event"]["event_code"],
            "aggregation_method": bundle["aggregation_method"],
            "record_count": bundle["record_count"],
            "player_order": [row["display_name"] for row in ranked],
            "rank_values": [row["rank_value"] for row in ranked],
            "primary_metric_values": [row["primary_metric_value"] for row in ranked],
            "best_single_scores": [row["best_single_score"] for row in ranked],
        }
    if target == "db.connection_pragmas":
        return {
            "foreign_keys": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            "busy_timeout": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
        }
    if target == "db.transaction_rollback":
        marker = str(args.get("display_name") or args.get("marker") or "rollback_probe")
        before = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM players WHERE display_name = ?",
                (marker,),
            ).fetchone()["count"]
        )
        try:
            with transaction(connection):
                connection.execute(
                    """
                    INSERT INTO players (display_name, status, created_at, updated_at)
                    VALUES (?, 'active', ?, ?)
                    """,
                    (
                        marker,
                        str(args.get("created_at") or "2026-06-02 00:00:00"),
                        str(args.get("updated_at") or "2026-06-02 00:00:00"),
                    ),
                )
                raise RuntimeError("forced rollback for regression test")
        except RuntimeError:
            pass
        after = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM players WHERE display_name = ?",
                (marker,),
            ).fetchone()["count"]
        )
        return {
            "row_exists": after > 0,
            "count_delta": after - before,
        }
    if target == "mode.infer_rating_bucket_code":
        return {
            "value": infer_rating_bucket_code(
                args["variant_code"],
                args["competition_type"],
                args.get("target_value"),
            )
        }
    if target == "tournament.settle_event":
        summary = settle_event(connection, args["event_code"])
        event_row = connection.execute(
            "SELECT id FROM events WHERE event_code = ?",
            (args["event_code"],),
        ).fetchone()
        event_id = event_row["id"]
        result_rows = connection.execute(
            """
            SELECT
                er.rank_value,
                er.primary_metric_value,
                p.display_name
            FROM event_results er
            JOIN players p ON p.id = er.player_id
            WHERE er.event_id = ?
            ORDER BY er.rank_value ASC, p.display_name ASC
            """,
            (event_id,),
        ).fetchall()
        used_rows = connection.execute(
            """
            SELECT id
            FROM event_attempt_records
            WHERE event_id = ? AND evaluation_status = 'used'
            ORDER BY id ASC
            """,
            (event_id,),
        ).fetchall()
        pending_rows = connection.execute(
            """
            SELECT id
            FROM event_attempt_records
            WHERE event_id = ? AND evaluation_status = 'pending'
            ORDER BY id ASC
            """,
            (event_id,),
        ).fetchall()
        return {
            "event_code": summary["event_code"],
            "player_count": summary["player_count"],
            "record_count": summary["record_count"],
            "player_order": [row["display_name"] for row in result_rows],
            "rank_values": [row["rank_value"] for row in result_rows],
            "primary_metric_values": [row["primary_metric_value"] for row in result_rows],
            "used_record_ids": [row["id"] for row in used_rows],
            "pending_record_ids": [row["id"] for row in pending_rows],
        }
    if target == "bot.parse_score_input":
        return _parse_score_input(args["text"])
    if target == "bot.handle_private_message":
        force_admin = bool(args.get("force_admin", False))
        old_is_bot_admin = bot_private_module.is_bot_admin
        try:
            if force_admin:
                bot_private_module.is_bot_admin = lambda bot_platform, bot_user_id: True
            return {
                "reply": handle_private_message(
                    connection,
                    bot_platform=str(args.get("bot_platform") or "qq"),
                    bot_user_id=str(args["bot_user_id"]),
                    text=str(args["text"]),
                    message_segments=args.get("message_segments"),
                )
            }
        finally:
            bot_private_module.is_bot_admin = old_is_bot_admin
    if target == "bot.handle_group_message":
        group_id = str(args["group_id"])
        old_enabled = bot_private_module.GROUP_CHAT_ENABLED
        old_whitelist = set(bot_private_module.GROUP_CHAT_WHITELIST)
        old_rate_limit = bot_private_module.GROUP_CHAT_RATE_LIMIT_PER_MINUTE
        try:
            bot_private_module.GROUP_CHAT_ENABLED = bool(args.get("group_enabled", True))
            bot_private_module.GROUP_CHAT_WHITELIST = {group_id}
            bot_private_module.GROUP_CHAT_RATE_LIMIT_PER_MINUTE = int(args.get("rate_limit", 1000))
            return {
                "reply": handle_group_message(
                    connection,
                    bot_platform=str(args.get("bot_platform") or "qq"),
                    bot_user_id=str(args["bot_user_id"]),
                    group_id=group_id,
                    text=str(args["text"]),
                    message_segments=args.get("message_segments"),
                    is_at_bot=bool(args.get("is_at_bot", True)),
                )
            }
        finally:
            bot_private_module.GROUP_CHAT_ENABLED = old_enabled
            bot_private_module.GROUP_CHAT_WHITELIST = old_whitelist
            bot_private_module.GROUP_CHAT_RATE_LIMIT_PER_MINUTE = old_rate_limit
    if target == "bot.group_is_allowed_command":
        return {
            "value": _group_is_allowed_command(
                args["message"],
                has_flow=bool(args.get("has_flow", False)),
            )
        }
    if target == "bot.group_should_redirect_reply":
        return {
            "value": _group_should_redirect_reply(
                args["reply_text"],
            )
        }
    if target == "bot.group_should_consume_rate_limit":
        return {
            "value": _group_should_consume_rate_limit(
                args["message"],
                has_flow=bool(args.get("has_flow", False)),
            )
        }
    if target == "bot.group_global_rate_limit_sequence":
        group_id = str(args["group_id"])
        old_global_limit = bot_private_module.GROUP_CHAT_GLOBAL_RATE_LIMIT_PER_MINUTE
        old_state = dict(bot_private_module.GROUP_GLOBAL_RATE_LIMIT_STATE)
        try:
            bot_private_module.GROUP_CHAT_GLOBAL_RATE_LIMIT_PER_MINUTE = int(args.get("rate_limit", 2))
            bot_private_module.GROUP_GLOBAL_RATE_LIMIT_STATE[group_id] = []
            values = []
            for user_id in args.get("user_ids", []):
                values.append(_consume_group_global_rate_limit(group_id))
            return {"values": values}
        finally:
            bot_private_module.GROUP_CHAT_GLOBAL_RATE_LIMIT_PER_MINUTE = old_global_limit
            bot_private_module.GROUP_GLOBAL_RATE_LIMIT_STATE.clear()
            bot_private_module.GROUP_GLOBAL_RATE_LIMIT_STATE.update(old_state)
    if target == "bot.group_reply_slot_sequence":
        group_id = str(args["group_id"])
        old_limit = bot_private_module.GROUP_CHAT_MAX_CONCURRENT_REPLY_JOBS_PER_GROUP
        old_state = dict(bot_private_module.GROUP_REPLY_JOB_STATE)
        try:
            bot_private_module.GROUP_CHAT_MAX_CONCURRENT_REPLY_JOBS_PER_GROUP = int(args.get("limit", 2))
            bot_private_module.GROUP_REPLY_JOB_STATE.clear()
            values = []
            for _user_id in args.get("user_ids", []):
                values.append(_acquire_group_reply_slot(group_id))
            return {"values": values}
        finally:
            bot_private_module.GROUP_CHAT_MAX_CONCURRENT_REPLY_JOBS_PER_GROUP = old_limit
            bot_private_module.GROUP_REPLY_JOB_STATE.clear()
            bot_private_module.GROUP_REPLY_JOB_STATE.update(old_state)
    if target == "bot.message_handler_error_reply":
        return {
            "reply": _message_handler_error_reply(
                RuntimeError(str(args.get("message") or "boom")),
                is_group=bool(args.get("is_group", False)),
            )
        }
    if target == "bot.patch_onebot_reply_lookup":
        class DummyBotModule:
            pass

        dummy = DummyBotModule()
        dummy._check_reply = lambda *args, **kwargs: "original"
        patched = patch_onebot_reply_lookup(dummy)
        return {
            "patched": patched,
            "check_reply_name": getattr(dummy._check_reply, "__name__", ""),
            "check_reply_result": dummy._check_reply(),
        }
    if target == "bot.render_send_timeout_fallback_message":
        return {
            "reply": render_send_timeout_fallback_message(str(args.get("reply") or ""))
        }
    if target == "bot.is_transport_unstable_error":
        return {
            "value": is_transport_unstable_error(RuntimeError(str(args.get("message") or "")))
        }
    if target == "bot.verse_shared_reply_cache_sequence":
        old_reply_cache = dict(verse_query_module.VERSE_QUERY_REPLY_CACHE)
        old_inflight = dict(verse_query_module.VERSE_QUERY_INFLIGHT)
        old_live_leaderboard_rows = verse_query_module._load_live_leaderboard_rows
        try:
            verse_query_module.VERSE_QUERY_REPLY_CACHE.clear()
            verse_query_module.VERSE_QUERY_INFLIGHT.clear()
            verse_query_module._load_live_leaderboard_rows = lambda variant_code, time_key="all", page=1: [
                {"username": "alpha_user", "score": 1234, "rating_value": 567.8}
            ]
            first_reply = handle_private_message(
                connection,
                bot_platform=str(args.get("bot_platform") or "qq"),
                bot_user_id=str(args.get("first_bot_user_id") or "20001"),
                text=str(args.get("text") or "4x4 wr"),
            )
            cache_size_after_first = len(verse_query_module.VERSE_QUERY_REPLY_CACHE)
            second_reply = handle_private_message(
                connection,
                bot_platform=str(args.get("bot_platform") or "qq"),
                bot_user_id=str(args.get("second_bot_user_id") or "20002"),
                text=str(args.get("text") or "4x4 wr"),
            )
            cache_size_after_second = len(verse_query_module.VERSE_QUERY_REPLY_CACHE)
            return {
                "first_reply": first_reply,
                "second_reply": second_reply,
                "cache_size_after_first": cache_size_after_first,
                "cache_size_after_second": cache_size_after_second,
            }
        finally:
            verse_query_module._load_live_leaderboard_rows = old_live_leaderboard_rows
            verse_query_module.VERSE_QUERY_REPLY_CACHE.clear()
            verse_query_module.VERSE_QUERY_REPLY_CACHE.update(old_reply_cache)
            verse_query_module.VERSE_QUERY_INFLIGHT.clear()
            verse_query_module.VERSE_QUERY_INFLIGHT.update(old_inflight)
    if target == "bot.verse_rawr_first_row_fast_path":
        old_live_leaderboard_rows = verse_query_module._load_live_leaderboard_rows
        old_live_rating_snapshot = verse_query_module._load_live_rating_snapshot
        try:
            verse_query_module._load_live_leaderboard_rows = lambda variant_code, time_key="all", page=1: [
                {"username": "alpha_user", "score": 1234, "rating": 987.6},
                {"username": "beta_user", "score": 2222, "rating": 999.9},
            ]
            calls = {"count": 0}

            def _track_rating_snapshot(username, variant_code):
                calls["count"] += 1
                return {"rating_value": 1000.0}

            verse_query_module._load_live_rating_snapshot = _track_rating_snapshot
            reply = verse_query_module._rawr_reply(str(args.get("variant_code") or "4x4"))
            return {
                "reply": reply,
                "snapshot_calls": calls["count"],
            }
        finally:
            verse_query_module._load_live_leaderboard_rows = old_live_leaderboard_rows
            verse_query_module._load_live_rating_snapshot = old_live_rating_snapshot
    if target == "bot.verse_full_board_no_local_fallback":
        old_score_focused_games = verse_query_module._load_score_focused_games
        old_local_games = verse_query_module._load_local_games
        try:
            with transaction(connection):
                connection.execute(
                    "INSERT INTO players (id, display_name, status, created_at, updated_at) VALUES (9103, 'FullBoardUser', 'active', '2026-06-04 09:00:00', '2026-06-04 09:00:00');"
                )
                connection.execute(
                    "INSERT INTO player_accounts (id, player_id, platform_id, account_key, account_name, account_display_name, is_primary, metadata_json, created_at, updated_at) VALUES (9914, 9103, (SELECT id FROM platforms WHERE code = '2048verse'), 'full_board_user', 'full_board_user', 'full_board_user', 1, '{}', '2026-06-04 09:00:00', '2026-06-04 09:00:00');"
                )
                connection.execute(
                    "INSERT INTO bot_account_bindings (id, bot_platform, bot_user_id, game_platform, player_id, account_key, display_name, is_active, metadata_json, created_at, updated_at) VALUES (9913, 'qq', '20001', '2048verse', 9103, 'full_board_user', 'full_board_user', 1, '{}', '2026-06-04 09:00:00', '2026-06-04 09:00:00');"
                )
            verse_query_module._load_score_focused_games = lambda *args, **kwargs: None
            local_calls = {"count": 0}

            def _track_local_games(connection, player_id, variant_code):
                local_calls["count"] += 1
                return [{"id": 1, "score": 9999, "ended_at": None, "board_values": [1024], "board_sum": 1024, "max_tile": 1024}]

            verse_query_module._load_local_games = _track_local_games
            reply = verse_query_module.handle_verse_query_message(
                connection,
                bot_platform=str(args.get("bot_platform") or "qq"),
                bot_user_id=str(args.get("bot_user_id") or "20001"),
                text=str(args.get("text") or "24满盘"),
            )
            return {
                "reply": reply,
                "local_calls": local_calls["count"],
            }
        finally:
            verse_query_module._load_score_focused_games = old_score_focused_games
            verse_query_module._load_local_games = old_local_games
    if target == "bot.load_my_score_event_rows":
        binding = args["binding"]
        rows = _load_my_score_event_rows(
            connection,
            binding,
            archived_only=bool(args.get("archived_only", False)),
            month_key=args.get("month_key"),
        )
        return {
            "count": len(rows),
            "event_codes": [row["event_code"] for row in rows],
            "statuses": [row["status"] for row in rows],
            "registration_statuses": [row.get("registration_status") for row in rows],
        }
    if target == "registration.list_player_registrations":
        rows = list_player_registrations(
            connection,
            args["player_id"],
            platform_code=args.get("platform_code"),
            active_only=bool(args.get("active_only", True)),
        )
        return {
            "count": len(rows),
            "event_codes": [row["event_code"] for row in rows],
            "statuses": [row["status"] for row in rows],
        }

    raise ValueError("Unknown target: {}".format(target))


def run_case(connection, case: dict[str, Any], source_path: Path):
    seed_sql = (case.get("input") or {}).get("seed_sql")
    run_seed_sql(connection, seed_sql)
    actual = run_target(connection, case["input"])
    expected = case["expected"]
    return actual == expected, actual, expected


def discover_cases(suite: str = "all", pattern: str | None = None):
    for path in load_case_files(suite):
        if pattern and pattern.lower() not in str(path).lower():
            continue
        for index, case in enumerate(load_cases_from_file(path)):
            ensure_case_shape(case, path, index)
            yield path, index, case


def run_suite(suite: str = "all", pattern: str | None = None, *, emit_case_lines: bool = True):
    total = 0
    passed = 0
    failed = []

    for source_path, index, case in discover_cases(suite, pattern):
        total += 1
        case_name = case["name"]
        with fresh_test_connection() as connection:
            ok, actual, expected = run_case(connection, case, source_path)
        rel_path = source_path.relative_to(PROJECT_ROOT)
        label = "{} :: {}".format(rel_path, case_name)
        if ok:
            passed += 1
            if emit_case_lines:
                print("[PASS] {}".format(label))
        else:
            failed.append((label, actual, expected))
            if emit_case_lines:
                print("[FAIL] {}".format(label))
                print("  actual  : {}".format(json.dumps(actual, ensure_ascii=False, sort_keys=True, indent=2)))
                print("  expected: {}".format(json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2)))

    return {
        "suite": suite,
        "total": total,
        "passed": passed,
        "failed": failed,
    }


def run_all_suites(pattern: str | None = None, *, emit_case_lines: bool = True):
    suite_results = []
    total = 0
    passed = 0
    failed = []
    for suite in SUITE_ORDER:
        result = run_suite(suite, pattern, emit_case_lines=emit_case_lines)
        suite_results.append(result)
        total += result["total"]
        passed += result["passed"]
        failed.extend(result["failed"])
    return {
        "suites": suite_results,
        "total": total,
        "passed": passed,
        "failed": failed,
    }


def main():
    parser = argparse.ArgumentParser(description="Run lightweight JSON test cases.")
    parser.add_argument("--suite", choices=["all", *SUITE_ORDER], default="all")
    parser.add_argument("--pattern", help="Only run cases whose file path contains this text.")
    args = parser.parse_args()

    if args.suite == "all":
        result = run_all_suites(pattern=args.pattern, emit_case_lines=True)
    else:
        result = run_suite(args.suite, pattern=args.pattern, emit_case_lines=True)

    print("")
    print("Summary: {} passed, {} failed, {} total".format(result["passed"], len(result["failed"]), result["total"]))
    raise SystemExit(0 if not result["failed"] else 1)


if __name__ == "__main__":
    main()
