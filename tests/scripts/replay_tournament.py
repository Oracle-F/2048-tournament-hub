from __future__ import annotations

import argparse
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.testing_support import fresh_test_connection, set_test_database_environment

from scripts.real_tournament_support import REAL_TOURNAMENTS_DIR, load_real_tournament_snapshot, replay_real_tournament_snapshot


def _iter_snapshot_paths():
    if not REAL_TOURNAMENTS_DIR.exists():
        return []
    return sorted(REAL_TOURNAMENTS_DIR.glob("tournament_*.json"), key=lambda path: int(path.stem.split("_")[-1]))


def run_suite(*, emit_lines: bool = True):
    set_test_database_environment()
    snapshot_paths = _iter_snapshot_paths()
    results = []

    for path in snapshot_paths:
        snapshot = load_real_tournament_snapshot(path)
        with fresh_test_connection() as connection:
            result = replay_real_tournament_snapshot(connection, snapshot)

        item = {
            "name": path.name,
            "path": str(path),
            "ok": bool(result["ok"]),
            "diffs": list(result["diffs"]),
            "import_summary": result["import_summary"],
            "build_summary": result["build_summary"],
            "settle_summary": result["settle_summary"],
            "rating_summary": result["rating_summary"],
        }
        results.append(item)

        if emit_lines:
            print("[REAL] {}".format(path.name))
            if item["ok"]:
                print("PASS")
            else:
                print("FAIL")
                for diff in item["diffs"]:
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
    parser = argparse.ArgumentParser(description="Replay exported real tournament snapshots.")
    parser.parse_args()

    set_test_database_environment()
    result = run_suite(emit_lines=True)
    print("")
    print(
        "Real Tournament Summary: {} passed, {} failed, {} total".format(
            result["passed"],
            len(result["failed"]),
            result["total"],
        )
    )
    raise SystemExit(0 if not result["failed"] else 1)


if __name__ == "__main__":
    main()
