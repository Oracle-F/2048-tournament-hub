from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from settings import LOCAL_TIMEZONE, TEMP_DIR


BOT_CONNECTION_STATE_PATH = TEMP_DIR / "bot_connection_state.json"
BOT_CONNECTION_RUNTIME_LOG_PATH = TEMP_DIR / "napcat_runtime.log"
BOT_CONNECTION_STATE_DEFAULT_STALE_SECONDS = 120
BOT_CONNECTION_STATE_DEFAULT_STARTUP_GRACE_SECONDS = 90
BOT_CONNECTION_STATE_DEFAULT_RESTART_COOLDOWN_SECONDS = 300
BOT_CONNECTION_STATE_DEFAULT_ATTENTION_RESTART_COUNT = 2
BOT_CONNECTION_STATE_DEFAULT_ATTENTION_COOLDOWN_SECONDS = 900
BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_LINES = 80
BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_BYTES = 65536
BOT_CONNECTION_RUNTIME_LOG_HARD_ALERT_MARKERS = (
    ("KickedOffLine", "kicked_offline"),
    ("登录已失效", "login_invalid"),
    ("登录失效", "login_invalid"),
    ("账号被踢下线", "kicked_offline"),
    ("风控下线", "risk_controlled"),
)


def _now():
    return datetime.now(LOCAL_TIMEZONE)


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(LOCAL_TIMEZONE).isoformat()


def _parse_iso(value: Any):
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def _state_path(path: Path | str | None = None) -> Path:
    if path is None:
        return BOT_CONNECTION_STATE_PATH
    return Path(path)


def _runtime_log_path(path: Path | str | None = None) -> Path:
    if path is None:
        return BOT_CONNECTION_RUNTIME_LOG_PATH
    return Path(path)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _coerce_int(value: Any, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def read_text_tail(
    path: Path | str | None = None,
    *,
    max_lines: int = BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_LINES,
    max_bytes: int = BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    log_path = _runtime_log_path(path)
    try:
        stat_result = log_path.stat()
    except FileNotFoundError:
        return {
            "present": False,
            "path": str(log_path),
            "line_count": 0,
            "excerpt": [],
            "last_line": None,
            "truncated": False,
        }

    max_lines = _coerce_int(max_lines, BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_LINES) or BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_LINES
    max_bytes = _coerce_int(max_bytes, BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_BYTES) or BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_BYTES

    if stat_result.st_size <= max_bytes:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    else:
        with log_path.open("rb") as handle:
            handle.seek(max(0, stat_result.st_size - max_bytes))
            text = handle.read().decode("utf-8", errors="replace")

    lines = text.splitlines()
    excerpt = lines[-max_lines:] if max_lines > 0 else []
    return {
        "present": True,
        "path": str(log_path),
        "line_count": len(lines),
        "excerpt": excerpt,
        "last_line": excerpt[-1] if excerpt else None,
        "truncated": stat_result.st_size > max_bytes or len(lines) > max_lines,
    }


def detect_runtime_log_hard_alert(runtime_log: dict[str, Any] | None) -> dict[str, Any]:
    if not runtime_log or not runtime_log.get("present"):
        return {
            "hard_alert_required": False,
            "reason": "missing_runtime_log",
            "matched_marker": None,
            "matched_line": None,
        }

    excerpt = runtime_log.get("excerpt") or []
    for raw_line in reversed(excerpt):
        line = str(raw_line or "").strip()
        if not line:
            continue
        lowered = line.lower()
        for marker, reason in BOT_CONNECTION_RUNTIME_LOG_HARD_ALERT_MARKERS:
            if marker.lower() in lowered:
                return {
                    "hard_alert_required": True,
                    "reason": reason,
                    "matched_marker": marker,
                    "matched_line": line,
                }

    return {
        "hard_alert_required": False,
        "reason": "no_hard_alert",
        "matched_marker": None,
        "matched_line": None,
    }


def load_bot_connection_state(path: Path | str | None = None) -> dict[str, Any]:
    state_path = _state_path(path)
    try:
        raw = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_bot_connection_state(state: dict[str, Any], path: Path | str | None = None) -> Path:
    state_path = _state_path(path)
    _ensure_parent(state_path)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(state_path)
    return state_path


def _new_state(now: datetime) -> dict[str, Any]:
    now_iso = _to_iso(now)
    return {
        "status": "starting",
        "boot_at": now_iso,
        "last_seen_at": now_iso,
        "last_connect_at": None,
        "last_disconnect_at": None,
        "last_heartbeat_at": None,
        "last_meta_event_type": None,
        "last_meta_event_sub_type": None,
        "last_heartbeat_interval": None,
        "self_id": None,
        "last_attention_at": None,
        "last_attention_reason": None,
        "attention_count": 0,
        "updated_at": now_iso,
    }


def mark_bot_process_started(path: Path | str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    state = _new_state(current)
    save_bot_connection_state(state, path)
    return state


def _event_value(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def record_bot_meta_event(event: Any, path: Path | str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    state = load_bot_connection_state(path)
    if not state:
        state = _new_state(current)

    meta_event_type = str(_event_value(event, "meta_event_type", "") or "").strip() or None
    sub_type = str(_event_value(event, "sub_type", "") or "").strip() or None
    self_id = _event_value(event, "self_id", None)
    interval = _event_value(event, "interval", None)

    if self_id not in (None, ""):
        state["self_id"] = str(self_id)

    state["last_meta_event_type"] = meta_event_type
    state["last_meta_event_sub_type"] = sub_type
    state["last_seen_at"] = _to_iso(current)
    state["updated_at"] = _to_iso(current)

    if meta_event_type == "lifecycle":
        if sub_type == "connect":
            state["status"] = "online"
            state["last_connect_at"] = _to_iso(current)
        elif sub_type in {"disconnect", "disable"}:
            state["status"] = "offline"
            state["last_disconnect_at"] = _to_iso(current)
    elif meta_event_type == "heartbeat":
        state["status"] = "online"
        state["last_heartbeat_at"] = _to_iso(current)
        if interval not in (None, ""):
            try:
                state["last_heartbeat_interval"] = int(interval)
            except (TypeError, ValueError):
                state["last_heartbeat_interval"] = interval

    save_bot_connection_state(state, path)
    return state


def mark_bot_restart_attempt(
    path: Path | str | None = None,
    *,
    now: datetime | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    current = now or _now()
    state = load_bot_connection_state(path)
    if not state:
        state = _new_state(current)

    state["status"] = "restarting"
    state["last_restart_at"] = _to_iso(current)
    state["last_restart_reason"] = reason
    state["restart_count"] = _coerce_int(state.get("restart_count"), 0) + 1
    state["updated_at"] = _to_iso(current)
    save_bot_connection_state(state, path)
    return state


def mark_bot_attention_notice(
    path: Path | str | None = None,
    *,
    now: datetime | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    current = now or _now()
    state = load_bot_connection_state(path)
    if not state:
        state = _new_state(current)

    state["last_attention_at"] = _to_iso(current)
    state["last_attention_reason"] = reason
    state["attention_count"] = _coerce_int(state.get("attention_count"), 0) + 1
    state["updated_at"] = _to_iso(current)
    save_bot_connection_state(state, path)
    return state


def summarize_bot_connection_state(
    path: Path | str | None = None,
    *,
    stale_seconds: int = BOT_CONNECTION_STATE_DEFAULT_STALE_SECONDS,
    startup_grace_seconds: int = BOT_CONNECTION_STATE_DEFAULT_STARTUP_GRACE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    state = load_bot_connection_state(path)
    if not state:
        return {
            "present": False,
            "stale": True,
            "reason": "missing_state",
            "status": None,
            "age_seconds": None,
            "boot_age_seconds": None,
        }

    boot_at = _parse_iso(state.get("boot_at"))
    last_seen_at = _parse_iso(state.get("last_seen_at"))
    boot_age_seconds = None
    if boot_at is not None:
        boot_age_seconds = int((current - boot_at).total_seconds())

    if boot_age_seconds is not None and boot_age_seconds < startup_grace_seconds:
        return {
            "present": True,
            "stale": False,
            "reason": "startup_grace",
            "status": state.get("status"),
            "age_seconds": None if last_seen_at is None else int((current - last_seen_at).total_seconds()),
            "boot_age_seconds": boot_age_seconds,
        }

    age_seconds = None
    if last_seen_at is not None:
        age_seconds = int((current - last_seen_at).total_seconds())
        if age_seconds < stale_seconds:
            return {
                "present": True,
                "stale": False,
                "reason": "fresh",
                "status": state.get("status"),
                "age_seconds": age_seconds,
                "boot_age_seconds": boot_age_seconds,
            }

    return {
        "present": True,
        "stale": True,
        "reason": "stale",
        "status": state.get("status"),
        "age_seconds": age_seconds,
        "boot_age_seconds": boot_age_seconds,
    }


def should_attempt_restart(
    path: Path | str | None = None,
    *,
    stale_seconds: int = BOT_CONNECTION_STATE_DEFAULT_STALE_SECONDS,
    startup_grace_seconds: int = BOT_CONNECTION_STATE_DEFAULT_STARTUP_GRACE_SECONDS,
    restart_cooldown_seconds: int = BOT_CONNECTION_STATE_DEFAULT_RESTART_COOLDOWN_SECONDS,
    runtime_log_path: Path | str | None = None,
    runtime_log_max_lines: int = BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_LINES,
    runtime_log_max_bytes: int = BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_BYTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    state = load_bot_connection_state(path)
    if not state:
        return {
            "restart_required": False,
            "reason": "missing_state",
            "summary": {
                "present": False,
                "stale": True,
                "reason": "missing_state",
                "status": None,
                "age_seconds": None,
                "boot_age_seconds": None,
            },
        }

    summary = summarize_bot_connection_state(
        path,
        stale_seconds=stale_seconds,
        startup_grace_seconds=startup_grace_seconds,
        now=current,
    )
    if runtime_log_path is not None:
        runtime_log = read_text_tail(
            runtime_log_path,
            max_lines=runtime_log_max_lines,
            max_bytes=runtime_log_max_bytes,
        )
        hard_alert = detect_runtime_log_hard_alert(runtime_log)
        if hard_alert["hard_alert_required"]:
            return {
                "restart_required": False,
                "reason": "hard_alert",
                "summary": summary,
                "hard_alert_reason": hard_alert["reason"],
                "hard_alert_marker": hard_alert["matched_marker"],
                "hard_alert_line": hard_alert["matched_line"],
            }
    if not summary["stale"] and state.get("status") != "offline":
        return {
            "restart_required": False,
            "reason": summary["reason"],
            "summary": summary,
        }

    last_restart_at = _parse_iso(state.get("last_restart_at"))
    if last_restart_at is not None:
        restart_age_seconds = int((current - last_restart_at).total_seconds())
        if restart_age_seconds < restart_cooldown_seconds:
            summary["restart_age_seconds"] = restart_age_seconds
            return {
                "restart_required": False,
                "reason": "restart_cooldown",
                "summary": summary,
                "restart_age_seconds": restart_age_seconds,
            }

    return {
        "restart_required": True,
        "reason": summary["reason"],
        "summary": summary,
    }


def should_show_attention_notice(
    path: Path | str | None = None,
    *,
    stale_seconds: int = BOT_CONNECTION_STATE_DEFAULT_STALE_SECONDS,
    startup_grace_seconds: int = BOT_CONNECTION_STATE_DEFAULT_STARTUP_GRACE_SECONDS,
    attention_restart_count: int = BOT_CONNECTION_STATE_DEFAULT_ATTENTION_RESTART_COUNT,
    attention_cooldown_seconds: int = BOT_CONNECTION_STATE_DEFAULT_ATTENTION_COOLDOWN_SECONDS,
    runtime_log_path: Path | str | None = None,
    runtime_log_max_lines: int = BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_LINES,
    runtime_log_max_bytes: int = BOT_CONNECTION_RUNTIME_LOG_DEFAULT_MAX_BYTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    state = load_bot_connection_state(path)
    if not state:
        return {
            "attention_required": False,
            "reason": "missing_state",
            "summary": {
                "present": False,
                "stale": True,
                "reason": "missing_state",
                "status": None,
                "age_seconds": None,
                "boot_age_seconds": None,
            },
        }

    summary = summarize_bot_connection_state(
        path,
        stale_seconds=stale_seconds,
        startup_grace_seconds=startup_grace_seconds,
        now=current,
    )
    runtime_log = None
    hard_alert = None
    if runtime_log_path is not None:
        runtime_log = read_text_tail(
            runtime_log_path,
            max_lines=runtime_log_max_lines,
            max_bytes=runtime_log_max_bytes,
        )
        hard_alert = detect_runtime_log_hard_alert(runtime_log)
        if hard_alert["hard_alert_required"]:
            last_attention_at = _parse_iso(state.get("last_attention_at"))
            if last_attention_at is not None:
                attention_age_seconds = int((current - last_attention_at).total_seconds())
                if attention_age_seconds < attention_cooldown_seconds:
                    summary["attention_age_seconds"] = attention_age_seconds
                    return {
                        "attention_required": False,
                        "reason": "attention_cooldown",
                        "summary": summary,
                        "hard_alert_reason": hard_alert["reason"],
                        "hard_alert_marker": hard_alert["matched_marker"],
                        "hard_alert_line": hard_alert["matched_line"],
                        "attention_age_seconds": attention_age_seconds,
                    }

            return {
                "attention_required": True,
                "reason": "hard_alert",
                "summary": summary,
                "hard_alert_reason": hard_alert["reason"],
                "hard_alert_marker": hard_alert["matched_marker"],
                "hard_alert_line": hard_alert["matched_line"],
            }
    if not summary["stale"]:
        return {
            "attention_required": False,
            "reason": summary["reason"],
            "summary": summary,
        }

    restart_count = _coerce_int(state.get("restart_count"), 0) or 0
    if restart_count < max(1, int(attention_restart_count)):
        return {
            "attention_required": False,
            "reason": "attention_restart_threshold",
            "summary": summary,
            "restart_count": restart_count,
        }

    last_attention_at = _parse_iso(state.get("last_attention_at"))
    if last_attention_at is not None:
        attention_age_seconds = int((current - last_attention_at).total_seconds())
        if attention_age_seconds < attention_cooldown_seconds:
            summary["attention_age_seconds"] = attention_age_seconds
            return {
                "attention_required": False,
                "reason": "attention_cooldown",
                "summary": summary,
                "restart_count": restart_count,
                "attention_age_seconds": attention_age_seconds,
            }

    return {
        "attention_required": True,
        "reason": "persistent_offline",
        "summary": summary,
        "restart_count": restart_count,
    }


def connection_watchdog_roundtrip(
    *,
    boot_at: datetime,
    heartbeat_at: datetime,
    check_at: datetime,
    stale_seconds: int,
    startup_grace_seconds: int,
    path: Path | str | None = None,
) -> dict[str, Any]:
    state_path = _state_path(path)
    mark_bot_process_started(state_path, now=boot_at)
    startup_summary = summarize_bot_connection_state(
        state_path,
        stale_seconds=stale_seconds,
        startup_grace_seconds=startup_grace_seconds,
        now=check_at,
    )
    record_bot_meta_event(
        {
            "meta_event_type": "heartbeat",
            "interval": 30000,
            "self_id": "10001",
        },
        state_path,
        now=heartbeat_at,
    )
    post_heartbeat_summary = summarize_bot_connection_state(
        state_path,
        stale_seconds=stale_seconds,
        startup_grace_seconds=startup_grace_seconds,
        now=heartbeat_at,
    )
    final_summary = summarize_bot_connection_state(
        state_path,
        stale_seconds=stale_seconds,
        startup_grace_seconds=startup_grace_seconds,
        now=check_at,
    )
    return {
        "startup_reason": startup_summary["reason"],
        "startup_stale": startup_summary["stale"],
        "post_heartbeat_reason": post_heartbeat_summary["reason"],
        "post_heartbeat_stale": post_heartbeat_summary["stale"],
        "final_reason": final_summary["reason"],
        "final_stale": final_summary["stale"],
        "final_status": final_summary["status"],
        "final_age_seconds": final_summary["age_seconds"],
    }
