import json
from datetime import datetime


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _parse_json(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def load_event_edit_bundle(connection, event_code):
    row = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            e.status,
            e.is_official,
            e.is_rated,
            e.registration_close_time,
            e.start_time,
            e.end_time,
            rb.code AS rating_bucket_code,
            rs.id AS rule_set_id,
            rs.rule_config_json
        FROM events e
        LEFT JOIN rating_buckets rb ON rb.id = e.rating_bucket_id
        LEFT JOIN event_rule_sets rs ON rs.event_id = e.id
        WHERE e.event_code = ?
        ORDER BY rs.version DESC
        LIMIT 1
        """,
        (event_code,),
    ).fetchone()
    if row is None:
        raise ValueError("Event not found: {}".format(event_code))
    return row


def update_event_basic_info(
    connection,
    event_code,
    *,
    event_name,
    registration_close_time=None,
    start_time,
    end_time,
    is_official,
    is_rated,
    actor_type="organizer_cli",
    actor_id="organizer_event_hub",
):
    bundle = load_event_edit_bundle(connection, event_code)
    if is_rated and not bundle["rating_bucket_code"]:
        raise ValueError("This event has no rating bucket, so it cannot be marked as rated")
    if start_time and end_time:
        start_dt = datetime.fromisoformat(start_time.replace(" ", "T"))
        end_dt = datetime.fromisoformat(end_time.replace(" ", "T"))
        if start_dt > end_dt:
            raise ValueError("Event start_time cannot be later than end_time")

    updated_at = now_iso()
    before_payload = {
        "event_name": bundle["event_name"],
        "registration_close_time": bundle["registration_close_time"],
        "start_time": bundle["start_time"],
        "end_time": bundle["end_time"],
        "is_official": bundle["is_official"],
        "is_rated": bundle["is_rated"],
        "status": bundle["status"],
    }
    resolved_registration_close_time = (
        registration_close_time
        if registration_close_time is not None
        else bundle["registration_close_time"]
    )
    after_payload = {
        "event_name": event_name,
        "registration_close_time": resolved_registration_close_time,
        "start_time": start_time,
        "end_time": end_time,
        "is_official": 1 if is_official else 0,
        "is_rated": 1 if is_rated else 0,
    }

    connection.execute(
        """
        UPDATE events
        SET event_name = ?,
            registration_close_time = ?,
            start_time = ?,
            end_time = ?,
            is_official = ?,
            is_rated = ?,
            updated_at = ?
        WHERE event_code = ?
        """,
        (
            event_name,
            resolved_registration_close_time,
            start_time,
            end_time,
            1 if is_official else 0,
            1 if is_rated else 0,
            updated_at,
            event_code,
        ),
    )

    if bundle["rule_set_id"] is not None:
        rule_config = _parse_json(bundle["rule_config_json"], {})
        if start_time or end_time:
            rule_config["time_window"] = {"start": start_time, "end": end_time}
        elif "time_window" in rule_config:
            del rule_config["time_window"]
        connection.execute(
            """
            UPDATE event_rule_sets
            SET rule_config_json = ?
            WHERE id = ?
            """,
            (json.dumps(rule_config, ensure_ascii=False), bundle["rule_set_id"]),
        )

    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_type, actor_id, action_type, target_table, target_id,
            reason, before_json, after_json, created_at
        )
        VALUES (?, ?, 'update_event_basic_info', 'events', ?, ?, ?, ?, ?)
        """,
        (
            actor_type,
            actor_id,
            bundle["id"],
            "manual edit from organizer_event_hub",
            json.dumps(before_payload, ensure_ascii=False),
            json.dumps(after_payload, ensure_ascii=False),
            updated_at,
        ),
    )

    result_count = connection.execute(
        "SELECT COUNT(*) AS count FROM event_results WHERE event_id = ?",
        (bundle["id"],),
    ).fetchone()["count"]
    return {
        "event_id": bundle["id"],
        "event_code": bundle["event_code"],
        "result_count": result_count,
        "before": before_payload,
        "after": after_payload,
    }
