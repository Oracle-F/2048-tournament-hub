import json
from datetime import datetime
from pathlib import Path

from importers.organizer_json_parser import parse_organizer_ranking_json


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def lookup_id(connection, table_name, code):
    row = connection.execute(
        "SELECT id FROM {} WHERE code = ?".format(table_name),
        (code,),
    ).fetchone()
    return None if row is None else row["id"]


def ensure_player(connection, display_name, username, platform_id):
    existing = connection.execute(
        """
        SELECT p.id AS player_id
        FROM player_accounts pa
        JOIN players p ON p.id = pa.player_id
        WHERE pa.platform_id = ? AND pa.account_key = ?
        """,
        (platform_id, username),
    ).fetchone()
    if existing is not None:
        connection.execute(
            """
            UPDATE players
            SET display_name = ?, updated_at = ?
            WHERE id = ?
            """,
            (display_name, now_iso(), existing["player_id"]),
        )
        return existing["player_id"], False

    cursor = connection.execute(
        """
        INSERT INTO players (display_name, status, metadata_json, created_at, updated_at)
        VALUES (?, 'active', ?, ?, ?)
        """,
        (display_name, json.dumps({}, ensure_ascii=False), now_iso(), now_iso()),
    )
    player_id = cursor.lastrowid
    connection.execute(
        """
        INSERT INTO player_accounts (
            player_id, platform_id, account_key, account_name, account_display_name,
            is_primary, metadata_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            player_id,
            platform_id,
            username,
            username,
            display_name,
            json.dumps({}, ensure_ascii=False),
            now_iso(),
            now_iso(),
        ),
    )
    return player_id, True


def ensure_event(connection, parsed):
    event = parsed["event"]
    platform_id = lookup_id(connection, "platforms", event["platform_code"])
    variant_id = lookup_id(connection, "variants", event["variant_code"]) if event.get("variant_code") else None
    rating_bucket_id = (
        lookup_id(connection, "rating_buckets", event["rating_bucket_code"])
        if event.get("rating_bucket_code")
        else None
    )
    if platform_id is None:
        raise ValueError("Unknown platform code: {}".format(event["platform_code"]))

    connection.execute(
        """
        INSERT INTO events (
            event_code, event_name, platform_id, variant_id, rating_bucket_id,
            event_type, competition_type, status, is_official, is_rated, start_time,
            end_time, seal_time, source, tags_json, metadata_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_code) DO UPDATE SET
            event_name = excluded.event_name,
            platform_id = excluded.platform_id,
            variant_id = excluded.variant_id,
            rating_bucket_id = excluded.rating_bucket_id,
            event_type = excluded.event_type,
            competition_type = excluded.competition_type,
            status = excluded.status,
            is_official = excluded.is_official,
            is_rated = excluded.is_rated,
            start_time = excluded.start_time,
            end_time = excluded.end_time,
            seal_time = excluded.seal_time,
            source = excluded.source,
            tags_json = excluded.tags_json,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            event["event_code"],
            event["event_name"],
            platform_id,
            variant_id,
            rating_bucket_id,
            event["event_type"],
            event["competition_type"],
            event["status"],
            1 if event["is_official"] else 0,
            1 if event["is_rated"] else 0,
            event.get("start_time"),
            event.get("end_time"),
            event.get("seal_time"),
            event.get("source"),
            json.dumps(event.get("tags", []), ensure_ascii=False),
            json.dumps(event.get("metadata", {}), ensure_ascii=False),
            now_iso(),
            now_iso(),
        ),
    )
    event_id = connection.execute(
        "SELECT id FROM events WHERE event_code = ?",
        (event["event_code"],),
    ).fetchone()["id"]
    return event_id, platform_id


def upsert_rule_set(connection, event_id, rule_set):
    connection.execute(
        """
        INSERT INTO event_rule_sets (
            event_id, version, rule_type, ranking_metric, ranking_order,
            aggregation_method, validation_rule, tiebreakers_json, rule_config_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id, version) DO UPDATE SET
            rule_type = excluded.rule_type,
            ranking_metric = excluded.ranking_metric,
            ranking_order = excluded.ranking_order,
            aggregation_method = excluded.aggregation_method,
            validation_rule = excluded.validation_rule,
            tiebreakers_json = excluded.tiebreakers_json,
            rule_config_json = excluded.rule_config_json
        """,
        (
            event_id,
            rule_set["version"],
            rule_set["rule_type"],
            rule_set["ranking_metric"],
            rule_set["ranking_order"],
            rule_set.get("aggregation_method"),
            rule_set.get("validation_rule"),
            json.dumps(rule_set.get("tiebreakers", []), ensure_ascii=False),
            json.dumps(rule_set.get("rule_config", {}), ensure_ascii=False),
            now_iso(),
        ),
    )


def upsert_result(connection, event_id, player_id, result):
    extra = result.get("extra") or {}
    scoring_game_count = extra.get("计分局数")
    if scoring_game_count is None:
        scoring_game_count = extra.get("scoring_game_count")
    total_full_boards = result.get("secondary_metric_value")

    connection.execute(
        """
        INSERT INTO event_results (
            event_id, player_id, result_status, rank_value,
            primary_metric_type, primary_metric_value,
            secondary_metric_type, secondary_metric_value,
            scoring_game_count, total_full_boards,
            is_published, result_payload_json, calculated_at, updated_at
        )
        VALUES (?, ?, 'archived', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(event_id, player_id) DO UPDATE SET
            result_status = excluded.result_status,
            rank_value = excluded.rank_value,
            primary_metric_type = excluded.primary_metric_type,
            primary_metric_value = excluded.primary_metric_value,
            secondary_metric_type = excluded.secondary_metric_type,
            secondary_metric_value = excluded.secondary_metric_value,
            scoring_game_count = excluded.scoring_game_count,
            total_full_boards = excluded.total_full_boards,
            result_payload_json = excluded.result_payload_json,
            updated_at = excluded.updated_at
        """,
        (
            event_id,
            player_id,
            result["rank"],
            result["primary_metric_type"],
            result["primary_metric_value"],
            result.get("secondary_metric_type"),
            result.get("secondary_metric_value"),
            scoring_game_count,
            total_full_boards,
            json.dumps(result["raw_payload"], ensure_ascii=False),
            now_iso(),
            now_iso(),
        ),
    )


def upsert_snapshot(connection, event_id, snapshot):
    connection.execute(
        """
        INSERT INTO result_snapshots (
            event_id, snapshot_type, snapshot_label, payload_json, signature, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            snapshot["snapshot_type"],
            snapshot.get("snapshot_label"),
            json.dumps(snapshot["payload"], ensure_ascii=False, indent=2),
            snapshot.get("signature"),
            now_iso(),
        ),
    )


def record_sync_run_start(connection, source_type, metadata):
    cursor = connection.execute(
        """
        INSERT INTO sync_runs (
            source_type, status, started_at, records_seen, records_inserted, records_updated, metadata_json
        )
        VALUES (?, 'running', ?, 0, 0, 0, ?)
        """,
        (source_type, now_iso(), json.dumps(metadata, ensure_ascii=False)),
    )
    return cursor.lastrowid


def finish_sync_run(connection, sync_run_id, status, seen_count, inserted_count, updated_count, error_message=None):
    connection.execute(
        """
        UPDATE sync_runs
        SET status = ?, finished_at = ?, records_seen = ?, records_inserted = ?,
            records_updated = ?, error_message = ?
        WHERE id = ?
        """,
        (status, now_iso(), seen_count, inserted_count, updated_count, error_message, sync_run_id),
    )


def ingest_parsed_event(connection, parsed):
    event_id, platform_id = ensure_event(connection, parsed)
    upsert_rule_set(connection, event_id, parsed["rule_set"])

    inserted_players = 0
    for result in parsed["results"]:
        player_id, inserted = ensure_player(
            connection,
            display_name=result["display_name"],
            username=result["username"],
            platform_id=platform_id,
        )
        if inserted:
            inserted_players += 1
        upsert_result(connection, event_id, player_id, result)

    upsert_snapshot(connection, event_id, parsed["snapshot"])
    return {
        "event_id": event_id,
        "result_count": len(parsed["results"]),
        "new_players": inserted_players,
    }


def ingest_organizer_json_file(connection, file_path: Path):
    parsed = parse_organizer_ranking_json(file_path)
    return ingest_parsed_event(connection, parsed)

