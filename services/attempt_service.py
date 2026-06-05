import json
from datetime import timedelta

from services.ingest_service import ensure_player
from services.raw_score_import_service import upsert_event_attempt_record, upsert_performance_record
from services.verse_adapter import (
    fetch_recent_games,
    get_game_end_time,
    get_game_start_time,
    get_game_terminal_board_sum,
    parse_local_time,
)
from settings import LOCAL_TIMEZONE


FALLBACK_COMPLETION_GRACE = timedelta(minutes=180)


def now_local():
    from datetime import datetime

    return datetime.now(LOCAL_TIMEZONE)


def now_iso():
    return now_local().replace(tzinfo=None).isoformat(timespec="seconds")


def effective_game_start_time(game):
    start_time = get_game_start_time(game)
    if start_time is not None:
        return start_time
    return get_game_end_time(game)


def lookup_event(connection, event_code):
    row = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.platform_id,
            e.variant_id,
            e.event_type,
            e.competition_type,
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


def start_attempt_session(connection, event_code, username, display_name=None, deadline_minutes=30):
    event = lookup_event(connection, event_code)
    if event["platform_code"] != "2048verse":
        raise ValueError("First-game locking is currently only supported for 2048verse events")

    player_id, inserted = ensure_player(
        connection,
        display_name=display_name or username,
        username=username,
        platform_id=event["platform_id"],
    )
    active = connection.execute(
        """
        SELECT id, status FROM attempt_sessions
        WHERE event_id = ? AND player_id = ? AND status IN ('pending_lock', 'locked_in_progress')
        ORDER BY id DESC
        LIMIT 1
        """,
        (event["id"], player_id),
    ).fetchone()
    if active is not None:
        raise ValueError("Active attempt session already exists: {}".format(active["id"]))

    start_command_time = now_local()
    lock_deadline_time = start_command_time + timedelta(minutes=deadline_minutes)
    cursor = connection.execute(
        """
        INSERT INTO attempt_sessions (
            event_id, player_id, session_type, status, start_command_time,
            lock_deadline_time, source, metadata_json, created_at, updated_at
        )
        VALUES (?, ?, 'first_game_after_manual_start', 'pending_lock', ?, ?, 'manual_start', ?, ?, ?)
        """,
        (
            event["id"],
            player_id,
            start_command_time.replace(tzinfo=None).isoformat(timespec="seconds"),
            lock_deadline_time.replace(tzinfo=None).isoformat(timespec="seconds"),
            json.dumps(
                {
                    "username": username,
                    "lock_method": "vrs_prefix_chain",
                    "review": {"status": "pending", "reviewer": None, "note": None},
                },
                ensure_ascii=False,
            ),
            now_iso(),
            now_iso(),
        ),
    )
    return {
        "attempt_session_id": cursor.lastrowid,
        "event_code": event["event_code"],
        "username": username,
        "new_player": inserted,
        "deadline_time": lock_deadline_time.replace(tzinfo=None).isoformat(timespec="seconds"),
    }


def load_open_attempt_sessions(connection, limit=None, event_code=None):
    query = """
        SELECT
            s.id,
            s.event_id,
            s.player_id,
            s.status,
            s.start_command_time,
            s.lock_deadline_time,
            s.locked_record_id,
            s.metadata_json,
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
        WHERE s.status IN ('pending_lock', 'locked_in_progress')
    """
    parameters = []
    if event_code:
        query += " AND e.event_code = ?"
        parameters.append(event_code)
    query += " ORDER BY s.id ASC"
    if limit is not None:
        query += " LIMIT {}".format(int(limit))
    return connection.execute(query, tuple(parameters)).fetchall()


def load_session_metadata(session_row):
    raw = session_row["metadata_json"]
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def candidate_time_basis(game):
    explicit_start = get_game_start_time(game)
    if explicit_start is not None:
        return explicit_start, "started_at"
    ended_at = get_game_end_time(game)
    if ended_at is not None:
        return ended_at, "played_at_fallback"
    return None, None


def is_game_after_session_start(game, session_start):
    candidate_time, _ = candidate_time_basis(game)
    return candidate_time is not None and candidate_time >= session_start


def is_game_within_session_window(game, session_start, deadline_time):
    candidate_time, basis = candidate_time_basis(game)
    if candidate_time is None or candidate_time < session_start:
        return False, basis
    if deadline_time is None:
        return True, basis
    if basis == "started_at":
        return candidate_time <= deadline_time, basis
    return candidate_time <= deadline_time + FALLBACK_COMPLETION_GRACE, basis


def find_locked_game(games, source_record_id):
    if not source_record_id:
        return None
    for game in games:
        if str(game.get("id")) == str(source_record_id):
            return game
    return None


def expire_attempt_session(connection, session_id):
    connection.execute(
        """
        UPDATE attempt_sessions
        SET status = 'expired', updated_at = ?
        WHERE id = ?
        """,
        (now_iso(), session_id),
    )


def complete_attempt_session(connection, session_id, performance_record_id, game, matched_by=None):
    row = connection.execute("SELECT metadata_json FROM attempt_sessions WHERE id = ?", (session_id,)).fetchone()
    metadata = load_session_metadata(row) if row is not None else {}
    metadata.update(
        {
            "source_record_id": str(game.get("id")),
            "started_at": game.get("started_at") or game.get("created_at") or game.get("start_time"),
            "played_at": game.get("played_at"),
            "matched_by": matched_by,
        }
    )
    connection.execute(
        """
        UPDATE attempt_sessions
        SET status = 'completed', locked_record_id = ?, completed_time = ?, updated_at = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            performance_record_id,
            now_iso(),
            now_iso(),
            json.dumps(metadata, ensure_ascii=False),
            session_id,
        ),
    )


def lock_attempt_session_in_progress(connection, session_id, performance_record_id, game, matched_by=None):
    row = connection.execute("SELECT metadata_json FROM attempt_sessions WHERE id = ?", (session_id,)).fetchone()
    metadata = load_session_metadata(row) if row is not None else {}
    metadata.update(
        {
            "source_record_id": str(game.get("id")),
            "started_at": game.get("started_at") or game.get("created_at") or game.get("start_time"),
            "played_at": game.get("played_at"),
            "matched_by": matched_by,
        }
    )
    connection.execute(
        """
        UPDATE attempt_sessions
        SET status = 'locked_in_progress', locked_record_id = ?, updated_at = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            performance_record_id,
            now_iso(),
            json.dumps(metadata, ensure_ascii=False),
            session_id,
        ),
    )


def process_attempt_session(connection, session_row):
    if session_row["platform_code"] != "2048verse":
        return {"session_id": session_row["id"], "status": "skipped", "reason": "unsupported_platform"}
    if not session_row["account_key"]:
        return {"session_id": session_row["id"], "status": "skipped", "reason": "missing_account"}

    start_time = parse_local_time(session_row["start_command_time"])
    deadline_time = parse_local_time(session_row["lock_deadline_time"]) if session_row["lock_deadline_time"] else None
    games = fetch_recent_games(session_row["account_key"], session_row["variant_code"], start_time)
    metadata = load_session_metadata(session_row)
    locked_source_record_id = metadata.get("source_record_id")
    if session_row["status"] == "locked_in_progress" and locked_source_record_id:
        locked_game = find_locked_game(games, locked_source_record_id)
        if locked_game is not None:
            ended_at = get_game_end_time(locked_game)
            if ended_at is None:
                return {
                    "session_id": session_row["id"],
                    "status": "locked_in_progress",
                    "source_record_id": str(locked_game.get("id")),
                }
            first_game = locked_game
            matched_by = metadata.get("matched_by") or ("started_at" if get_game_start_time(locked_game) is not None else "played_at_fallback")
        else:
            first_game = None
            matched_by = metadata.get("matched_by")
    else:
        first_game = None
        matched_by = None
        for game in games:
            within_window, basis = is_game_within_session_window(game, start_time, deadline_time)
            if not within_window:
                continue
            first_game = game
            matched_by = basis
            break

    if first_game is None:
        now_time = now_local()
        if deadline_time is not None and now_time > deadline_time:
            expire_attempt_session(connection, session_row["id"])
            return {"session_id": session_row["id"], "status": "expired"}
        return {"session_id": session_row["id"], "status": "waiting"}

    event_row = {
        "id": session_row["event_id"],
        "platform_id": session_row["platform_id"],
        "variant_id": session_row["variant_id"],
        "event_code": session_row["event_code"],
    }
    score_value = int(first_game.get("score", 0))
    competition_score = score_value
    if session_row["competition_type"] == "points_series_3x4":
        board_sum = get_game_terminal_board_sum(first_game)
        if board_sum is not None:
            competition_score = board_sum
    row = {
        "username": session_row["account_key"],
        "display_name": session_row["display_name"],
        "source_record_id": str(first_game.get("id")),
        "record_type": "locked_first_game" if matched_by == "started_at" else "tracked_next_record",
        "started_at": (effective_game_start_time(first_game) or start_time).replace(tzinfo=None).isoformat(timespec="seconds"),
        "ended_at": (
            get_game_end_time(first_game).replace(tzinfo=None).isoformat(timespec="seconds")
            if get_game_end_time(first_game) is not None
            else None
        ),
        "raw_score": score_value,
        "final_score": score_value,
        "competition_score": competition_score,
        "primary_time_ms": None,
        "target_tile_value": None,
        "score_before_target": None,
        "result_state": "completed" if get_game_end_time(first_game) is not None else "in_progress",
        "evidence": None,
        "raw_payload": first_game,
    }
    saved = upsert_performance_record(connection, event_row, row)
    upsert_event_attempt_record(
        connection,
        event_id=session_row["event_id"],
        player_id=saved["player_id"],
        performance_record_id=saved["performance_record_id"],
        row=row,
    )
    if get_game_end_time(first_game) is None:
        lock_attempt_session_in_progress(
            connection,
            session_row["id"],
            saved["performance_record_id"],
            first_game,
            matched_by=matched_by,
        )
        return {
            "session_id": session_row["id"],
            "status": "locked_in_progress",
            "source_record_id": row["source_record_id"],
            "matched_by": matched_by,
        }

    complete_attempt_session(
        connection,
        session_row["id"],
        saved["performance_record_id"],
        first_game,
        matched_by=matched_by,
    )
    return {
        "session_id": session_row["id"],
        "status": "completed",
        "source_record_id": row["source_record_id"],
        "score": row["raw_score"],
        "matched_by": matched_by,
    }


def process_open_attempt_sessions(connection, limit=None, event_code=None):
    sessions = load_open_attempt_sessions(connection, limit=limit, event_code=event_code)
    results = []
    for session in sessions:
        try:
            results.append(process_attempt_session(connection, session))
        except Exception as exc:
            results.append(
                {
                    "session_id": session["id"],
                    "status": "error",
                    "reason": str(exc),
                }
            )
    return results
