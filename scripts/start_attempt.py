import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from services.attempt_service import start_attempt_session
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Create one manual first-game attempt session")
    parser.add_argument("--event", required=True, help="Target event_code")
    parser.add_argument("--user", required=True, help="Platform username")
    parser.add_argument("--display-name", help="Optional display name override")
    parser.add_argument("--deadline-minutes", type=int, default=30)
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
            result = start_attempt_session(
                connection,
                event_code=args.event,
                username=args.user,
                display_name=args.display_name,
                deadline_minutes=args.deadline_minutes,
            )
        print(
            "Started attempt session {attempt_session_id} for {username} in {event_code}, deadline={deadline_time}".format(
                **result
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()

