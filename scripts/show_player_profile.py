import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect
from services.player_profile_service import (
    build_player_profile,
    build_player_profile_by_query,
    export_player_profile,
    find_players,
    format_player_profile,
)
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Show or export one player profile")
    parser.add_argument("query", nargs="?", help="display name, account name, or account key")
    parser.add_argument("--player-id", type=int, help="player id")
    parser.add_argument("--search", action="store_true", help="only list matched players")
    parser.add_argument("--export", action="store_true", help="export txt/json profile")
    return parser.parse_args()


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    connection = connect(DATABASE_PATH)
    try:
        if args.search:
            if not args.query:
                print("Please provide a search query.")
                return
            rows = find_players(connection, args.query)
            if not rows:
                print("No players matched.")
                return
            for row in rows:
                print(
                    "- id={} | {} | accounts={} | keys={}".format(
                        row["id"],
                        row["display_name"],
                        row["account_names"] or "-",
                        row["account_keys"] or "-",
                    )
                )
            return

        if args.player_id:
            profile = build_player_profile(connection, args.player_id)
        elif args.query:
            profile = build_player_profile_by_query(connection, args.query)
        else:
            print("Please provide a query or --player-id.")
            return

        if args.export:
            result = export_player_profile(connection, profile["player"]["id"])
            print("Exported player profile -> {}".format(result["output_dir"]))
            print("TXT: {}".format(result["txt_path"]))
            print("JSON: {}".format(result["json_path"]))
            return

        print(format_player_profile(profile))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
