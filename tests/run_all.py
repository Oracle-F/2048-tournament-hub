from __future__ import annotations

import argparse
import sys
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from scripts.case_runner import SUITE_LABELS, run_suite
from scripts.api_snapshot_runner import run_suite as run_api_snapshot_suite
from scripts.replay_tournament import run_suite as run_real_tournament_suite
from scripts.simulation_runner import run_default_suite, run_fast_suite


SUITE_DISPLAY_NAMES = {
    "real_tournament": "Real Tournament Replays",
    "api_snapshot": "API Snapshots",
}


def main():
    parser = argparse.ArgumentParser(description="Run the full tournament test suite.")
    parser.add_argument("--fast", action="store_true", help="Skip the heaviest simulation scenario.")
    args = parser.parse_args()

    simulation_runner = run_fast_suite if args.fast else run_default_suite
    sections = [
        ("bug", lambda: run_suite("bug", emit_case_lines=True)),
        ("score", lambda: run_suite("score", emit_case_lines=True)),
        ("ranking", lambda: run_suite("ranking", emit_case_lines=True)),
        ("tournament", lambda: run_suite("tournament", emit_case_lines=True)),
        ("real_tournament", lambda: run_real_tournament_suite(emit_lines=True)),
        ("api_snapshot", lambda: run_api_snapshot_suite(emit_lines=True)),
        ("simulation", lambda: simulation_runner(emit_lines=True)),
    ]

    results = []
    print("====================")
    for key, runner in sections:
        label = SUITE_LABELS.get(key, SUITE_DISPLAY_NAMES.get(key, key.replace("_", " ").title()))
        if key == "simulation" and args.fast:
            label = "{} (fast)".format(label)
        print("")
        print(label)
        result = runner()
        results.append((label, result))
        status = "PASS" if not result["failed"] else "FAIL"
        print("{}/{} {}".format(result["passed"], result["total"], status))

    total_passed = sum(item[1]["passed"] for item in results)
    total = sum(item[1]["total"] for item in results)
    total_failed = total - total_passed

    print("")
    print("====================")
    print("TOTAL PASS" if total_failed == 0 else "TOTAL FAIL")
    raise SystemExit(0 if total_failed == 0 else 1)


if __name__ == "__main__":
    main()
