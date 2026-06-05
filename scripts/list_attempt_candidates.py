import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect
from services.attempt_bind_service import list_attempt_candidates
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="List candidate verse games for one attempt session")
    parser.add_argument("--event", required=True, help="Target event_code")
    parser.add_argument("--user", required=True, help="Platform username")
    parser.add_argument("--session-id", type=int, help="Optional explicit attempt_session id")
    parser.add_argument("--max-pages", type=int, default=10)
    return parser.parse_args()


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    connection = connect(DATABASE_PATH)
    try:
        result = list_attempt_candidates(
            connection,
            event_code=args.event,
            username=args.user,
            session_id=args.session_id,
            max_pages=args.max_pages,
        )
        session = result["session"]
        print(
            "Session {} | status={} | user={} | start={} | deadline={}".format(
                session["id"],
                session["status"],
                session["account_key"],
                session["start_command_time"],
                session["lock_deadline_time"],
            )
        )
        if not result["candidates"]:
            print("No candidates found.")
            return
        for item in result["candidates"]:
            print(
                "- id={source_record_id} score={score} started={started_at} ended={ended_at} max={max_tile_value} user_id={user_id} window={within_lock_window} start_offset={start_offset_seconds}s".format(
                    **item
                )
            )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
