import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import connect, transaction
from services.timed_reservation_service import settle_due_reservations
from settings import DATABASE_PATH


def main():
    parser = argparse.ArgumentParser(description="Rebuild reservation timed-scoring settlements to V2 game-level records.")
    parser.add_argument("event_code", help="Event code, e.g. 20260428")
    args = parser.parse_args()

    connection = connect(DATABASE_PATH)
    with transaction(connection):
        result = settle_due_reservations(connection, event_code=args.event_code)
    print(
        "rebuild done: event_code={} settled_count={} touched_events={}".format(
            args.event_code,
            result.get("settled_count"),
            result.get("event_count"),
        )
    )


if __name__ == "__main__":
    main()
