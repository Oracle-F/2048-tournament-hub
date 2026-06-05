import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from services.raw_score_import_service import import_score_file
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Import raw score records for one event")
    parser.add_argument("--event", required=True, help="Target event_code")
    parser.add_argument("--file", required=True, help="CSV or JSON file path")
    return parser.parse_args()


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    file_path = Path(args.file).expanduser().resolve()
    connection = connect(DATABASE_PATH)
    try:
        with transaction(connection):
            result = import_score_file(connection, args.event, file_path)
        print(
            "Imported scores for {event_code} from {file_path} -> records={record_count}, new_players={new_players}".format(
                **result
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()

