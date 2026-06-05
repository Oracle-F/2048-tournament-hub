import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect
from services.rating_service import (
    export_leaderboards,
    list_performance_leaderboard,
    list_rating_buckets,
    list_rating_leaderboard,
)
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Show or export long-term leaderboards")
    parser.add_argument("--bucket", help="rating bucket code")
    parser.add_argument("--type", choices=["rating", "performance"], default="rating")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--export", action="store_true", help="Export txt/json leaderboards")
    parser.add_argument("--list-buckets", action="store_true", help="List available rating buckets")
    return parser.parse_args()


def print_buckets(connection):
    rows = list_rating_buckets(connection)
    if not rows:
        print("No rating buckets.")
        return
    for row in rows:
        print(
            "- {code} | {name} | family={family_code} | events={rated_event_count} | players={rated_player_count}".format(
                **row
            )
        )


def print_rating(connection, bucket_code, limit):
    result = list_rating_leaderboard(connection, bucket_code, limit=limit)
    print("{} rating leaderboard ({})".format(result["bucket"]["name"], result["bucket"]["code"]))
    if not result["rows"]:
        print("- no ratings yet")
        return
    for index, row in enumerate(result["rows"], start=1):
        print(
            "{}. {} | rating {:.1f} | RD {:.1f} | events {} | last {}".format(
                index,
                row["display_name"],
                row["rating_value"],
                row["rating_deviation"],
                row["event_count"],
                row["last_event_code"] or "-",
            )
        )


def print_performance(connection, bucket_code, limit):
    result = list_performance_leaderboard(connection, bucket_code, limit=limit)
    print("{} performance leaderboard ({})".format(result["bucket"]["name"], result["bucket"]["code"]))
    if not result["rows"]:
        print("- no results yet")
        return
    for index, row in enumerate(result["rows"], start=1):
        print(
            "{}. {} | {} {} | event {}".format(
                index,
                row["display_name"],
                row["primary_metric_type"],
                row["primary_metric_value"],
                row["event_code"],
            )
        )


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    connection = connect(DATABASE_PATH)
    try:
        if args.list_buckets:
            print_buckets(connection)
            return
        if not args.bucket:
            print("Please provide --bucket, or use --list-buckets.")
            return
        if args.export:
            result = export_leaderboards(connection, args.bucket, limit=args.limit)
            print("Exported leaderboards -> {}".format(result["output_dir"]))
            print("TXT: {}".format(result["txt_path"]))
            print("JSON: {}".format(result["json_path"]))
            print("PNG: {}".format(result["image_path"]))
            return
        if args.type == "rating":
            print_rating(connection, args.bucket, args.limit)
        else:
            print_performance(connection, args.bucket, args.limit)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
