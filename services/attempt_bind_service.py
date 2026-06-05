import json
from datetime import timedelta

from services.raw_score_import_service import upsert_event_attempt_record, upsert_performance_record
from services.verse_adapter import (
    fetch_recent_games,
    get_game_end_time,
    get_game_start_time,
    get_game_terminal_board_sum,
    parse_local_time,
)


def lookup_attempt_session(connection, event_code, username, session_id=None):
    if session_id is not None:
        row = connection.execute(
            """
            SELECT
                s.id,
                s.event_id,
                s.player_id,
                s.status,
                s.start_command_time,
                s.lock_deadline_time,
                e.event_code,
                e.platform_id,
                e.variant_id,
                p.code AS platform_code,
                v.code AS variant_code,
                pa.account_key,
                pl.display_name
            FROM attempt_sessions s
            JOIN events e ON e.id = s.event_id
            JOIN platforms p ON p.id = e.platform_id
            LEFT JOIN variants v ON v.id = e.variant_id
            JOIN players pl ON pl.id = s.player_id
            LEFT JOIN player_accounts pa
                ON pa.player_id = s.player_id
               AND pa.platform_id = e.platform_id
               AND pa.is_primary = 1
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT
                s.id,
                s.event_id,
                s.player_id,
                s.status,
                s.start_command_time,
                s.lock_deadline_time,
                e.event_code,
                e.platform_id,
                e.variant_id,
                p.code AS platform_code,
                v.code AS variant_code,
                pa.account_key,
                pl.display_name
            FROM attempt_sessions s
            JOIN events e ON e.id = s.event_id
            JOIN platforms p ON p.id = e.platform_id
            LEFT JOIN variants v ON v.id = e.variant_id
            JOIN players pl ON pl.id = s.player_id
            LEFT JOIN player_accounts pa
                ON pa.player_id = s.player_id
               AND pa.platform_id = e.platform_id
               AND pa.is_primary = 1
            WHERE e.event_code = ? AND pa.account_key = ?
              AND s.status IN ('pending_lock', 'locked_in_progress', 'completed')
            ORDER BY
                CASE
                    WHEN s.status = 'pending_lock' THEN 0
                    WHEN s.status = 'locked_in_progress' THEN 1
                    WHEN s.status = 'completed' THEN 2
                    ELSE 3
                END,
                s.id DESC
            LIMIT 1
            """,
            (event_code, username),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT
                    s.id,
                    s.event_id,
                    s.player_id,
                    s.status,
                    s.start_command_time,
                    s.lock_deadline_time,
                    e.event_code,
                    e.platform_id,
                    e.variant_id,
                    p.code AS platform_code,
                    v.code AS variant_code,
                    pa.account_key,
                    pl.display_name
                FROM attempt_sessions s
                JOIN events e ON e.id = s.event_id
                JOIN platforms p ON p.id = e.platform_id
                LEFT JOIN variants v ON v.id = e.variant_id
                JOIN players pl ON pl.id = s.player_id
                LEFT JOIN player_accounts pa
                    ON pa.player_id = s.player_id
                   AND pa.platform_id = e.platform_id
                   AND pa.is_primary = 1
                WHERE e.event_code = ? AND pa.account_key = ?
                ORDER BY s.id DESC
                LIMIT 1
                """,
                (event_code, username),
            ).fetchone()

    if row is None:
        raise ValueError("Attempt session not found")
    if row["platform_code"] != "2048verse":
        raise ValueError("Candidate listing/binding currently only supports 2048verse sessions")
    if not row["account_key"]:
        raise ValueError("Attempt session has no platform account")
    return row


def build_event_row(session_row):
    return {
        "id": session_row["event_id"],
        "platform_id": session_row["platform_id"],
        "variant_id": session_row["variant_id"],
        "event_code": session_row["event_code"],
    }


def normalize_verse_game(game, username, display_name, competition_type=None):
    started_at = get_game_start_time(game) or get_game_end_time(game)
    ended_at = get_game_end_time(game)
    score = int(game.get("score", 0))
    competition_score = score
    if competition_type == "points_series_3x4":
        board_sum = get_game_terminal_board_sum(game)
        if board_sum is not None:
            competition_score = board_sum
    return {
        "username": username,
        "display_name": display_name,
        "source_record_id": str(game.get("id")),
        "record_type": "manual_bound_verse_game",
        "started_at": started_at.replace(tzinfo=None).isoformat(timespec="seconds") if started_at else None,
        "ended_at": ended_at.replace(tzinfo=None).isoformat(timespec="seconds") if ended_at else None,
        "raw_score": score,
        "final_score": score,
        "competition_score": competition_score,
        "primary_time_ms": None,
        "target_tile_value": None,
        "score_before_target": None,
        "result_state": "completed" if ended_at is not None else "in_progress",
        "evidence": None,
        "raw_payload": game,
    }


def deep_find_value(payload, candidate_keys):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in candidate_keys and value not in (None, ""):
                return value
        for value in payload.values():
            nested = deep_find_value(value, candidate_keys)
            if nested not in (None, ""):
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = deep_find_value(item, candidate_keys)
            if nested not in (None, ""):
                return nested
    return None


def parse_optional_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_game_max_tile(game):
    return parse_optional_int(
        deep_find_value(
            game,
            {
                "max_tile",
                "maxtile",
                "max_tile_value",
                "tile",
                "tile_value",
                "top_tile",
                "largest_tile",
                "max",
            },
        )
    )


def get_game_user_id(game):
    value = deep_find_value(
        game,
        {
            "user_id",
            "userid",
            "player_id",
            "playerid",
            "account_id",
            "accountid",
        },
    )
    if value in (None, ""):
        return None
    return str(value)


def build_match_candidate(game, target_end_time):
    ended_at = get_game_end_time(game)
    started_at = get_game_start_time(game)
    delta_seconds = None
    if ended_at is not None and target_end_time is not None:
        delta_seconds = int(abs((ended_at - target_end_time).total_seconds()))
    payload_keys = sorted(str(key) for key in game.keys())
    return {
        "source_record_id": str(game.get("id")),
        "score": int(game.get("score", 0)),
        "started_at": started_at.replace(tzinfo=None).isoformat(timespec="seconds") if started_at else None,
        "ended_at": ended_at.replace(tzinfo=None).isoformat(timespec="seconds") if ended_at else None,
        "max_tile_value": get_game_max_tile(game),
        "user_id": get_game_user_id(game),
        "end_time_delta_seconds": delta_seconds,
        "payload_keys": payload_keys,
    }


def compute_exact_minute_match(candidate_time, target_time):
    if candidate_time is None or target_time is None:
        return False
    return candidate_time.strftime("%Y-%m-%d %H:%M") == target_time.strftime("%Y-%m-%d %H:%M")


def annotate_candidate(candidate, session_row, *, query_score=None, query_end_time=None, query_max_tile_value=None, query_user_id=None):
    session_start = parse_local_time(session_row["start_command_time"])
    session_deadline = parse_local_time(session_row["lock_deadline_time"]) if session_row["lock_deadline_time"] else None
    candidate_start = parse_local_time(candidate["started_at"]) if candidate.get("started_at") else None
    candidate_end = parse_local_time(candidate["ended_at"]) if candidate.get("ended_at") else None

    within_window = candidate_start is not None and candidate_start >= session_start
    if within_window and session_deadline is not None and candidate_start > session_deadline:
        within_window = False

    start_offset_seconds = None
    if candidate_start is not None:
        start_offset_seconds = int((candidate_start - session_start).total_seconds())

    deadline_overrun_seconds = None
    if session_deadline is not None and candidate_start is not None and candidate_start > session_deadline:
        deadline_overrun_seconds = int((candidate_start - session_deadline).total_seconds())

    score_match = query_score is None or int(candidate["score"]) == int(query_score)
    exact_second_match = (
        query_end_time is not None and candidate_end is not None and candidate_end == query_end_time
    )
    exact_minute_match = compute_exact_minute_match(candidate_end, query_end_time)
    max_tile_match = None
    if query_max_tile_value is not None:
        if candidate.get("max_tile_value") is None:
            max_tile_match = "missing"
        elif int(candidate["max_tile_value"]) == int(query_max_tile_value):
            max_tile_match = "exact"
        else:
            max_tile_match = "mismatch"
    user_id_match = None
    if query_user_id:
        if not candidate.get("user_id"):
            user_id_match = "missing"
        elif str(candidate["user_id"]) == str(query_user_id):
            user_id_match = "exact"
        else:
            user_id_match = "mismatch"

    recommendation_score = 0
    if within_window:
        recommendation_score += 100
    if score_match:
        recommendation_score += 80
    if exact_second_match:
        recommendation_score += 60
    elif exact_minute_match:
        recommendation_score += 45
    if max_tile_match == "exact":
        recommendation_score += 25
    if user_id_match == "exact":
        recommendation_score += 20
    if candidate.get("end_time_delta_seconds") is not None:
        recommendation_score -= min(candidate["end_time_delta_seconds"], 600)

    match_tags = []
    if within_window:
        match_tags.append("in_window")
    else:
        match_tags.append("out_of_window")
    if exact_second_match:
        match_tags.append("exact_second")
    elif exact_minute_match:
        match_tags.append("exact_minute")
    if max_tile_match == "exact":
        match_tags.append("max_tile_exact")
    elif max_tile_match == "missing":
        match_tags.append("max_tile_missing")
    if user_id_match == "exact":
        match_tags.append("user_id_exact")
    elif user_id_match == "missing":
        match_tags.append("user_id_missing")

    candidate["within_lock_window"] = within_window
    candidate["start_offset_seconds"] = start_offset_seconds
    candidate["deadline_overrun_seconds"] = deadline_overrun_seconds
    candidate["score_match"] = score_match
    candidate["exact_second_match"] = exact_second_match
    candidate["exact_minute_match"] = exact_minute_match
    candidate["max_tile_match"] = max_tile_match
    candidate["user_id_match"] = user_id_match
    candidate["recommendation_score"] = recommendation_score
    candidate["match_tags"] = match_tags
    return candidate


def list_attempt_candidates(connection, event_code, username, session_id=None, max_pages=10):
    session_row = lookup_attempt_session(connection, event_code, username, session_id=session_id)
    start_time = parse_local_time(session_row["start_command_time"])
    deadline_time = parse_local_time(session_row["lock_deadline_time"]) if session_row["lock_deadline_time"] else None
    games = fetch_recent_games(session_row["account_key"], session_row["variant_code"], start_time, max_pages=max_pages)

    candidates = []
    for game in games:
        started_at = get_game_start_time(game) or get_game_end_time(game)
        ended_at = get_game_end_time(game)
        if started_at is None or started_at < start_time:
            continue
        if deadline_time is not None and started_at > deadline_time:
            continue
        candidates.append(
            annotate_candidate(
                {
                "source_record_id": str(game.get("id")),
                "score": int(game.get("score", 0)),
                "started_at": started_at.replace(tzinfo=None).isoformat(timespec="seconds") if started_at else None,
                "ended_at": ended_at.replace(tzinfo=None).isoformat(timespec="seconds") if ended_at else None,
                "max_tile_value": get_game_max_tile(game),
                "user_id": get_game_user_id(game),
                },
                session_row,
            )
        )

    return {
        "session": session_row,
        "candidates": candidates,
    }


def bind_attempt_record(connection, event_code, username, source_record_id, session_id=None, max_pages=10):
    session_row = lookup_attempt_session(connection, event_code, username, session_id=session_id)
    start_time = parse_local_time(session_row["start_command_time"])
    games = fetch_recent_games(session_row["account_key"], session_row["variant_code"], start_time, max_pages=max_pages)
    target_game = None
    for game in games:
        if str(game.get("id")) == str(source_record_id):
            target_game = game
            break
    if target_game is None:
        raise ValueError("Verse game not found in current candidate window: {}".format(source_record_id))

    event_row = build_event_row(session_row)
    row = normalize_verse_game(
        target_game,
        session_row["account_key"],
        session_row["display_name"],
        session_row.get("competition_type"),
    )
    saved = upsert_performance_record(connection, event_row, row)
    upsert_event_attempt_record(
        connection,
        event_id=session_row["event_id"],
        player_id=saved["player_id"],
        performance_record_id=saved["performance_record_id"],
        row=row,
    )
    connection.execute(
        """
        UPDATE attempt_sessions
        SET status = 'completed',
            locked_record_id = ?,
            completed_time = ?,
            updated_at = ?,
            metadata_json = ?
        WHERE id = ?
        """,
        (
            saved["performance_record_id"],
            row["ended_at"],
            row["ended_at"] or row["started_at"],
            json.dumps(
                {
                    "bound_manually": True,
                    "source_record_id": row["source_record_id"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                },
                ensure_ascii=False,
            ),
            session_row["id"],
        ),
    )
    return {
        "session_id": session_row["id"],
        "event_code": session_row["event_code"],
        "username": session_row["account_key"],
        "source_record_id": row["source_record_id"],
        "score": row["raw_score"],
    }


def find_attempt_candidates_by_result(
    connection,
    event_code,
    username,
    *,
    score,
    ended_at,
    max_tile_value=None,
    user_id=None,
    tolerance_minutes=2,
    session_id=None,
    max_pages=30,
):
    session_row = lookup_attempt_session(connection, event_code, username, session_id=session_id)
    start_time = parse_local_time(session_row["start_command_time"])
    target_end_time = parse_local_time(ended_at)
    tolerance = timedelta(minutes=tolerance_minutes)
    games = fetch_recent_games(
        session_row["account_key"],
        session_row["variant_code"],
        start_time,
        max_pages=max_pages,
        stop_when_before=False,
    )

    candidates = []
    for game in games:
        game_started_at = get_game_start_time(game) or get_game_end_time(game)
        game_ended_at = get_game_end_time(game)
        game_score = int(game.get("score", 0))
        if game_started_at is None or game_started_at < start_time:
            continue
        if game_ended_at is None:
            continue
        if game_score != int(score):
            continue
        if abs(game_ended_at - target_end_time) > tolerance:
            continue

        candidate_max_tile = get_game_max_tile(game)
        if max_tile_value is not None and candidate_max_tile is not None and candidate_max_tile != int(max_tile_value):
            continue

        candidate_user_id = get_game_user_id(game)
        if user_id and candidate_user_id and str(candidate_user_id) != str(user_id):
            continue

        candidates.append(
            annotate_candidate(
                build_match_candidate(game, target_end_time),
                session_row,
                query_score=score,
                query_end_time=target_end_time,
                query_max_tile_value=max_tile_value,
                query_user_id=user_id,
            )
        )

    candidates.sort(
        key=lambda item: (
            -item["recommendation_score"],
            item["end_time_delta_seconds"] if item["end_time_delta_seconds"] is not None else 10**9,
            item["started_at"] or "",
            item["source_record_id"] or "",
        )
    )
    return {
        "session": session_row,
        "query": {
            "score": int(score),
            "ended_at": target_end_time.replace(tzinfo=None).isoformat(timespec="seconds"),
            "max_tile_value": max_tile_value,
            "user_id": str(user_id) if user_id not in (None, "") else None,
            "tolerance_minutes": tolerance_minutes,
        },
        "candidates": candidates,
    }
