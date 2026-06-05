import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from services.rating_service import recalculate_event_bucket_ratings, recalculate_ratings
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Recalculate player ratings from settled rated events")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bucket", help="Only recalculate one rating bucket code")
    group.add_argument("--event", help="Recalculate the rating bucket used by one event_code")
    return parser.parse_args()


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    connection = connect(DATABASE_PATH)
    try:
        with transaction(connection):
            if args.event:
                result = recalculate_event_bucket_ratings(connection, args.event)
            else:
                result = recalculate_ratings(connection, args.bucket)
        print(
            "Ratings recalculated -> bucket={} events={} skipped={} players={} history={}".format(
                result["bucket_code"] or "all",
                result["event_count"],
                result["skipped_event_count"],
                result["player_count"],
                result["history_count"],
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
