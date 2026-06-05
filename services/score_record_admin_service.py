import json
from datetime import datetime

from services.raw_score_import_service import lookup_event


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _parse_json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def list_event_score_records(connection, event_code, *, username=None, include_voided=True):
    event = lookup_event(connection, event_code)
    query = """
        SELECT
            ear.id AS event_attempt_record_id,
            ear.player_id,
            ear.performance_record_id,
            ear.evaluation_status,
            ear.notes,
            pr.source_record_id,
            pr.record_type,
            pr.started_at,
            pr.ended_at,
            pr.raw_score,
            pr.final_score,
            pr.primary_time_ms,
            pr.target_tile_value,
            pr.result_state,
            pr.is_valid_source,
            pr.evidence_json,
            pr.raw_payload_json,
            pr.updated_at,
            p.display_name,
            pa.account_key AS username
        FROM event_attempt_records ear
        JOIN performance_records pr ON pr.id = ear.performance_record_id
        JOIN players p ON p.id = ear.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = ear.player_id
           AND pa.platform_id = ?
           AND pa.is_primary = 1
        WHERE ear.event_id = ?
    """
    parameters = [event["platform_id"], event["id"]]
    if username:
        query += " AND pa.account_key = ?"
        parameters.append(username)
    if not include_voided:
        query += " AND pr.is_valid_source = 1"
    query += " ORDER BY COALESCE(pr.ended_at, '') DESC, ear.id DESC"
    rows = connection.execute(query, tuple(parameters)).fetchall()
    result = []
    for row in rows:
        result.append(
            {
                **dict(row),
                "evidence": _parse_json(row["evidence_json"]),
                "raw_payload": _parse_json(row["raw_payload_json"]),
            }
        )
    return {
        "event": event,
        "rows": result,
    }


def get_score_record(connection, event_code, event_attempt_record_id):
    payload = list_event_score_records(connection, event_code, include_voided=True)
    for row in payload["rows"]:
        if row["event_attempt_record_id"] == event_attempt_record_id:
            return row
    raise ValueError("Score record not found in event: {}".format(event_attempt_record_id))


def void_score_record(
    connection,
    event_code,
    event_attempt_record_id,
    *,
    reason,
    actor_type="organizer_cli",
    actor_id="organizer_event_hub",
):
    record = get_score_record(connection, event_code, event_attempt_record_id)
    if not reason or not reason.strip():
        raise ValueError("Void reason is required")
    if not record["is_valid_source"]:
        raise ValueError("This score record is already voided")

    event = lookup_event(connection, event_code)
    updated_at = now_iso()
    after_payload = {
        "is_valid_source": 0,
        "evaluation_status": "void",
        "void_reason": reason.strip(),
        "voided_at": updated_at,
        "voided_by": {
            "actor_type": actor_type,
            "actor_id": actor_id,
        },
    }
    notes = reason.strip()
    if record["notes"]:
        notes = "{} | 作废原因: {}".format(record["notes"], reason.strip())
    else:
        notes = "作废原因: {}".format(reason.strip())

    connection.execute(
        """
        UPDATE performance_records
        SET is_valid_source = 0,
            updated_at = ?
        WHERE id = ?
        """,
        (updated_at, record["performance_record_id"]),
    )
    connection.execute(
        """
        UPDATE event_attempt_records
        SET evaluation_status = 'void',
            notes = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (notes, updated_at, event_attempt_record_id),
    )
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_type, actor_id, action_type, target_table, target_id,
            reason, before_json, after_json, created_at
        )
        VALUES (?, ?, 'void_score_record', 'performance_records', ?, ?, ?, ?, ?)
        """,
        (
            actor_type,
            actor_id,
            record["performance_record_id"],
            reason.strip(),
            json.dumps(
                {
                    "event_code": event_code,
                    "event_id": event["id"],
                    "event_attempt_record_id": record["event_attempt_record_id"],
                    "performance_record_id": record["performance_record_id"],
                    "source_record_id": record["source_record_id"],
                    "is_valid_source": record["is_valid_source"],
                    "evaluation_status": record["evaluation_status"],
                    "notes": record["notes"],
                },
                ensure_ascii=False,
            ),
            json.dumps(after_payload, ensure_ascii=False),
            updated_at,
        ),
    )
    result_count = connection.execute(
        "SELECT COUNT(*) AS count FROM event_results WHERE event_id = ?",
        (event["id"],),
    ).fetchone()["count"]
    return {
        "event_code": event_code,
        "event_attempt_record_id": record["event_attempt_record_id"],
        "performance_record_id": record["performance_record_id"],
        "source_record_id": record["source_record_id"],
        "display_name": record["display_name"],
        "username": record["username"],
        "result_count": result_count,
        "reason": reason.strip(),
    }
