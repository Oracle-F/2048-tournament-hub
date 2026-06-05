import json

from services.settlement_service import (
    is_record_within_event_window,
    load_candidate_records,
    load_event_bundle,
    metric_from_record,
    parse_local_event_time,
)


def parse_progress_time(value):
    if not value:
        return None
    return parse_local_event_time(value)


def latest_session_time(player_sessions):
    for session in player_sessions:
        for key in ("completed_time", "start_command_time", "lock_deadline_time"):
            parsed = parse_progress_time(session[key])
            if parsed is not None:
                return parsed
    return None


def required_attempt_count(rule_set, rule_config):
    aggregation_method = rule_set["aggregation_method"] or "best_single"
    if aggregation_method == "best_single":
        return 1
    if aggregation_method in {"best_of_n", "average_of_n"}:
        return int(rule_config.get("aggregation_count") or 1)
    if aggregation_method == "weighted_top_n":
        return 1
    return None


def load_registered_players(connection, event):
    rows = connection.execute(
        """
        SELECT
            r.id AS registration_id,
            r.status AS registration_status,
            r.registered_via,
            r.registered_at,
            p.id AS player_id,
            p.display_name,
            pa.account_key AS username
        FROM registrations r
        JOIN players p ON p.id = r.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = p.id
           AND pa.platform_id = ?
           AND pa.is_primary = 1
        WHERE r.event_id = ? AND r.status = 'active'
        ORDER BY LOWER(COALESCE(pa.account_key, p.display_name)) ASC, r.id ASC
        """,
        (event["platform_id"], event["id"]),
    ).fetchall()
    return {
        row["player_id"]: {
            "player_id": row["player_id"],
            "display_name": row["display_name"],
            "username": row["username"],
            "registration_id": row["registration_id"],
            "registration_status": row["registration_status"],
            "registered_via": row["registered_via"],
            "registered_at": row["registered_at"],
        }
        for row in rows
    }


def load_attempt_sessions(connection, event_id):
    rows = connection.execute(
        """
        SELECT
            s.id,
            s.player_id,
            s.status,
            s.start_command_time,
            s.lock_deadline_time,
            s.completed_time,
            s.locked_record_id
        FROM attempt_sessions s
        WHERE s.event_id = ?
        ORDER BY s.id DESC
        """,
        (event_id,),
    ).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["player_id"], []).append(row)
    return grouped


def build_player_status(event, rule_set, rule_config, player_records, player_sessions):
    aggregation_method = rule_set["aggregation_method"] or "best_single"
    needed_attempts = required_attempt_count(rule_set, rule_config)
    valid_count = len(player_records)
    latest_score = None
    best_score = None
    latest_record_time = None
    if player_records:
        ordered = sorted(
            player_records,
            key=lambda item: (item["ended_at"] or "", item["performance_record_id"]),
        )
        latest_score = metric_from_record(event["competition_type"], ordered[-1])
        latest_record_time = parse_progress_time(ordered[-1]["ended_at"]) or parse_progress_time(ordered[-1]["started_at"])
        metric_values = [metric_from_record(event["competition_type"], row) for row in player_records]
        metric_values = [value for value in metric_values if value is not None]
        if metric_values:
            if (rule_set["ranking_order"] or "desc") == "asc":
                best_score = min(metric_values)
            else:
                best_score = max(metric_values)

    latest_session_status = player_sessions[0]["status"] if player_sessions else None
    latest_session_dt = latest_session_time(player_sessions)
    has_active_session = any(session["status"] in {"pending_lock", "locked_in_progress"} for session in player_sessions)
    if has_active_session:
        status = "进行中"
    elif valid_count <= 0:
        status = "待开始"
    elif aggregation_method in {"best_single", "best_of_n", "average_of_n"} and needed_attempts is not None:
        status = "已完赛" if valid_count >= needed_attempts else "进行中"
    else:
        status = "进行中"

    lock_status_map = {
        None: "-",
        "pending_lock": "等待识别",
        "locked_in_progress": "已识别，待完赛",
        "completed": "已完成",
        "expired": "过期",
        "cancelled": "取消",
    }

    return {
        "status": status,
        "valid_record_count": valid_count,
        "required_attempts": needed_attempts,
        "latest_score": latest_score,
        "best_score": best_score,
        "latest_record_time": latest_record_time.isoformat(timespec="seconds") if latest_record_time else None,
        "latest_session_time": latest_session_dt.isoformat(timespec="seconds") if latest_session_dt else None,
        "last_activity_time": max(
            [item for item in (latest_record_time, latest_session_dt) if item is not None],
            default=None,
        ).isoformat(timespec="seconds")
        if any(item is not None for item in (latest_record_time, latest_session_dt))
        else None,
        "latest_session_status": latest_session_status,
        "lock_status_short": lock_status_map.get(latest_session_status, latest_session_status or "-"),
    }


def build_event_progress_snapshot(connection, event_code):
    event, rule_set = load_event_bundle(connection, event_code)
    rule_config = json.loads(rule_set["rule_config_json"] or "{}")
    start_dt = parse_local_event_time(event["start_time"])
    end_dt = parse_local_event_time(event["end_time"])

    registered = load_registered_players(connection, event)
    sessions_by_player = load_attempt_sessions(connection, event["id"])
    candidate_records = load_candidate_records(connection, event["id"])
    records_by_player = {}
    for record in candidate_records:
        if not is_record_within_event_window(record, start_dt, end_dt):
            continue
        records_by_player.setdefault(record["player_id"], []).append(record)
        if record["player_id"] not in registered:
            registered[record["player_id"]] = {
                "player_id": record["player_id"],
                "display_name": record["display_name"],
                "username": record["account_name"],
                "registration_id": None,
                "registration_status": "implicit",
                "registered_via": "implicit_record",
                "registered_at": None,
            }

    participants = []
    grouped = {"待开始": [], "进行中": [], "已完赛": []}
    for player_id, player_info in sorted(
        registered.items(),
        key=lambda item: ((item[1]["username"] or item[1]["display_name"] or "").lower(), item[0]),
    ):
        player_records = records_by_player.get(player_id, [])
        player_sessions = sessions_by_player.get(player_id, [])
        status_info = build_player_status(event, rule_set, rule_config, player_records, player_sessions)
        participant = {
            **player_info,
            **status_info,
        }
        participants.append(participant)
        grouped[participant["status"]].append(participant)

    return {
        "event": event,
        "rule_set": rule_set,
        "rule_config": rule_config,
        "participants": participants,
        "grouped": grouped,
        "counts": {key: len(value) for key, value in grouped.items()},
    }


def get_player_progress_detail(connection, event_code, username):
    snapshot = build_event_progress_snapshot(connection, event_code)
    player = None
    for item in snapshot["participants"]:
        if item["username"] == username:
            player = item
            break
    if player is None:
        raise ValueError("Player not found in this event: {}".format(username))

    rows = connection.execute(
        """
        SELECT
            pr.source_record_id,
            pr.started_at,
            pr.ended_at,
            pr.raw_score,
            pr.final_score,
            ear.evaluation_status,
            pr.is_valid_source
        FROM event_attempt_records ear
        JOIN performance_records pr ON pr.id = ear.performance_record_id
        JOIN player_accounts pa ON pa.player_id = ear.player_id
        JOIN events e ON e.id = ear.event_id
        WHERE e.event_code = ? AND pa.account_key = ? AND pa.platform_id = e.platform_id
        ORDER BY COALESCE(pr.started_at, ''), COALESCE(pr.ended_at, ''), pr.id
        """,
        (event_code, username),
    ).fetchall()

    sessions = connection.execute(
        """
        SELECT
            s.id,
            s.status,
            s.start_command_time,
            s.lock_deadline_time,
            s.completed_time,
            s.metadata_json
        FROM attempt_sessions s
        JOIN player_accounts pa ON pa.player_id = s.player_id
        JOIN events e ON e.id = s.event_id
        WHERE e.event_code = ? AND pa.account_key = ? AND pa.platform_id = e.platform_id
        ORDER BY s.id DESC
        """,
        (event_code, username),
    ).fetchall()

    result_row = connection.execute(
        """
        SELECT
            rank_value,
            primary_metric_value,
            secondary_metric_value,
            best_single_score
        FROM event_results er
        JOIN player_accounts pa ON pa.player_id = er.player_id
        JOIN events e ON e.id = er.event_id
        WHERE e.event_code = ? AND pa.account_key = ? AND pa.platform_id = e.platform_id
        LIMIT 1
        """,
        (event_code, username),
    ).fetchone()

    return {
        "event": snapshot["event"],
        "player": player,
        "records": rows,
        "sessions": sessions,
        "result": result_row,
    }
