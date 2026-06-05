import argparse
import json
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Debug timed event scoring inputs/outputs.")
    parser.add_argument("event_code", help="Event code to inspect")
    parser.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parents[1] / "data" / "tournament_hub.sqlite3"),
        help="Path to sqlite db",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    event = conn.execute(
        """
        SELECT e.id, e.event_code, e.event_name, e.competition_type, e.metadata_json
        FROM events e
        WHERE e.event_code = ?
        """,
        (args.event_code,),
    ).fetchone()
    if event is None:
        raise SystemExit("event not found: {}".format(args.event_code))

    print("event:", event["event_code"], event["event_name"], event["competition_type"])
    print("metadata:", event["metadata_json"])

    rows = conn.execute(
        """
        SELECT
            p.display_name,
            pa.account_key AS username,
            er.rank_value,
            er.primary_metric_value,
            er.secondary_metric_value,
            er.result_payload_json
        FROM event_results er
        JOIN players p ON p.id = er.player_id
        LEFT JOIN player_accounts pa ON pa.player_id = p.id AND pa.is_primary = 1
        WHERE er.event_id = ?
        ORDER BY er.rank_value ASC, LOWER(COALESCE(pa.account_key, p.display_name)) ASC
        """,
        (event["id"],),
    ).fetchall()

    print("results_count:", len(rows))
    for row in rows:
        payload = json.loads(row["result_payload_json"] or "{}")
        source_records = payload.get("source_records") or []
        comp_scores = [item.get("competition_score") for item in source_records if item.get("competition_score") is not None]
        raw_scores = [item.get("final_score") for item in source_records if item.get("final_score") is not None]
        print(
            "[rank={}] {} ({}) primary={} attempts={} total_full_boards={} comp_scores={} raw_scores={}".format(
                row["rank_value"],
                row["display_name"],
                row["username"] or "-",
                row["primary_metric_value"],
                row["secondary_metric_value"],
                payload.get("total_full_boards"),
                comp_scores[:12],
                raw_scores[:12],
            )
        )


if __name__ == "__main__":
    main()

