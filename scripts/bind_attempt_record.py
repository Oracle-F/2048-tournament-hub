import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from services.attempt_bind_service import bind_attempt_record, find_attempt_candidates_by_result
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Bind one verse game record to an attempt session")
    parser.add_argument("--event", required=True, help="Target event_code")
    parser.add_argument("--user", required=True, help="Platform username")
    parser.add_argument("--record-id", help="Internal Verse game id to bind")
    parser.add_argument("--score", type=int, help="Supplement score used for matching")
    parser.add_argument("--ended-at", help="Supplement game end time, format YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--max-tile", type=int, help="Optional max tile value used for matching")
    parser.add_argument("--user-id", help="Optional platform user id used for matching")
    parser.add_argument("--tolerance-minutes", type=int, default=2, help="End time tolerance used for matching")
    parser.add_argument("--session-id", type=int, help="Optional explicit attempt_session id")
    parser.add_argument("--max-pages", type=int, default=30)
    args = parser.parse_args()
    if not args.record_id and (args.score is None or not args.ended_at):
        parser.error("Either --record-id or both --score and --ended-at are required.")
    return args


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    connection = connect(DATABASE_PATH)
    try:
        with transaction(connection):
            if args.record_id:
                result = bind_attempt_record(
                    connection,
                    event_code=args.event,
                    username=args.user,
                    source_record_id=args.record_id,
                    session_id=args.session_id,
                    max_pages=args.max_pages,
                )
            else:
                matched = find_attempt_candidates_by_result(
                    connection,
                    event_code=args.event,
                    username=args.user,
                    score=args.score,
                    ended_at=args.ended_at,
                    max_tile_value=args.max_tile,
                    user_id=args.user_id,
                    tolerance_minutes=args.tolerance_minutes,
                    session_id=args.session_id,
                    max_pages=args.max_pages,
                )
                candidates = matched["candidates"]
                if not candidates:
                    raise ValueError("No matching Verse record found.")
                if len(candidates) > 1:
                    for item in candidates:
                        print(
                            "- id={source_record_id} score={score} started={started_at} ended={ended_at} delta={end_time_delta_seconds}s rec={recommendation_score} max={max_tile_value} user_id={user_id}".format(
                                **item
                            )
                        )
                        print(
                            "  window={} start_offset={}s deadline_overrun={}s exact_second={} exact_minute={} max_tile_match={} user_id_match={}".format(
                                "yes" if item.get("within_lock_window") else "no",
                                item["start_offset_seconds"] if item.get("start_offset_seconds") is not None else "-",
                                item["deadline_overrun_seconds"] if item.get("deadline_overrun_seconds") is not None else "-",
                                "yes" if item.get("exact_second_match") else "no",
                                "yes" if item.get("exact_minute_match") else "no",
                                item.get("max_tile_match") or "-",
                                item.get("user_id_match") or "-",
                            )
                        )
                        print("  tags={}".format(", ".join(item["match_tags"]) if item.get("match_tags") else "-"))
                        print("  payload_keys={}".format(", ".join(item["payload_keys"]) if item.get("payload_keys") else "-"))
                    raise ValueError(
                        "Multiple matching Verse records found. Narrow the conditions or bind by --record-id."
                    )
                result = bind_attempt_record(
                    connection,
                    event_code=args.event,
                    username=args.user,
                    source_record_id=candidates[0]["source_record_id"],
                    session_id=args.session_id,
                    max_pages=args.max_pages,
                )
        print(
            "Bound record {source_record_id} to session {session_id} for {username} in {event_code}, score={score}".format(
                **result
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
