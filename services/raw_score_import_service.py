import json
from datetime import datetime
from pathlib import Path

from importers.raw_score_file_parser import parse_score_file
from services.ingest_service import ensure_player


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def lookup_event(connection, event_code):
    row = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.platform_id,
            e.variant_id,
            p.code AS platform_code,
            v.code AS variant_code
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if row is None:
        raise ValueError("Event not found: {}".format(event_code))
    return row


def upsert_performance_record(connection, event_row, row):
    player_id, inserted_player = ensure_player(
        connection,
        display_name=row["display_name"],
        username=row["username"],
        platform_id=event_row["platform_id"],
    )
    player_account_id = connection.execute(
        """
        SELECT id FROM player_accounts
        WHERE player_id = ? AND platform_id = ? AND account_key = ?
        """,
        (player_id, event_row["platform_id"], row["username"]),
    ).fetchone()["id"]

    connection.execute(
        """
        INSERT INTO performance_records (
            platform_id, player_account_id, variant_id, source_record_id, record_type,
            started_at, ended_at, raw_score, final_score, primary_time_ms,
            target_tile_value, score_before_target, evidence_json, result_state,
            raw_payload_json, ingested_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(platform_id, source_record_id) DO UPDATE SET
            player_account_id = excluded.player_account_id,
            variant_id = excluded.variant_id,
            record_type = excluded.record_type,
            started_at = excluded.started_at,
            ended_at = excluded.ended_at,
            raw_score = excluded.raw_score,
            final_score = excluded.final_score,
            primary_time_ms = excluded.primary_time_ms,
            target_tile_value = excluded.target_tile_value,
            score_before_target = excluded.score_before_target,
            evidence_json = excluded.evidence_json,
            result_state = excluded.result_state,
            raw_payload_json = excluded.raw_payload_json,
            updated_at = excluded.updated_at
        """,
        (
            event_row["platform_id"],
            player_account_id,
            event_row["variant_id"],
            row["source_record_id"],
            row["record_type"],
            row["started_at"],
            row["ended_at"],
            row["raw_score"],
            row["final_score"],
            row["primary_time_ms"],
            row["target_tile_value"],
            row["score_before_target"],
            json.dumps(row["evidence"], ensure_ascii=False) if row["evidence"] is not None else None,
            row["result_state"],
            json.dumps(row["raw_payload"], ensure_ascii=False),
            now_iso(),
            now_iso(),
        ),
    )
    performance_record_id = connection.execute(
        """
        SELECT id FROM performance_records
        WHERE platform_id = ? AND source_record_id = ?
        """,
        (event_row["platform_id"], row["source_record_id"]),
    ).fetchone()["id"]

    return {
        "player_id": player_id,
        "player_inserted": inserted_player,
        "performance_record_id": performance_record_id,
    }


def upsert_event_attempt_record(connection, event_id, player_id, performance_record_id, row):
    derived_metric_json = {
        "competition_score": row["competition_score"],
        "raw_score": row["raw_score"],
        "final_score": row["final_score"],
        "primary_time_ms": row["primary_time_ms"],
        "target_tile_value": row["target_tile_value"],
    }
    connection.execute(
        """
        INSERT INTO event_attempt_records (
            event_id, player_id, performance_record_id, record_role, evaluation_status,
            derived_metric_value, sort_metric_primary, sort_metric_secondary,
            derived_metric_json, created_at, updated_at
        )
        VALUES (?, ?, ?, 'candidate', 'pending', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id, performance_record_id) DO UPDATE SET
            player_id = excluded.player_id,
            record_role = excluded.record_role,
            evaluation_status = excluded.evaluation_status,
            derived_metric_value = excluded.derived_metric_value,
            sort_metric_primary = excluded.sort_metric_primary,
            sort_metric_secondary = excluded.sort_metric_secondary,
            derived_metric_json = excluded.derived_metric_json,
            updated_at = excluded.updated_at
        """,
        (
            event_id,
            player_id,
            performance_record_id,
            row["competition_score"],
            row["competition_score"],
            row["primary_time_ms"],
            json.dumps(derived_metric_json, ensure_ascii=False),
            now_iso(),
            now_iso(),
        ),
    )


def import_score_file(connection, event_code: str, file_path: Path):
    event_row = lookup_event(connection, event_code)
    records = parse_score_file(file_path, default_source_prefix=event_code)
    inserted_players = 0

    for row in records:
        saved = upsert_performance_record(connection, event_row, row)
        if saved["player_inserted"]:
            inserted_players += 1
        upsert_event_attempt_record(
            connection,
            event_id=event_row["id"],
            player_id=saved["player_id"],
            performance_record_id=saved["performance_record_id"],
            row=row,
        )

    return {
        "event_id": event_row["id"],
        "event_code": event_row["event_code"],
        "record_count": len(records),
        "new_players": inserted_players,
        "file_path": str(file_path),
    }


def add_manual_score(
    connection,
    event_code,
    *,
    username,
    display_name=None,
    source_record_id=None,
    started_at=None,
    ended_at,
    raw_score=None,
    final_score=None,
    competition_score=None,
    primary_time_ms=None,
    target_tile_value=None,
    score_before_target=None,
    evidence_note=None,
):
    event_row = lookup_event(connection, event_code)
    display_name = display_name or username
    source_record_id = source_record_id or "{}_{}_manual_{}".format(event_code, username, now_iso().replace(":", "").replace("-", ""))
    effective_started_at = started_at or ended_at
    if final_score is None:
        final_score = raw_score
    if competition_score is None:
        competition_score = final_score if final_score is not None else raw_score
    evidence = {"note": evidence_note} if evidence_note else None
    row = {
        "username": username,
        "display_name": display_name,
        "source_record_id": source_record_id,
        "record_type": "manual_score_entry",
        "started_at": effective_started_at,
        "ended_at": ended_at,
        "raw_score": raw_score,
        "final_score": final_score,
        "competition_score": competition_score,
        "primary_time_ms": primary_time_ms,
        "target_tile_value": target_tile_value,
        "score_before_target": score_before_target,
        "result_state": "completed",
        "evidence": evidence,
        "raw_payload": {
            "source": "manual_score_entry",
            "entered_at": now_iso(),
            "started_at_was_unknown": started_at is None,
            "evidence_note": evidence_note,
        },
    }
    saved = upsert_performance_record(connection, event_row, row)
    upsert_event_attempt_record(
        connection,
        event_id=event_row["id"],
        player_id=saved["player_id"],
        performance_record_id=saved["performance_record_id"],
        row=row,
    )
    return {
        "event_id": event_row["id"],
        "event_code": event_row["event_code"],
        "player_id": saved["player_id"],
        "performance_record_id": saved["performance_record_id"],
        "source_record_id": source_record_id,
        "new_player": saved["player_inserted"],
    }
