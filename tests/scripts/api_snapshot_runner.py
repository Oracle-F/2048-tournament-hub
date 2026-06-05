from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = TESTS_DIR.parent
SNAPSHOT_DIR = PROJECT_ROOT / "tests" / "api_snapshots"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.verse_adapter import build_leaderboard_url, build_user_url, fetch_json
from services.verse_query_service import _load_live_games, _load_live_leaderboard_rows, _load_live_rating_snapshot, _rawr_reply, _wr_reply


API_CASES = [
    {
        "variant": "4x4",
        "username": "snapshot_user",
        "display_name": "Snapshot Hero",
        "leaderboard_score": 54321,
        "rating": 1800.0,
        "rank": 12,
        "total_games": 3,
        "hs": 54321,
        "top_game_score": 54321,
    },
    {
        "variant": "3x3",
        "username": "snapshot_user_33",
        "display_name": "Snapshot 33",
        "leaderboard_score": 27654,
        "rating": 1666.5,
        "rank": 8,
        "total_games": 2,
        "hs": 27654,
        "top_game_score": 27654,
    },
    {
        "variant": "2x4",
        "username": "snapshot_user_24",
        "display_name": "Snapshot 24",
        "leaderboard_score": 33888,
        "rating": 1710.0,
        "rank": 5,
        "total_games": 2,
        "hs": 33888,
        "top_game_score": 33888,
    },
]


def _load_snapshot_file(name: str):
    path = SNAPSHOT_DIR / name
    if not path.exists():
        return None, "missing snapshot: {}".format(path)
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, "invalid json in {}: {}".format(path, exc)


def _append_check(diffs: list[str], condition: bool, message: str):
    if not condition:
        diffs.append(message)


def _check_leaderboard_snapshot(case: dict[str, object], diffs: list[str]):
    variant = str(case["variant"])
    payload = fetch_json(build_leaderboard_url(variant, "all", 1))
    _append_check(diffs, isinstance(payload, dict), "leaderboard snapshot did not load as object")
    rows = [] if not isinstance(payload, dict) else payload.get("leaderboard")
    _append_check(diffs, isinstance(rows, list) and len(rows) >= 1, "leaderboard snapshot missing rows")

    parsed_rows = _load_live_leaderboard_rows(variant, time_key="all", page=1)
    _append_check(diffs, isinstance(parsed_rows, list) and len(parsed_rows) >= 1, "leaderboard parser returned no rows")

    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        expected_username = rows[0].get("username")
        expected_score = rows[0].get("score")
        _append_check(
            diffs,
            isinstance(parsed_rows, list) and parsed_rows and parsed_rows[0].get("username") == expected_username,
            "leaderboard parser username mismatch",
        )
        _append_check(
            diffs,
            isinstance(parsed_rows, list) and parsed_rows and parsed_rows[0].get("score") == expected_score,
            "leaderboard parser score mismatch",
        )

        wr_text = _wr_reply(variant)
        rawr_text = _rawr_reply(variant)
        _append_check(diffs, isinstance(wr_text, str) and expected_username in wr_text, "WR reply missing expected username")
        _append_check(diffs, isinstance(rawr_text, str) and expected_username in rawr_text, "raWR reply missing expected username")


def _check_profile_snapshot(case: dict[str, object], diffs: list[str]):
    variant = str(case["variant"])
    username = str(case["username"])
    payload = fetch_json(build_user_url(username, variant, 1))
    _append_check(diffs, isinstance(payload, dict), "profile snapshot did not load as object")
    _append_check(diffs, isinstance(payload, dict) and isinstance(payload.get("games"), list), "profile snapshot missing games")

    rating_snapshot = _load_live_rating_snapshot(username, variant)
    _append_check(diffs, isinstance(rating_snapshot, dict), "rating snapshot parser returned nothing")
    if isinstance(payload, dict):
        _append_check(
            diffs,
            isinstance(rating_snapshot, dict) and rating_snapshot.get("total_games") == payload.get("totalGames"),
            "rating snapshot totalGames mismatch",
        )
        _append_check(
            diffs,
            isinstance(rating_snapshot, dict) and rating_snapshot.get("best_score") == payload.get("hs"),
            "rating snapshot hs mismatch",
        )

    games = _load_live_games(username, variant, profile_snapshot=rating_snapshot)
    _append_check(diffs, isinstance(games, list) and len(games) >= 1, "live games parser returned no games")

    if isinstance(payload, dict) and isinstance(payload.get("games"), list) and payload["games"]:
        expected_best = max(
            [item.get("score") for item in payload["games"] if isinstance(item, dict) and item.get("score") is not None] or [None]
        )
        actual_best = max([item.get("score") for item in games if isinstance(item, dict) and item.get("score") is not None] or [None])
        _append_check(diffs, expected_best == actual_best, "live games best score mismatch")
        _append_check(
            diffs,
            isinstance(games, list) and len(games) == len(payload["games"]),
            "live games count mismatch",
        )


def _check_contract_snapshot(diffs: list[str]):
    payload, error = _load_snapshot_file("score_import_response.json")
    if error:
        diffs.append(error)
        return
    required_fields = ["schema_version", "snapshot_type", "status", "summary", "contract"]
    for field in required_fields:
        _append_check(diffs, field in payload, "score import contract missing field: {}".format(field))
    if not diffs:
        _append_check(diffs, payload.get("snapshot_type") == "api_contract", "score import snapshot_type mismatch")
        _append_check(diffs, payload.get("status") == "ok", "score import status mismatch")
        summary = payload.get("summary") or {}
        _append_check(diffs, isinstance(summary, dict) and "imported_count" in summary, "score import summary missing imported_count")


def run_suite(*, emit_lines: bool = True):
    os.environ["VERSE_API_SNAPSHOT_DIR"] = str(SNAPSHOT_DIR)
    checks = []
    for case in API_CASES:
        checks.append(("leaderboard {}".format(case["variant"]), lambda diffs, case=case: _check_leaderboard_snapshot(case, diffs)))
        checks.append(("profile {}".format(case["variant"]), lambda diffs, case=case: _check_profile_snapshot(case, diffs)))
    checks.append(("contract", _check_contract_snapshot))
    results = []
    for name, checker in checks:
        diffs = []
        checker(diffs)
        ok = not diffs
        result = {"name": name, "ok": ok, "diffs": diffs}
        results.append(result)
        if emit_lines:
            print("[API] {}".format(name))
            print("PASS" if ok else "FAIL")
            for diff in diffs:
                print(diff)

    passed = sum(1 for item in results if item["ok"])
    total = len(results)
    return {
        "results": results,
        "passed": passed,
        "total": total,
        "failed": [item for item in results if not item["ok"]],
    }


def main():
    parser = argparse.ArgumentParser(description="Run API snapshot smoke tests.")
    parser.parse_args()

    os.environ["VERSE_API_SNAPSHOT_DIR"] = str(SNAPSHOT_DIR)
    result = run_suite(emit_lines=True)
    print("")
    print(
        "API Snapshot Summary: {} passed, {} failed, {} total".format(
            result["passed"],
            len(result["failed"]),
            result["total"],
        )
    )
    raise SystemExit(0 if not result["failed"] else 1)


if __name__ == "__main__":
    main()
