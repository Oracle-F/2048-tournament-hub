"""Redacted official-QQ relationship state for proactive group delivery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from services.bot_transport import BotContractError
from settings import LOCAL_TIMEZONE, TEMP_DIR


RelationshipSubjectKind = Literal["group", "c2c"]
RelationshipStatus = Literal[
    "unknown",
    "joined",
    "receivable",
    "rejected",
    "removed",
]
DEFAULT_OFFICIAL_TARGET_STATE_PATH = (
    TEMP_DIR / "stars_cup_bot" / "official_target_relationship.json"
)
_SCHEMA_VERSION = 1
_VALID_STATUSES = frozenset(
    {"unknown", "joined", "receivable", "rejected", "removed"}
)
_EVENT_FIELDS: dict[
    str,
    tuple[RelationshipSubjectKind, str, RelationshipStatus],
] = {
    "GROUP_ADD_ROBOT": ("group", "group_openid", "joined"),
    "GROUP_DEL_ROBOT": ("group", "group_openid", "removed"),
    "GROUP_MSG_REJECT": ("group", "group_openid", "rejected"),
    "GROUP_MSG_RECEIVE": ("group", "group_openid", "receivable"),
    "C2C_MSG_REJECT": ("c2c", "openid", "rejected"),
    "C2C_MSG_RECEIVE": ("c2c", "openid", "receivable"),
    "FRIEND_ADD": ("c2c", "openid", "joined"),
    "FRIEND_DEL": ("c2c", "openid", "removed"),
}


class OfficialRelationshipStateError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class OfficialRelationshipUpdate:
    event_type: str
    subject_kind: RelationshipSubjectKind
    subject_id: str
    status: RelationshipStatus
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class OfficialTargetState:
    target_hash: str
    status: RelationshipStatus
    event_type: str | None = None
    observed_at: datetime | None = None


def _local_time(value: datetime | None) -> datetime:
    current = value or datetime.now(LOCAL_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LOCAL_TIMEZONE)
    return current.astimezone(LOCAL_TIMEZONE)


def _target_hash(target_group_openid: str) -> str:
    target = str(target_group_openid or "").strip()
    if not target:
        raise BotContractError("official target group must not be empty")
    return hashlib.sha256(target.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.{}.tmp".format(path.name, uuid4().hex))
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def official_relationship_event_to_update(
    event_type: str,
    event: Any,
    *,
    observed_at: datetime | None = None,
) -> OfficialRelationshipUpdate | None:
    normalized_type = str(event_type or "").strip().upper()
    mapped = _EVENT_FIELDS.get(normalized_type)
    if mapped is None:
        return None
    subject_kind, field_name, status = mapped
    subject_id = str(getattr(event, field_name, "") or "").strip()
    if not subject_id:
        raise BotContractError(
            "official relationship event is missing {}".format(field_name)
        )
    return OfficialRelationshipUpdate(
        event_type=normalized_type,
        subject_kind=subject_kind,
        subject_id=subject_id,
        status=status,
        observed_at=_local_time(observed_at),
    )


def load_official_target_state(
    state_path: Path,
    target_group_openid: str,
) -> OfficialTargetState:
    expected_hash = _target_hash(target_group_openid)
    path = Path(state_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return OfficialTargetState(
            target_hash=expected_hash,
            status="unknown",
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OfficialRelationshipStateError(
            "official target relationship state is unreadable",
            code="state_unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise OfficialRelationshipStateError(
            "official target relationship state must be an object",
            code="state_invalid",
        )
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise OfficialRelationshipStateError(
            "official target relationship state schema is unsupported",
            code="state_schema_unsupported",
        )
    if payload.get("target_hash") != expected_hash:
        raise OfficialRelationshipStateError(
            "official target relationship state belongs to another target",
            code="state_target_mismatch",
        )
    status = str(payload.get("status") or "").strip()
    if status not in _VALID_STATUSES:
        raise OfficialRelationshipStateError(
            "official target relationship status is invalid",
            code="state_status_invalid",
        )
    event_type_value = payload.get("event_type")
    event_type = (
        str(event_type_value).strip() or None
        if event_type_value is not None
        else None
    )
    observed_raw = payload.get("observed_at")
    observed_at = None
    if observed_raw is not None:
        try:
            observed_at = _local_time(
                datetime.fromisoformat(str(observed_raw))
            )
        except ValueError as exc:
            raise OfficialRelationshipStateError(
                "official target relationship timestamp is invalid",
                code="state_timestamp_invalid",
            ) from exc
    return OfficialTargetState(
        target_hash=expected_hash,
        status=status,
        event_type=event_type,
        observed_at=observed_at,
    )


def observe_official_relationship_event(
    update: OfficialRelationshipUpdate,
    target_group_openid: str,
    state_path: Path,
) -> OfficialTargetState | None:
    expected_hash = _target_hash(target_group_openid)
    if update.subject_kind != "group":
        return None
    if update.subject_id != str(target_group_openid).strip():
        return None
    state = OfficialTargetState(
        target_hash=expected_hash,
        status=update.status,
        event_type=update.event_type,
        observed_at=update.observed_at,
    )
    _atomic_write_json(
        Path(state_path),
        {
            "schema_version": _SCHEMA_VERSION,
            "target_hash": state.target_hash,
            "status": state.status,
            "event_type": state.event_type,
            "observed_at": state.observed_at.isoformat(),
        },
    )
    return state


def official_target_send_allowed(
    state: OfficialTargetState,
) -> tuple[bool, str]:
    reasons = {
        "unknown": "relationship_unknown",
        "joined": "relationship_joined",
        "receivable": "relationship_receivable",
        "rejected": "relationship_rejected",
        "removed": "robot_removed",
    }
    reason = reasons.get(state.status)
    if reason is None:
        raise OfficialRelationshipStateError(
            "official target relationship status is invalid",
            code="state_status_invalid",
        )
    return state.status not in {"rejected", "removed"}, reason
