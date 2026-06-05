import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import connect, transaction
from services.settlement_service import settle_event
from settings import DATABASE_PATH


def main():
    parser = argparse.ArgumentParser(description="Remove a player from an event by setting registration cancelled.")
    parser.add_argument("event_code", help="Event code, e.g. 20260428")
    parser.add_argument("username", help="Primary account key or display_name")
    parser.add_argument("--no-settle", action="store_true", help="Skip settle_event after update")
    args = parser.parse_args()

    connection = connect(DATABASE_PATH)
    with transaction(connection):
        rows = connection.execute(
            """
            SELECT
                e.id AS event_id,
                e.event_code,
                p.id AS player_id,
                p.display_name,
                pa.account_key,
                r.status
            FROM events e
            JOIN registrations r ON r.event_id = e.id
            JOIN players p ON p.id = r.player_id
            LEFT JOIN player_accounts pa ON pa.player_id = p.id AND pa.is_primary = 1
            WHERE e.event_code = ?
              AND (pa.account_key = ? OR p.display_name = ?)
            """,
            (args.event_code, args.username, args.username),
        ).fetchall()

        if not rows:
            print("not found: event_code={} username={}".format(args.event_code, args.username))
            return
        if len(rows) > 1:
            print("multiple matches found, please disambiguate:")
            for row in rows:
                print(dict(row))
            return

        row = rows[0]
        event_id = row["event_id"]
        player_id = row["player_id"]
        print("target:", dict(row))

        connection.execute(
            """
            UPDATE registrations
            SET status = 'cancelled'
            WHERE event_id = ? AND player_id = ?
            """,
            (event_id, player_id),
        )
        reservation_cols = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(timed_event_reservations)").fetchall()
        }
        if "updated_at" in reservation_cols:
            connection.execute(
                """
                UPDATE timed_event_reservations
                SET status = 'cancelled', updated_at = datetime('now', 'localtime')
                WHERE event_id = ? AND player_id = ?
                """,
                (event_id, player_id),
            )
        else:
            connection.execute(
                """
                UPDATE timed_event_reservations
                SET status = 'cancelled'
                WHERE event_id = ? AND player_id = ?
                """,
                (event_id, player_id),
            )
        print("registration cancelled.")

        if not args.no_settle:
            result = settle_event(connection, args.event_code)
            print(
                "settled: event_code={} player_count={} record_count={} aggregation={}".format(
                    result.get("event_code"),
                    result.get("player_count"),
                    result.get("record_count"),
                    result.get("aggregation_method"),
                )
            )


if __name__ == "__main__":
    main()
