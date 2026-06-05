import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from services.event_admin_service import create_or_update_event, infer_rating_bucket_code
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Create or update one event in tournament hub")
    parser.add_argument("--event-code")
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--platform", default="2048verse")
    parser.add_argument("--variant", required=True)
    parser.add_argument("--event-type", required=True)
    parser.add_argument("--competition-type", required=True)
    parser.add_argument("--rating-bucket")
    parser.add_argument("--target-value", type=int)
    parser.add_argument("--status", default="draft")
    parser.add_argument("--start-time")
    parser.add_argument("--end-time")
    parser.add_argument("--seal-time")
    parser.add_argument("--source", default="manual_setup")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--tiebreaker", action="append", default=[])
    parser.add_argument("--aggregation-method")
    parser.add_argument("--aggregation-count", type=int)
    parser.add_argument("--missing-attempt-policy")
    parser.add_argument("--official", action="store_true")
    parser.add_argument("--rated", action="store_true")
    parser.add_argument("--remark")
    parser.add_argument("--metadata-json")
    return parser.parse_args()


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    if args.metadata_json:
        metadata = json.loads(args.metadata_json)
    else:
        metadata = {}
    if args.remark:
        metadata["remark"] = args.remark

    rule_overrides = {}
    if args.tiebreaker:
        rule_overrides["tiebreakers"] = args.tiebreaker
    if args.aggregation_method:
        rule_overrides["aggregation_method"] = args.aggregation_method
    if args.aggregation_count is not None:
        rule_overrides["aggregation_count"] = args.aggregation_count
    if args.missing_attempt_policy:
        rule_overrides["missing_attempt_policy"] = args.missing_attempt_policy

    rating_bucket_code = args.rating_bucket or infer_rating_bucket_code(
        args.variant,
        args.competition_type,
        args.target_value,
    )

    connection = connect(DATABASE_PATH)
    try:
        with transaction(connection):
            result = create_or_update_event(
                connection,
                event_code=args.event_code,
                event_name=args.event_name,
                platform_code=args.platform,
                variant_code=args.variant,
                event_type=args.event_type,
                competition_type=args.competition_type,
                rating_bucket_code=rating_bucket_code,
                status=args.status,
                is_official=args.official,
                is_rated=args.rated,
                registration_close_time=args.start_time,
                start_time=args.start_time,
                end_time=args.end_time,
                seal_time=args.seal_time,
                source=args.source,
                tags=args.tag,
                target_value=args.target_value,
                metadata=metadata,
                rule_overrides=rule_overrides,
            )
        print(
            "Saved event {event_code} -> id={event_id}, rating_bucket={rating_bucket_code}".format(
                **result
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
