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

from scripts.real_tournament_support import (
    REAL_TOURNAMENTS_DIR,
    export_real_tournament_snapshot,
    open_readonly_connection,
    write_real_tournament_snapshot,
)
from settings import PRODUCTION_DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Export one real tournament snapshot.")
    parser.add_argument("event_id", type=int, help="Source event ID")
    parser.add_argument(
        "--database",
        type=Path,
        default=PRODUCTION_DATABASE_PATH,
        help="Read-only source database path.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REAL_TOURNAMENTS_DIR,
        help="Where to write the snapshot JSON.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.database.exists():
        print("Database file not found: {}".format(args.database))
        raise SystemExit(1)

    connection = open_readonly_connection(args.database)
    try:
        snapshot = export_real_tournament_snapshot(connection, args.event_id)
        output_path = args.output_dir / "tournament_{}.json".format(args.event_id)
        write_real_tournament_snapshot(snapshot, output_path)
        print("Exported tournament snapshot -> {}".format(output_path))
        print("Event: {} ({})".format(snapshot["event"]["event_code"], snapshot["event"]["event_name"]))
        print(
            "Raw scores: {} | Ranking rows: {} | Rating rows: {}".format(
                snapshot["summary"]["raw_score_count"],
                snapshot["summary"]["ranking_count"],
                snapshot["summary"]["rating_count"],
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
