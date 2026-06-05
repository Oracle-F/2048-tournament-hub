import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from services.attempt_bind_service import lookup_attempt_session
from settings import DATA_DIR


EVIDENCE_ROOT_DIR = DATA_DIR / "evidence"
EARLY_CHECKPOINT_TILE_DEFAULT = 128
EARLY_REPLAY_MAX_BYTES_DEFAULT = 5120


def _read_positive_int_env(env_name, default_value):
    raw = str(os.getenv(env_name, "")).strip()
    if not raw:
        return default_value
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return value if value > 0 else default_value


EARLY_REPLAY_MAX_BYTES = _read_positive_int_env("EARLY_REPLAY_MAX_BYTES", EARLY_REPLAY_MAX_BYTES_DEFAULT)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _load_metadata(raw):
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dump_metadata(value):
    return json.dumps(value, ensure_ascii=False)


def _build_storage_dir(event_code, username):
    return EVIDENCE_ROOT_DIR / event_code / username


def _sha256_of_file(file_path):
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_into_evidence(event_code, username, source_path, prefix):
    source = Path(source_path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise ValueError("回放文件不存在或不是文件: {}".format(source))
    target_dir = _build_storage_dir(event_code, username)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = source.suffix or ".vrs"
    target_path = target_dir / "{}_{}{}".format(prefix, stamp, suffix)
    while target_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target_path = target_dir / "{}_{}{}".format(prefix, stamp, suffix)
    shutil.copy2(source, target_path)
    return target_path.resolve()


def _read_session_row(connection, session_id):
    row = connection.execute(
        """
        SELECT
            s.id,
            s.status,
            s.event_id,
            s.player_id,
            s.metadata_json,
            e.event_code,
            pa.account_key
        FROM attempt_sessions s
        JOIN events e ON e.id = s.event_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = s.player_id
           AND pa.platform_id = e.platform_id
           AND pa.is_primary = 1
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise ValueError("锁局会话不存在: {}".format(session_id))
    return row


def _resolve_session(connection, event_code, username, session_id=None):
    session = lookup_attempt_session(connection, event_code, username, session_id=session_id)
    return _read_session_row(connection, session["id"])


def _update_session_metadata(connection, session_row, metadata, new_status=None):
    connection.execute(
        """
        UPDATE attempt_sessions
        SET metadata_json = ?, status = COALESCE(?, status), updated_at = ?
        WHERE id = ?
        """,
        (_dump_metadata(metadata), new_status, now_iso(), session_row["id"]),
    )


def _load_replay_entries(metadata):
    early = metadata.get("early_replay")
    final = metadata.get("final_replay")
    if not isinstance(early, dict):
        early = None
    if not isinstance(final, dict):
        final = None
    return early, final


def verify_replay_prefix(early_path, final_path):
    early = Path(early_path)
    final = Path(final_path)
    if not early.exists():
        return {"status": "failed", "reason": "early_replay_missing", "details": {"early_path": str(early)}}
    if not final.exists():
        return {"status": "failed", "reason": "final_replay_missing", "details": {"final_path": str(final)}}
    if not early.is_file():
        return {"status": "failed", "reason": "early_replay_not_file", "details": {"early_path": str(early)}}
    if not final.is_file():
        return {"status": "failed", "reason": "final_replay_not_file", "details": {"final_path": str(final)}}
    early_size = early.stat().st_size
    final_size = final.stat().st_size
    if early_size <= 0:
        return {"status": "failed", "reason": "early_replay_empty", "details": {"early_size_bytes": early_size}}
    if final_size < early_size:
        return {
            "status": "failed",
            "reason": "final_shorter_than_early",
            "details": {"early_size_bytes": early_size, "final_size_bytes": final_size},
        }
    with early.open("rb") as early_handle, final.open("rb") as final_handle:
        compare_size = 1024 * 1024
        while True:
            early_chunk = early_handle.read(compare_size)
            if not early_chunk:
                break
            final_chunk = final_handle.read(len(early_chunk))
            if early_chunk != final_chunk:
                return {
                    "status": "failed",
                    "reason": "prefix_mismatch",
                    "details": {"early_size_bytes": early_size, "final_size_bytes": final_size},
                }
    return {
        "status": "passed",
        "reason": "prefix_matched",
        "details": {"early_size_bytes": early_size, "final_size_bytes": final_size},
    }


def _refresh_session_status_from_chain(metadata):
    early, final = _load_replay_entries(metadata)
    check = metadata.get("prefix_check") if isinstance(metadata.get("prefix_check"), dict) else None
    review = metadata.get("review") if isinstance(metadata.get("review"), dict) else None
    if not early or not final:
        return "pending_lock"
    if not check or check.get("status") != "passed":
        return "pending_lock"
    if not review or review.get("status") != "approved":
        return "locked_in_progress"
    return "completed"


def attach_early_replay(
    connection,
    event_code,
    username,
    source_file_path,
    *,
    checkpoint_tile=EARLY_CHECKPOINT_TILE_DEFAULT,
    session_id=None,
):
    source = Path(source_file_path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise ValueError("回放文件不存在或不是文件: {}".format(source))
    source_size = source.stat().st_size
    if source_size > EARLY_REPLAY_MAX_BYTES:
        raise ValueError(
            "early 回放文件过大（{} 字节），当前上限为 {} 字节。请在更早节点发送 floor。".format(
                source_size,
                EARLY_REPLAY_MAX_BYTES,
            )
        )

    session = _resolve_session(connection, event_code, username, session_id=session_id)
    metadata = _load_metadata(session["metadata_json"])
    metadata.setdefault("lock_method", "vrs_prefix_chain")
    stored_path = _copy_into_evidence(event_code, username, source_file_path, "early")
    metadata["early_replay"] = {
        "path": str(stored_path),
        "sha256": _sha256_of_file(stored_path),
        "size_bytes": stored_path.stat().st_size,
        "submitted_at": now_iso(),
        "checkpoint_tile": int(checkpoint_tile),
    }
    metadata["review"] = {"status": "pending", "reviewer": None, "note": None}
    metadata.pop("prefix_check", None)
    new_status = _refresh_session_status_from_chain(metadata)
    _update_session_metadata(connection, session, metadata, new_status=new_status)
    return {"session_id": session["id"], "event_code": event_code, "username": username, "stored_path": str(stored_path)}


def attach_final_replay(connection, event_code, username, source_file_path, *, session_id=None):
    session = _resolve_session(connection, event_code, username, session_id=session_id)
    metadata = _load_metadata(session["metadata_json"])
    metadata.setdefault("lock_method", "vrs_prefix_chain")
    stored_path = _copy_into_evidence(event_code, username, source_file_path, "final")
    metadata["final_replay"] = {
        "path": str(stored_path),
        "sha256": _sha256_of_file(stored_path),
        "size_bytes": stored_path.stat().st_size,
        "submitted_at": now_iso(),
    }
    metadata["review"] = {"status": "pending", "reviewer": None, "note": None}
    metadata.pop("prefix_check", None)
    new_status = _refresh_session_status_from_chain(metadata)
    _update_session_metadata(connection, session, metadata, new_status=new_status)
    return {"session_id": session["id"], "event_code": event_code, "username": username, "stored_path": str(stored_path)}


def run_replay_prefix_check(connection, event_code, username, *, session_id=None):
    session = _resolve_session(connection, event_code, username, session_id=session_id)
    metadata = _load_metadata(session["metadata_json"])
    early, final = _load_replay_entries(metadata)
    if not early or not final:
        reason = "missing_early_or_final_replay"
        metadata["prefix_check"] = {"status": "failed", "checked_at": now_iso(), "reason": reason, "details": {}}
        new_status = _refresh_session_status_from_chain(metadata)
        _update_session_metadata(connection, session, metadata, new_status=new_status)
        return {"session_id": session["id"], "status": "failed", "reason": reason, "details": {}}
    result = verify_replay_prefix(early["path"], final["path"])
    metadata["prefix_check"] = {
        "status": result["status"],
        "checked_at": now_iso(),
        "reason": result["reason"],
        "details": result.get("details") or {},
    }
    if result["status"] != "passed":
        metadata["review"] = {"status": "pending", "reviewer": None, "note": None}
    new_status = _refresh_session_status_from_chain(metadata)
    _update_session_metadata(connection, session, metadata, new_status=new_status)
    return {"session_id": session["id"], **result}


def set_replay_review_status(connection, event_code, username, *, approved, reviewer, note="", session_id=None):
    session = _resolve_session(connection, event_code, username, session_id=session_id)
    metadata = _load_metadata(session["metadata_json"])
    metadata.setdefault("review", {})
    metadata["review"] = {
        "status": "approved" if approved else "rejected",
        "reviewer": reviewer,
        "note": note or None,
        "reviewed_at": now_iso(),
    }
    new_status = _refresh_session_status_from_chain(metadata)
    _update_session_metadata(connection, session, metadata, new_status=new_status)
    return {"session_id": session["id"], "review_status": metadata["review"]["status"], "status": new_status}


def get_replay_chain_status(connection, event_code, username, *, session_id=None):
    session = _resolve_session(connection, event_code, username, session_id=session_id)
    metadata = _load_metadata(session["metadata_json"])
    early, final = _load_replay_entries(metadata)
    prefix_check = metadata.get("prefix_check") if isinstance(metadata.get("prefix_check"), dict) else None
    review = metadata.get("review") if isinstance(metadata.get("review"), dict) else None
    return {
        "session_id": session["id"],
        "event_code": event_code,
        "username": username,
        "status": session["status"],
        "lock_method": metadata.get("lock_method"),
        "early_replay": early,
        "final_replay": final,
        "prefix_check": prefix_check,
        "review": review,
    }


def list_replay_chain_issues(connection, event_code):
    rows = connection.execute(
        """
        SELECT
            s.id,
            s.status,
            s.metadata_json,
            p.display_name,
            pa.account_key AS username
        FROM attempt_sessions s
        JOIN events e ON e.id = s.event_id
        JOIN players p ON p.id = s.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = p.id
           AND pa.platform_id = e.platform_id
           AND pa.is_primary = 1
        WHERE e.event_code = ?
          AND s.status IN ('pending_lock', 'locked_in_progress', 'completed')
        ORDER BY s.id DESC
        """,
        (event_code,),
    ).fetchall()
    latest = {}
    for row in rows:
        key = row["username"] or "player_{}".format(row["id"])
        if key not in latest:
            latest[key] = row
    issues = []
    for row in latest.values():
        if row["status"] in {"expired", "cancelled"}:
            continue
        metadata = _load_metadata(row["metadata_json"])
        early, final = _load_replay_entries(metadata)
        check = metadata.get("prefix_check") if isinstance(metadata.get("prefix_check"), dict) else None
        review = metadata.get("review") if isinstance(metadata.get("review"), dict) else None
        label = "{} ({})".format(row["display_name"], row["username"] or "-")
        if not early:
            issues.append("选手 {} 缺少早期回放文件。".format(label))
            continue
        if not final:
            issues.append("选手 {} 缺少终局回放文件。".format(label))
            continue
        if not check:
            issues.append("选手 {} 尚未执行回放前缀校验。".format(label))
            continue
        if check.get("status") != "passed":
            issues.append("选手 {} 回放前缀校验失败：{}。".format(label, check.get("reason") or "unknown"))
            continue
        if not review or review.get("status") != "approved":
            issues.append("选手 {} 回放证据尚未人工复核通过。".format(label))
    return issues
