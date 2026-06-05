import json
import os
import hashlib
import re
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*_args, **_kwargs):
        return False

from services.competition_mode_service import event_uses_locking
from services.raw_score_import_service import add_manual_score
from services.replay_lock_service import (
    attach_final_replay,
    run_replay_prefix_check,
    set_replay_review_status,
    verify_replay_prefix,
)
from services.verse_adapter import parse_local_time
from settings import LOCAL_TIMEZONE


ROOT_DIR = Path(__file__).resolve().parents[1]
DISCORD_DATE_FORMATS = (
    "%a %b %d %Y %I:%M:%S %p",
    "%a %b %d %Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)
REPLAY_DUPLICATE_SUFFIX_RE = re.compile(r"\((\d+)\)$")
REPLAY_SCORE_RE = re.compile(r"_(\d+)(?:\(\d+\))?\.vrs$", re.IGNORECASE)


def _now_iso_local():
    return datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None).isoformat(timespec="seconds")


def _load_env_file_fallback(path):
    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()


def _parse_json(value):
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sha256_of_file(file_path):
    digest = hashlib.sha256()
    path = Path(file_path).expanduser().resolve()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _parse_positive_int_env(key, default_value):
    raw = str(os.getenv(key, "")).strip()
    if not raw:
        return default_value
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return value if value > 0 else default_value


def _parse_optional_local_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return parse_local_time(text)
    except Exception:
        return None


def _normalize_board_size(value):
    if not value:
        return ""
    text = str(value).strip().lower().replace(" ", "")
    return text.replace("×", "x")


def _normalize_replay_filename(value):
    name = str(value or "").strip()
    if not name:
        return ""
    path = Path(name)
    stem = path.stem
    stem = REPLAY_DUPLICATE_SUFFIX_RE.sub("", stem).rstrip()
    suffix = path.suffix or ""
    return (stem + suffix).lower()


def _extract_replay_score_from_filename(value):
    text = str(value or "").strip()
    if not text:
        return None
    match = REPLAY_SCORE_RE.search(Path(text).name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _parse_discord_date_text(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in DISCORD_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=LOCAL_TIMEZONE)
        except ValueError:
            continue
    return None


def _load_event_for_refresh(connection, event_code):
    row = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            e.start_time,
            e.end_time,
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


def _load_sync_runtime_args():
    load_dotenv(ROOT_DIR / ".env")
    _load_env_file_fallback(ROOT_DIR / ".env")
    load_dotenv(ROOT_DIR / ".env.bot.secret", override=True)
    _load_env_file_fallback(ROOT_DIR / ".env.bot.secret")
    token = str(os.getenv("DISCORD_BOT_TOKEN", "")).strip()
    max_pages = _parse_positive_int_env("DISCORD_LOCK_SYNC_MAX_PAGES", 3)
    limit = _parse_positive_int_env("DISCORD_LOCK_SYNC_LIMIT", 50)
    from scripts.sync_discord_game_review import parse_channel_ids

    channel_ids = parse_channel_ids(None)
    return {
        "token": token,
        "channel_ids": channel_ids,
        "max_pages": max_pages,
        "limit": max(1, min(100, limit)),
    }


def _prioritize_channel_ids(channel_ids, replay_score=None):
    ordered = [str(item).strip() for item in (channel_ids or []) if str(item).strip()]
    if len(ordered) < 4 or replay_score is None:
        return ordered

    high_channel = ordered[0]
    mid_channel = ordered[1]
    low_channel = ordered[2]
    suspicious_channel = ordered[3]

    if replay_score >= 190000:
        preferred = [high_channel, suspicious_channel, mid_channel, low_channel]
    elif replay_score >= 40000:
        preferred = [mid_channel, suspicious_channel, high_channel, low_channel]
    else:
        preferred = [low_channel, suspicious_channel, mid_channel, high_channel]

    deduped = []
    seen = set()
    for channel_id in preferred + ordered:
        if channel_id in seen:
            continue
        seen.add(channel_id)
        deduped.append(channel_id)
    return deduped


def _sync_discord_review_messages(connection, runtime, *, download_only_usernames=None):
    token = runtime["token"]
    channel_ids = runtime["channel_ids"]
    if not token or not channel_ids:
        return {"enabled": False, "reason": "missing_token_or_channel_ids", "channels": []}

    from scripts.sync_discord_game_review import ensure_tables, sync_channel

    ensure_tables(connection)
    summaries = []
    for channel_id in channel_ids:
        summary = sync_channel(
            connection,
            token=token,
            channel_id=channel_id,
            max_pages=runtime["max_pages"],
            limit=runtime["limit"],
            full_scan=False,
            download_files=True,
            dry_run=False,
            download_only_usernames=download_only_usernames,
        )
        summaries.append(summary)
    diagnostics = _build_sync_diagnostics(connection, channel_ids)
    return {"enabled": True, "reason": "ok", "channels": summaries, "diagnostics": diagnostics}


def sync_locking_scores_from_discord(connection, *, download_only_usernames=None):
    runtime = _load_sync_runtime_args()
    return _sync_discord_review_messages(connection, runtime, download_only_usernames=download_only_usernames)


def targeted_sync_locking_scores_from_discord(
    connection,
    *,
    download_only_usernames=None,
    replay_score=None,
    deep=False,
):
    runtime = _load_sync_runtime_args()
    runtime = dict(runtime)
    runtime["channel_ids"] = _prioritize_channel_ids(runtime["channel_ids"], replay_score=replay_score)
    if deep:
        runtime["max_pages"] = _parse_positive_int_env("DISCORD_LOCK_DEEP_SYNC_MAX_PAGES", max(runtime["max_pages"], 12))
        runtime["limit"] = _parse_positive_int_env("DISCORD_LOCK_DEEP_SYNC_LIMIT", runtime["limit"])

    token = runtime["token"]
    channel_ids = runtime["channel_ids"]
    if not token or not channel_ids:
        return {"enabled": False, "reason": "missing_token_or_channel_ids", "channels": []}

    from scripts.sync_discord_game_review import ensure_tables, sync_channel

    ensure_tables(connection)
    summaries = []
    for channel_id in channel_ids:
        summary = sync_channel(
            connection,
            token=token,
            channel_id=channel_id,
            max_pages=runtime["max_pages"],
            limit=runtime["limit"],
            full_scan=bool(deep),
            download_files=True,
            dry_run=False,
            download_only_usernames=download_only_usernames,
        )
        summaries.append(summary)
    diagnostics = _build_sync_diagnostics(connection, channel_ids)
    return {
        "enabled": True,
        "reason": "ok",
        "mode": "deep_full_scan" if deep else "targeted_shallow_scan",
        "channels": summaries,
        "diagnostics": diagnostics,
        "ordered_channel_ids": channel_ids,
    }


def _build_sync_diagnostics(connection, channel_ids):
    channels = []
    total_recent = 0
    total_normal = 0
    total_attach_msgs = 0
    total_redacted = 0
    for channel_id in channel_ids:
        row = connection.execute(
            """
            WITH recent AS (
                SELECT message_kind, metadata_json, content_text
                FROM discord_review_messages
                WHERE channel_id = ?
                ORDER BY id DESC
                LIMIT 200
            )
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN message_kind = 'normal_score' THEN 1 ELSE 0 END) AS normal_score_count,
                SUM(CASE WHEN json_extract(metadata_json, '$.attachments_count') > 0 THEN 1 ELSE 0 END) AS attachment_msg_count,
                SUM(
                    CASE
                        WHEN json_extract(metadata_json, '$.redacted_like') THEN 1
                        WHEN message_kind = 'unknown'
                             AND COALESCE(length(content_text), 0) = 0
                             AND COALESCE(json_extract(metadata_json, '$.attachments_count'), 0) = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS redacted_like_count
            FROM recent
            """,
            (channel_id,),
        ).fetchone()
        total = int(row["total"] or 0)
        normal_score_count = int(row["normal_score_count"] or 0)
        attachment_msg_count = int(row["attachment_msg_count"] or 0)
        redacted_like_count = int(row["redacted_like_count"] or 0)
        total_recent += total
        total_normal += normal_score_count
        total_attach_msgs += attachment_msg_count
        total_redacted += redacted_like_count
        channels.append(
            {
                "channel_id": str(channel_id),
                "recent_total": total,
                "normal_score_count": normal_score_count,
                "attachment_msg_count": attachment_msg_count,
                "redacted_like_count": redacted_like_count,
            }
        )
    likely_redacted = (
        total_recent >= 20
        and total_normal == 0
        and total_attach_msgs == 0
        and total_redacted >= int(total_recent * 0.8)
    )
    return {
        "likely_redacted": likely_redacted,
        "recent_total": total_recent,
        "normal_score_count": total_normal,
        "attachment_msg_count": total_attach_msgs,
        "redacted_like_count": total_redacted,
        "channels": channels,
    }


def _load_candidate_attachments(connection, username=None, *, limit=600):
    clauses = [
        "m.message_kind = 'normal_score'",
        "a.local_path IS NOT NULL",
    ]
    params = []
    if username:
        clauses.append("lower(COALESCE(m.parsed_username, '')) = lower(?)")
        params.append(username)
    params.append(int(limit))
    rows = connection.execute(
        """
        SELECT
            m.channel_id,
            m.message_id,
            m.created_at_discord,
            m.parsed_date_text,
            m.parsed_score,
            m.parsed_username,
            m.parsed_board_size,
            m.message_kind,
            a.attachment_id,
            a.filename,
            a.local_path
        FROM discord_review_messages m
        JOIN discord_review_attachments a
          ON a.channel_id = m.channel_id
         AND a.message_id = m.message_id
        WHERE {}
        ORDER BY m.created_at_discord DESC, m.message_id DESC
        LIMIT ?
        """.format(" AND ".join(clauses)),
        tuple(params),
    ).fetchall()
    candidates = []
    for row in rows:
        filename = str(row["filename"] or "")
        if not filename.lower().endswith(".vrs"):
            continue
        local_path = Path(str(row["local_path"])).expanduser().resolve()
        if not local_path.exists() or not local_path.is_file():
            continue
        candidates.append(row)
    return candidates


def _load_exact_hash_candidates(connection, sha256_hex):
    return connection.execute(
        """
        SELECT
            m.channel_id,
            m.message_id,
            m.created_at_discord,
            m.parsed_date_text,
            m.parsed_score,
            m.parsed_username,
            m.parsed_board_size,
            m.message_kind,
            a.attachment_id,
            a.filename,
            a.local_path,
            a.sha256
        FROM discord_review_messages m
        JOIN discord_review_attachments a
          ON a.channel_id = m.channel_id
         AND a.message_id = m.message_id
        WHERE m.message_kind = 'normal_score'
          AND a.local_path IS NOT NULL
          AND a.sha256 = ?
        ORDER BY m.created_at_discord DESC, m.message_id DESC
        """,
        (sha256_hex,),
    ).fetchall()


def _load_filename_candidates(connection, username, filename):
    return connection.execute(
        """
        SELECT
            m.channel_id,
            m.message_id,
            m.created_at_discord,
            m.parsed_date_text,
            m.parsed_score,
            m.parsed_username,
            m.parsed_board_size,
            m.message_kind,
            a.attachment_id,
            a.filename,
            a.local_path,
            a.sha256
        FROM discord_review_messages m
        JOIN discord_review_attachments a
          ON a.channel_id = m.channel_id
         AND a.message_id = m.message_id
        WHERE m.message_kind = 'normal_score'
          AND lower(COALESCE(a.filename, '')) = lower(?)
        ORDER BY m.created_at_discord DESC, m.message_id DESC
        """,
        (filename,),
    ).fetchall()


def _candidate_matches_username(row, username):
    expected = str(username or "").strip().lower()
    parsed_username = str(row["parsed_username"] or "").strip().lower()
    if parsed_username:
        return parsed_username == expected, parsed_username
    filename = str(row["filename"] or "").strip().lower()
    if filename.startswith(expected + "_"):
        return True, ""
    return False, ""


def _filter_candidates_for_event(rows, event_row, username):
    variant_tag = _normalize_board_size(event_row["variant_code"])
    event_start_dt = parse_local_time(event_row["start_time"]) if event_row["start_time"] else None
    event_end_dt = parse_local_time(event_row["end_time"]) if event_row["end_time"] else None
    filtered = []
    mismatch_usernames = []
    for row in rows:
        matched_username, mismatch_username = _candidate_matches_username(row, username)
        if not matched_username:
            if mismatch_username:
                mismatch_usernames.append(mismatch_username)
            continue
        board_tag = _normalize_board_size(row["parsed_board_size"])
        if board_tag and variant_tag and board_tag != variant_tag:
            continue
        ended_at_dt = _resolve_candidate_ended_at(row) or _resolve_candidate_posted_at(row)
        if not _in_event_window(ended_at_dt, event_start_dt, event_end_dt):
            continue
        filtered.append(row)
    return filtered, sorted({item for item in mismatch_usernames if item})


def _load_open_lock_sessions(connection, event_code):
    return connection.execute(
        """
        SELECT
            s.id,
            s.event_id,
            s.player_id,
            s.status,
            s.start_command_time,
            s.lock_deadline_time,
            s.metadata_json,
            pa.account_key,
            pl.display_name
        FROM attempt_sessions s
        JOIN events e ON e.id = s.event_id
        JOIN players pl ON pl.id = s.player_id
        LEFT JOIN player_accounts pa
          ON pa.player_id = s.player_id
         AND pa.platform_id = e.platform_id
         AND pa.is_primary = 1
        WHERE e.event_code = ?
          AND s.status IN ('pending_lock', 'locked_in_progress')
        ORDER BY s.id ASC
        """,
        (event_code,),
    ).fetchall()


def _resolve_candidate_ended_at(row):
    date_text = row["parsed_date_text"]
    parsed = _parse_discord_date_text(date_text)
    posted = _resolve_candidate_posted_at(row)
    if parsed is None:
        return posted
    if posted is None:
        return parsed
    # Some review bots print Date in server-local/UTC without timezone marker.
    # If parsed date and Discord post timestamp differ by too much, trust posted_at.
    skew_limit_minutes = _parse_positive_int_env("DISCORD_DATE_POST_MAX_SKEW_MINUTES", 240)
    try:
        skew_seconds = abs(int((posted - parsed).total_seconds()))
    except Exception:
        return parsed
    if skew_seconds > max(60, skew_limit_minutes * 60):
        return posted
    return parsed


def _resolve_candidate_posted_at(row):
    created = row["created_at_discord"]
    if not created:
        return None
    try:
        return parse_local_time(created)
    except Exception:
        return None


def _in_event_window(ended_at_dt, event_start_dt, event_end_dt):
    if ended_at_dt is None:
        return False
    if event_start_dt is not None and ended_at_dt < event_start_dt:
        return False
    if event_end_dt is not None and ended_at_dt > event_end_dt:
        return False
    return True


def _session_needs_auto_bind(session_row):
    metadata = _parse_json(session_row["metadata_json"])
    early = metadata.get("early_replay") if isinstance(metadata.get("early_replay"), dict) else None
    final = metadata.get("final_replay") if isinstance(metadata.get("final_replay"), dict) else None
    auto_match = metadata.get("discord_auto_match") if isinstance(metadata.get("discord_auto_match"), dict) else None
    if not early:
        return False
    if final:
        return False
    if auto_match and auto_match.get("status") == "manual_required":
        return False
    if not early.get("path"):
        return False
    return True


def _session_update_manual_needed(connection, session_id, metadata, reason, details):
    metadata = dict(metadata)
    metadata["discord_auto_match"] = {
        "status": "manual_required",
        "reason": reason,
        "details": details,
        "updated_at": _now_iso_local(),
    }
    connection.execute(
        """
        UPDATE attempt_sessions
        SET metadata_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(metadata, ensure_ascii=False), _now_iso_local(), session_id),
    )


def _session_update_completed(connection, session_row, metadata, *, source_record_id, performance_record_id, ended_at, match_info):
    metadata = dict(metadata)
    metadata["source_record_id"] = source_record_id
    metadata["matched_by"] = "discord_replay_prefix"
    metadata["discord_auto_match"] = {
        "status": "matched",
        "updated_at": _now_iso_local(),
        **match_info,
    }
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
            performance_record_id,
            ended_at,
            _now_iso_local(),
            json.dumps(metadata, ensure_ascii=False),
            session_row["id"],
        ),
    )


def _auto_bind_one_session(connection, event_row, session_row, candidate_rows):
    metadata = _parse_json(session_row["metadata_json"])
    early = metadata.get("early_replay") if isinstance(metadata.get("early_replay"), dict) else None
    early_path = Path(str(early.get("path"))).expanduser().resolve()
    if not early_path.exists() or not early_path.is_file():
        return {"status": "skipped", "reason": "early_replay_missing"}

    event_start_dt = parse_local_time(event_row["start_time"]) if event_row["start_time"] else None
    event_end_dt = parse_local_time(event_row["end_time"]) if event_row["end_time"] else None
    session_start_dt = _parse_optional_local_time(session_row["start_command_time"])
    early_submitted_dt = _parse_optional_local_time(early.get("submitted_at"))
    normal_post_max_delay_minutes = _parse_positive_int_env("DISCORD_NORMAL_POST_MAX_DELAY_MINUTES", 180)
    variant_tag = _normalize_board_size(event_row["variant_code"])

    matched = []
    for item in candidate_rows:
        if str(item["message_kind"] or "") != "normal_score":
            continue
        score = item["parsed_score"]
        if score is None:
            continue
        board_tag = _normalize_board_size(item["parsed_board_size"])
        if board_tag and variant_tag and board_tag != variant_tag:
            continue
        ended_at_dt = _resolve_candidate_ended_at(item)
        posted_at_dt = _resolve_candidate_posted_at(item)
        if not _in_event_window(ended_at_dt, event_start_dt, event_end_dt):
            continue
        if session_start_dt is not None and ended_at_dt is not None and ended_at_dt < session_start_dt:
            continue
        if early_submitted_dt is not None and posted_at_dt is not None and posted_at_dt < early_submitted_dt:
            continue
        if posted_at_dt is not None and ended_at_dt is not None:
            if posted_at_dt < ended_at_dt:
                continue
            post_delay_seconds = int((posted_at_dt - ended_at_dt).total_seconds())
            if post_delay_seconds > normal_post_max_delay_minutes * 60:
                continue
        check = verify_replay_prefix(str(early_path), str(item["local_path"]))
        if check["status"] != "passed":
            continue
        matched.append((item, ended_at_dt))

    if not matched:
        return {"status": "no_match"}
    if len(matched) > 1:
        _session_update_manual_needed(
            connection,
            session_row["id"],
            metadata,
            reason="multiple_prefix_matches",
            details={
                "count": len(matched),
                "message_ids": [str(x[0]["message_id"]) for x in matched[:10]],
            },
        )
        return {"status": "manual_required", "reason": "multiple_matches", "count": len(matched)}

    hit, ended_at_dt = matched[0]
    ended_at_text = ended_at_dt.replace(tzinfo=None).isoformat(timespec="seconds")
    source_record_id = "discord_{}_{}_{}".format(event_row["event_code"], hit["channel_id"], hit["message_id"])

    attach_final_replay(
        connection,
        event_row["event_code"],
        session_row["account_key"],
        str(hit["local_path"]),
        session_id=session_row["id"],
    )
    check_result = run_replay_prefix_check(
        connection,
        event_row["event_code"],
        session_row["account_key"],
        session_id=session_row["id"],
    )
    if check_result.get("status") != "passed":
        _session_update_manual_needed(
            connection,
            session_row["id"],
            _parse_json(
                connection.execute("SELECT metadata_json FROM attempt_sessions WHERE id = ?", (session_row["id"],)).fetchone()[
                    "metadata_json"
                ]
            ),
            reason="post_attach_prefix_failed",
            details={"reason": check_result.get("reason")},
        )
        return {"status": "manual_required", "reason": "post_attach_prefix_failed"}

    set_replay_review_status(
        connection,
        event_row["event_code"],
        session_row["account_key"],
        approved=True,
        reviewer="auto:discord_sync",
        note="Auto approved via Discord replay-prefix match",
        session_id=session_row["id"],
    )

    score_value = int(hit["parsed_score"])
    added = add_manual_score(
        connection,
        event_row["event_code"],
        username=session_row["account_key"],
        display_name=session_row["display_name"],
        source_record_id=source_record_id,
        started_at=None,
        ended_at=ended_at_text,
        raw_score=score_value,
        final_score=score_value,
        competition_score=score_value,
        primary_time_ms=None,
        target_tile_value=None,
        score_before_target=None,
        evidence_note="discord_auto_bind channel={} message={} attachment={}".format(
            hit["channel_id"],
            hit["message_id"],
            hit["attachment_id"],
        ),
    )

    latest_metadata = _parse_json(
        connection.execute("SELECT metadata_json FROM attempt_sessions WHERE id = ?", (session_row["id"],)).fetchone()[
            "metadata_json"
        ]
    )
    _session_update_completed(
        connection,
        session_row,
        latest_metadata,
        source_record_id=source_record_id,
        performance_record_id=added["performance_record_id"],
        ended_at=ended_at_text,
        match_info={
            "channel_id": str(hit["channel_id"]),
            "message_id": str(hit["message_id"]),
            "attachment_id": str(hit["attachment_id"]),
            "filename": str(hit["filename"] or ""),
            "score": score_value,
        },
    )
    return {
        "status": "matched",
        "session_id": session_row["id"],
        "source_record_id": source_record_id,
        "score": score_value,
        "message_id": str(hit["message_id"]),
    }


def refresh_locking_event_scores_from_discord(connection, event_code):
    event_row = _load_event_for_refresh(connection, event_code)
    if not event_uses_locking(event_row["competition_type"], event_row["platform_code"], event_row["variant_code"]):
        return {"event_code": event_code, "enabled": False, "reason": "event_not_locking"}
    if event_row["platform_code"] != "2048verse":
        return {"event_code": event_code, "enabled": False, "reason": "unsupported_platform"}

    sync_summary = sync_locking_scores_from_discord(connection)
    if not sync_summary["enabled"]:
        return {
            "event_code": event_code,
            "enabled": False,
            "reason": sync_summary["reason"],
            "sync": sync_summary,
        }

    sessions = _load_open_lock_sessions(connection, event_code)
    return _refresh_locking_sessions(connection, event_row, sessions, sync_summary)


def refresh_locking_player_scores_from_discord(connection, event_code, *, player_id, sync_summary=None):
    event_row = _load_event_for_refresh(connection, event_code)
    if not event_uses_locking(event_row["competition_type"], event_row["platform_code"], event_row["variant_code"]):
        return {"event_code": event_code, "enabled": False, "reason": "event_not_locking"}
    if event_row["platform_code"] != "2048verse":
        return {"event_code": event_code, "enabled": False, "reason": "unsupported_platform"}

    if sync_summary is None:
        sync_summary = sync_locking_scores_from_discord(connection)
    if not sync_summary["enabled"]:
        return {
            "event_code": event_code,
            "enabled": False,
            "reason": sync_summary["reason"],
            "sync": sync_summary,
            "player_id": player_id,
        }

    sessions = [session for session in _load_open_lock_sessions(connection, event_code) if session["player_id"] == player_id]
    outcome = _refresh_locking_sessions(connection, event_row, sessions, sync_summary)
    outcome["player_id"] = player_id
    return outcome


def record_score_from_discord_final_replay(connection, event_code, *, username, replay_file_path):
    event_row = _load_event_for_refresh(connection, event_code)
    if event_row["platform_code"] != "2048verse":
        return {"event_code": event_code, "enabled": False, "reason": "unsupported_platform"}

    source = Path(replay_file_path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        return {
            "event_code": event_code,
            "enabled": False,
            "reason": "replay_file_missing",
            "replay_file_path": str(source),
        }

    replay_score = _extract_replay_score_from_filename(source.name)
    sync_summary = targeted_sync_locking_scores_from_discord(
        connection,
        download_only_usernames=[username],
        replay_score=replay_score,
        deep=False,
    )
    if not sync_summary["enabled"]:
        return {
            "event_code": event_code,
            "enabled": False,
            "reason": sync_summary["reason"],
            "sync": sync_summary,
            "username": username,
        }

    replay_filename = source.name
    normalized_replay_filename = _normalize_replay_filename(replay_filename)
    replay_sha256 = _sha256_of_file(source)
    def _attempt_match(current_sync_summary):
        filename_candidates = _load_filename_candidates(connection, username, replay_filename)
        if normalized_replay_filename and normalized_replay_filename != replay_filename.lower():
            filename_candidates.extend(_load_filename_candidates(connection, username, normalized_replay_filename))
            deduped = {}
            for row in filename_candidates:
                deduped[(str(row["channel_id"]), str(row["message_id"]), str(row["attachment_id"]))] = row
            filename_candidates = list(deduped.values())
        filename_matches, _filename_mismatches = _filter_candidates_for_event(filename_candidates, event_row, username)
        if len(filename_matches) == 1:
            return {"hit": filename_matches[0], "match_reason": "filename_exact", "sync": current_sync_summary}
        if len(filename_matches) > 1:
            return {
                "result": {
                    "event_code": event_code,
                    "enabled": True,
                    "reason": "multiple_filename_matches",
                    "sync": current_sync_summary,
                    "username": username,
                    "filename": replay_filename,
                    "match_count": len(filename_matches),
                    "message_ids": [str(item["message_id"]) for item in filename_matches[:10]],
                }
            }
        candidates = _load_exact_hash_candidates(connection, replay_sha256)
        if not candidates:
            return None
        hash_matches, mismatch_usernames = _filter_candidates_for_event(candidates, event_row, username)
        if not hash_matches:
            return {
                "result": {
                    "event_code": event_code,
                    "enabled": True,
                    "reason": "hash_matched_but_username_mismatch",
                    "sync": current_sync_summary,
                    "username": username,
                    "sha256": replay_sha256,
                    "filename": replay_filename,
                    "matched_usernames": mismatch_usernames,
                    "match_count": len(candidates),
                }
            }
        if len(hash_matches) > 1:
            return {
                "result": {
                    "event_code": event_code,
                    "enabled": True,
                    "reason": "multiple_exact_matches",
                    "sync": current_sync_summary,
                    "username": username,
                    "sha256": replay_sha256,
                    "filename": replay_filename,
                    "match_count": len(hash_matches),
                    "message_ids": [str(item["message_id"]) for item in hash_matches[:10]],
                }
            }
        return {"hit": hash_matches[0], "match_reason": "sha256_exact", "sync": current_sync_summary}

    attempt = _attempt_match(sync_summary)
    if attempt is None:
        deep_sync_summary = targeted_sync_locking_scores_from_discord(
            connection,
            download_only_usernames=[username],
            replay_score=replay_score,
            deep=True,
        )
        if not deep_sync_summary["enabled"]:
            return {
                "event_code": event_code,
                "enabled": False,
                "reason": deep_sync_summary["reason"],
                "sync": deep_sync_summary,
                "username": username,
            }
        attempt = _attempt_match(deep_sync_summary)
        if attempt is None:
            return {
                "event_code": event_code,
                "enabled": True,
                "reason": "no_filename_or_hash_match",
                "sync": deep_sync_summary,
                "username": username,
                "sha256": replay_sha256,
                "filename": replay_filename,
            }
    if attempt.get("result"):
        return attempt["result"]
    hit = attempt["hit"]
    match_reason = attempt["match_reason"]
    sync_summary = attempt["sync"]

    score_value = hit["parsed_score"]
    if score_value is None:
        return {
            "event_code": event_code,
            "enabled": True,
            "reason": "matched_message_missing_score",
            "sync": sync_summary,
            "username": username,
            "sha256": replay_sha256,
            "message_id": str(hit["message_id"]),
        }

    ended_at_dt = _resolve_candidate_ended_at(hit) or _resolve_candidate_posted_at(hit)
    ended_at_text = ended_at_dt.replace(tzinfo=None).isoformat(timespec="seconds") if ended_at_dt is not None else _now_iso_local()
    display_name_row = connection.execute(
        """
        SELECT p.display_name
        FROM registrations r
        JOIN events e ON e.id = r.event_id
        JOIN players p ON p.id = r.player_id
        LEFT JOIN player_accounts pa
          ON pa.player_id = p.id
         AND pa.platform_id = e.platform_id
         AND pa.is_primary = 1
        WHERE e.event_code = ? AND lower(pa.account_key) = lower(?) AND r.status = 'active'
        ORDER BY r.id DESC
        LIMIT 1
        """,
        (event_code, username),
    ).fetchone()
    display_name = display_name_row["display_name"] if display_name_row else username
    source_record_id = "discord_hash_{}_{}_{}".format(event_code, hit["channel_id"], hit["message_id"])
    added = add_manual_score(
        connection,
        event_code,
        username=username,
        display_name=display_name,
        source_record_id=source_record_id,
        started_at=None,
        ended_at=ended_at_text,
        raw_score=int(score_value),
        final_score=int(score_value),
        competition_score=int(score_value),
        primary_time_ms=None,
        target_tile_value=None,
        score_before_target=None,
        evidence_note="discord_hash_match sha256={} channel={} message={} attachment={} file={}".format(
            replay_sha256,
            hit["channel_id"],
            hit["message_id"],
            hit["attachment_id"],
            str(source),
        ),
    )
    return {
        "event_code": event_code,
        "enabled": True,
        "reason": "matched",
        "matched_by": match_reason,
        "sync": sync_summary,
        "username": username,
        "sha256": replay_sha256,
        "filename": replay_filename,
        "score": int(score_value),
        "message_id": str(hit["message_id"]),
        "attachment_id": str(hit["attachment_id"]),
        "source_record_id": source_record_id,
        "performance_record_id": added["performance_record_id"],
    }


def _refresh_locking_sessions(connection, event_row, sessions, sync_summary):
    processed = 0
    matched = 0
    manual_required = 0
    no_match = 0
    skipped = 0
    details = []

    for session in sessions:
        if not session["account_key"]:
            skipped += 1
            details.append({"session_id": session["id"], "status": "skipped", "reason": "missing_account"})
            continue
        if not _session_needs_auto_bind(session):
            skipped += 1
            details.append({"session_id": session["id"], "status": "skipped", "reason": "no_early_or_already_final"})
            continue
        processed += 1
        candidate_limit = _parse_positive_int_env("DISCORD_LOCK_CANDIDATE_LIMIT", 600)
        candidate_scope = "username"
        candidates = _load_candidate_attachments(connection, session["account_key"], limit=candidate_limit)
        if not candidates:
            candidate_scope = "global_recent"
            candidates = _load_candidate_attachments(connection, None, limit=candidate_limit)
        outcome = _auto_bind_one_session(connection, event_row, session, candidates)
        details.append({"session_id": session["id"], "candidate_scope": candidate_scope, **outcome})
        if outcome["status"] == "matched":
            matched += 1
        elif outcome["status"] == "manual_required":
            manual_required += 1
        elif outcome["status"] == "no_match":
            no_match += 1
        else:
            skipped += 1

    return {
        "event_code": event_row["event_code"],
        "enabled": True,
        "reason": "ok",
        "sync": sync_summary,
        "processed_sessions": processed,
        "matched_sessions": matched,
        "manual_required_sessions": manual_required,
        "no_match_sessions": no_match,
        "skipped_sessions": skipped,
        "details": details,
    }
