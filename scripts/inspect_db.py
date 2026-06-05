import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Inspect tournament hub database")
    parser.add_argument("--event", help="Show one event by event_code")
    parser.add_argument("--limit", type=int, default=10, help="Number of rows to show in summary lists")
    return parser.parse_args()


def fetch_scalar(connection, query, params=()):
    row = connection.execute(query, params).fetchone()
    if row is None:
        return 0
    return row[0]


def print_summary(connection, limit):
    print("Database: {}".format(DATABASE_PATH))
    print("")
    print("Counts")
    print("- events: {}".format(fetch_scalar(connection, "SELECT COUNT(*) FROM events")))
    print("- results: {}".format(fetch_scalar(connection, "SELECT COUNT(*) FROM event_results")))
    print("- snapshots: {}".format(fetch_scalar(connection, "SELECT COUNT(*) FROM result_snapshots")))
    print("- players: {}".format(fetch_scalar(connection, "SELECT COUNT(*) FROM players")))
    print("- player_accounts: {}".format(fetch_scalar(connection, "SELECT COUNT(*) FROM player_accounts")))
    print("- performance_records: {}".format(fetch_scalar(connection, "SELECT COUNT(*) FROM performance_records")))
    print("- sync_runs: {}".format(fetch_scalar(connection, "SELECT COUNT(*) FROM sync_runs")))
    print("")

    rows = connection.execute(
        """
        SELECT
            e.event_code,
            e.event_name,
            p.code AS platform_code,
            v.code AS variant_code,
            e.status,
            e.is_rated,
            e.is_official,
            e.start_time,
            COUNT(er.id) AS result_count
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        LEFT JOIN event_results er ON er.event_id = e.id
        GROUP BY e.id, e.event_code, e.event_name, p.code, v.code, e.status, e.is_rated, e.is_official, e.start_time
        ORDER BY COALESCE(e.start_time, '') DESC, e.event_code DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    print("Recent Events")
    if not rows:
        print("- none")
        return

    for row in rows:
        variant_code = row["variant_code"] or "-"
        print(
            "- {event_code} | {event_name} | {platform_code} | {variant_code} | "
            "status={status} | rated={is_rated} | official={is_official} | results={result_count}".format(
                **row
            )
        )


def print_event(connection, event_code, limit):
    event = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            p.code AS platform_code,
            v.code AS variant_code,
            rb.code AS rating_bucket_code,
            e.event_type,
            e.competition_type,
            e.status,
            e.is_rated,
            e.is_official,
            e.start_time,
            e.end_time,
            e.seal_time,
            e.source,
            e.tags_json
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        LEFT JOIN rating_buckets rb ON rb.id = e.rating_bucket_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()

    if event is None:
        print("Event not found: {}".format(event_code))
        return

    print("Event")
    print("- code: {}".format(event["event_code"]))
    print("- name: {}".format(event["event_name"]))
    print("- platform: {}".format(event["platform_code"]))
    print("- variant: {}".format(event["variant_code"] or "-"))
    print("- rating_bucket: {}".format(event["rating_bucket_code"] or "-"))
    print("- event_type: {}".format(event["event_type"]))
    print("- competition_type: {}".format(event["competition_type"]))
    print("- status: {}".format(event["status"]))
    print("- is_rated: {}".format(event["is_rated"]))
    print("- is_official: {}".format(event["is_official"]))
    print("- start_time: {}".format(event["start_time"] or "-"))
    print("- end_time: {}".format(event["end_time"] or "-"))
    print("- seal_time: {}".format(event["seal_time"] or "-"))
    print("- source: {}".format(event["source"] or "-"))
    print("- tags_json: {}".format(event["tags_json"] or "[]"))
    print("")

    results = connection.execute(
        """
        SELECT
            er.rank_value,
            p.display_name,
            pa.account_name,
            er.primary_metric_type,
            er.primary_metric_value,
            er.secondary_metric_type,
            er.secondary_metric_value,
            er.result_status
        FROM event_results er
        JOIN players p ON p.id = er.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = p.id
           AND pa.platform_id = (SELECT platform_id FROM events WHERE id = er.event_id)
           AND pa.is_primary = 1
        WHERE er.event_id = ?
        ORDER BY er.rank_value ASC, p.display_name ASC
        LIMIT ?
        """,
        (event["id"], limit),
    ).fetchall()

    print("Top Results")
    if not results:
        print("- none")
    else:
        for row in results:
            account_name = row["account_name"] or "-"
            secondary_type = row["secondary_metric_type"] or "-"
            secondary_value = row["secondary_metric_value"]
            if secondary_value is None:
                secondary_value = "-"
            print(
                "- #{rank_value} {display_name} ({account_name}) | "
                "{primary_metric_type}={primary_metric_value} | "
                "{secondary_metric_type}={secondary_metric_value} | "
                "status={result_status}".format(
                    rank_value=row["rank_value"],
                    display_name=row["display_name"],
                    account_name=account_name,
                    primary_metric_type=row["primary_metric_type"],
                    primary_metric_value=row["primary_metric_value"],
                    secondary_metric_type=secondary_type,
                    secondary_metric_value=secondary_value,
                    result_status=row["result_status"],
                )
            )

    snapshot_count = fetch_scalar(
        connection,
        "SELECT COUNT(*) FROM result_snapshots WHERE event_id = ?",
        (event["id"],),
    )
    print("")
    print("Snapshots")
    print("- count: {}".format(snapshot_count))


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    connection = connect(DATABASE_PATH)
    try:
        if args.event:
            print_event(connection, args.event, args.limit)
        else:
            print_summary(connection, args.limit)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
