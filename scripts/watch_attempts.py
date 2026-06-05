import argparse
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from services.attempt_service import process_open_attempt_sessions
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Watch and process open first-game attempt sessions")
    parser.add_argument("--limit", type=int, help="Process only the first N open sessions")
    parser.add_argument("--loop", action="store_true", help="Keep polling until interrupted")
    parser.add_argument("--interval", type=int, default=5, help="Polling interval in seconds when --loop is used")
    return parser.parse_args()


def run_once(connection, limit):
    with transaction(connection):
        results = process_open_attempt_sessions(connection, limit=limit)
    if not results:
        print("No open attempt sessions.")
        return
    for result in results:
        message = "session={session_id} status={status}".format(**result)
        if "source_record_id" in result:
            message += " source_record_id={source_record_id} score={score}".format(**result)
        if "reason" in result:
            message += " reason={reason}".format(**result)
        print(message)


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    connection = connect(DATABASE_PATH)
    try:
        if args.loop:
            while True:
                run_once(connection, args.limit)
                time.sleep(max(1, args.interval))
        else:
            run_once(connection, args.limit)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
