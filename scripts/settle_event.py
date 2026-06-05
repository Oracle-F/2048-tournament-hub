import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from services.settlement_service import settle_event
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Settle one event into event_results")
    parser.add_argument("--event", required=True, help="Target event_code")
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
            result = settle_event(connection, args.event)
        print(
            "Settled {event_code} -> players={player_count}, source_records={record_count}, aggregation={aggregation_method}".format(
                **result
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
