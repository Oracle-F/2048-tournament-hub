"""Idempotent in-process scheduler for the 群星杯 daily image delivery.

The scheduler is inert until a connected official QQ runtime explicitly
starts :meth:`OfficialStarsCupDailyScheduler.run_forever`.  Export work runs
in a worker thread so inbound WebSocket callbacks remain responsive.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.error import HTTPError, URLError

from bot_official_qq.relationship_state import (
    DEFAULT_OFFICIAL_TARGET_STATE_PATH,
    OfficialRelationshipStateError,
    load_official_target_state,
    official_target_send_allowed,
)
from services.bot_transport import (
    BotContractError,
    BotTransport,
    BotTransportError,
    SendReceipt,
)
from services.stars_cup_daily_service import (
    DEFAULT_STATE_ROOT,
    DailyRunResult,
    StarsCupDailyError,
    StarsCupDailyLocked,
    StarsCupDeliveryError,
    StarsCupDeliveryLocked,
    deliver_stars_cup_daily_images,
    run_stars_cup_daily_export,
)
from settings import LOCAL_TIMEZONE


LOGGER = logging.getLogger(__name__)
SchedulerStatus = Literal[
    "not_due",
    "sent",
    "retryable_failure",
    "terminal_failure",
]
ExportFunction = Callable[..., DailyRunResult]
DeliveryFunction = Callable[..., Awaitable[DailyRunResult]]
ReconciliationStatus = Literal[
    "not_started",
    "sent",
    "retryable_failure",
    "terminal_failure",
]
_TRANSIENT_HTTP_STATUS_CODES = frozenset(
    {408, 425, 429, *range(500, 600)}
)
_TRANSIENT_ERRNOS = frozenset(
    {
        errno.EAGAIN,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EPIPE,
        errno.ETIMEDOUT,
    }
)


@dataclass(frozen=True, slots=True)
class SchedulerTickResult:
    status: SchedulerStatus
    run_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerReconciliation:
    status: ReconciliationStatus
    run_id: str
    error_code: str | None = None


class StarsCupSchedulerStateError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def _local_time(value: datetime | None) -> datetime:
    current = value or datetime.now(LOCAL_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LOCAL_TIMEZONE)
    return current.astimezone(LOCAL_TIMEZONE)


def _exception_chain(exc: Exception) -> tuple[BaseException, ...]:
    values: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        values.append(current)
        current = current.__cause__ or current.__context__
    return tuple(values)


def _target_hash(target: str) -> str:
    return hashlib.sha256(target.encode("utf-8")).hexdigest()


def reconcile_stars_cup_scheduler_state(
    current: datetime,
    *,
    state_root: Path,
    group_openid: str,
) -> SchedulerReconciliation:
    run_id = _local_time(current).strftime("%Y%m%d")
    delivery_path = (
        Path(state_root) / "deliveries" / "{}.json".format(run_id)
    )
    try:
        payload = json.loads(delivery_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SchedulerReconciliation(
            status="not_started",
            run_id=run_id,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StarsCupSchedulerStateError(
            "Stars Cup delivery state is unreadable",
            code="scheduler_state_invalid",
        ) from exc
    if not isinstance(payload, dict):
        raise StarsCupSchedulerStateError(
            "Stars Cup delivery state must be an object",
            code="scheduler_state_invalid",
        )
    if (
        payload.get("schema_version") != 1
        or payload.get("run_id") != run_id
        or payload.get("target_hash") != _target_hash(group_openid)
        or payload.get("transport") != "qq_official"
    ):
        raise StarsCupSchedulerStateError(
            "Stars Cup delivery state identity is invalid",
            code="scheduler_state_invalid",
        )
    images = payload.get("images")
    if not isinstance(images, dict):
        raise StarsCupSchedulerStateError(
            "Stars Cup delivery images state is invalid",
            code="scheduler_state_invalid",
        )
    statuses: dict[str, str] = {}
    error_codes: dict[str, str | None] = {}
    valid_statuses = {
        "pending",
        "sending",
        "sent",
        "failed_retryable",
        "failed_terminal",
        "unknown",
    }
    for kind in ("total", "detail"):
        entry = images.get(kind)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise StarsCupSchedulerStateError(
                "Stars Cup delivery image entry is invalid",
                code="scheduler_state_invalid",
            )
        status = str(entry.get("status") or "").strip()
        if status not in valid_statuses:
            raise StarsCupSchedulerStateError(
                "Stars Cup delivery image status is invalid",
                code="scheduler_state_invalid",
            )
        statuses[kind] = status
        error_value = entry.get("error_code")
        error_codes[kind] = (
            str(error_value).strip() or None
            if error_value is not None
            else None
        )
    if any(status in {"unknown", "sending"} for status in statuses.values()):
        return SchedulerReconciliation(
            status="terminal_failure",
            run_id=run_id,
            error_code="delivery_unknown",
        )
    for kind in ("total", "detail"):
        if statuses.get(kind) == "failed_terminal":
            return SchedulerReconciliation(
                status="terminal_failure",
                run_id=run_id,
                error_code=error_codes.get(kind) or "delivery_terminal",
            )
    for kind in ("total", "detail"):
        if statuses.get(kind) == "failed_retryable":
            return SchedulerReconciliation(
                status="retryable_failure",
                run_id=run_id,
                error_code=error_codes.get(kind) or "delivery_retryable",
            )
    if statuses == {"total": "sent", "detail": "sent"}:
        return SchedulerReconciliation(
            status="sent",
            run_id=run_id,
            error_code="reconciled_sent",
        )
    return SchedulerReconciliation(
        status="not_started",
        run_id=run_id,
    )


def classify_scheduler_exception(
    exc: Exception,
) -> tuple[SchedulerStatus, str]:
    """Classify an escaped workflow exception without raising another one."""

    chain = _exception_chain(exc)
    for error in chain:
        if isinstance(error, BotTransportError):
            return (
                (
                    "retryable_failure"
                    if error.retryable
                    else "terminal_failure"
                ),
                error.code or "transport_error",
            )
    if any(isinstance(error, StarsCupDeliveryLocked) for error in chain):
        return "retryable_failure", "delivery_locked"
    if any(isinstance(error, StarsCupDailyLocked) for error in chain):
        return "retryable_failure", "daily_locked"
    if any(isinstance(error, TimeoutError) for error in chain):
        return "retryable_failure", "network_timeout"
    if any(isinstance(error, ConnectionError) for error in chain):
        return "retryable_failure", "network_connection"
    for error in chain:
        if isinstance(error, HTTPError):
            if int(error.code) in _TRANSIENT_HTTP_STATUS_CODES:
                return "retryable_failure", "http_{}".format(error.code)
            return "terminal_failure", "http_{}".format(error.code)
        if isinstance(error, URLError):
            return "retryable_failure", "network_url"
        if isinstance(error, OSError) and error.errno in _TRANSIENT_ERRNOS:
            return "retryable_failure", "network_os"
    if any(isinstance(error, BotContractError) for error in chain):
        return "terminal_failure", "contract_error"
    if any(
        isinstance(error, OfficialRelationshipStateError)
        for error in chain
    ):
        return "terminal_failure", "relationship_state_invalid"
    if any(isinstance(error, StarsCupSchedulerStateError) for error in chain):
        return "terminal_failure", "scheduler_state_invalid"
    if any(isinstance(error, StarsCupDeliveryError) for error in chain):
        return "terminal_failure", "delivery_error"
    if any(isinstance(error, StarsCupDailyError) for error in chain):
        return "terminal_failure", "daily_error"
    if any(isinstance(error, ValueError) for error in chain):
        return "terminal_failure", "invalid_configuration"
    if any(isinstance(error, OSError) for error in chain):
        return "terminal_failure", "filesystem_error"
    return "terminal_failure", "unexpected_exception"


class OfficialStarsCupDailyScheduler:
    """Run at most one durable delivery workflow per local calendar day."""

    def __init__(
        self,
        *,
        group_openid: str,
        send_time: time,
        state_root: Path = DEFAULT_STATE_ROOT,
        relationship_state_path: Path = DEFAULT_OFFICIAL_TARGET_STATE_PATH,
        poll_seconds: int = 30,
        retry_seconds: int = 900,
        export_function: ExportFunction = run_stars_cup_daily_export,
        delivery_function: DeliveryFunction = deliver_stars_cup_daily_images,
    ):
        target = str(group_openid or "").strip()
        if not target:
            raise ValueError("group_openid must not be empty")
        if send_time.tzinfo is not None:
            raise ValueError("send_time must be a local wall-clock time")
        if int(poll_seconds) < 1 or int(retry_seconds) < 1:
            raise ValueError("scheduler intervals must be positive")
        self.group_openid = target
        self.send_time = send_time
        self.state_root = Path(state_root)
        self.relationship_state_path = Path(relationship_state_path)
        self.poll_seconds = int(poll_seconds)
        self.retry_seconds = int(retry_seconds)
        self.export_function = export_function
        self.delivery_function = delivery_function
        self._completed_date = None
        self._retry_not_before: datetime | None = None
        self._reconciled_date = None

    def _is_due(self, current: datetime) -> bool:
        if self._completed_date == current.date():
            return False
        if current.time().replace(tzinfo=None) < self.send_time:
            return False
        if self._retry_not_before is not None and current < self._retry_not_before:
            return False
        return True

    @staticmethod
    def _classify_receipts(
        receipts: tuple[SendReceipt, ...],
    ) -> tuple[SchedulerStatus, str | None]:
        for receipt in receipts:
            if receipt.status == "failed" and receipt.retryable:
                return "retryable_failure", receipt.error_code
            if receipt.status in {"failed", "unknown"}:
                return "terminal_failure", receipt.error_code
        return "sent", None

    async def tick(
        self,
        transport: BotTransport,
        *,
        current_time: datetime | None = None,
    ) -> SchedulerTickResult:
        current = _local_time(current_time)
        if not self._is_due(current):
            return SchedulerTickResult(status="not_due")
        try:
            if self._reconciled_date != current.date():
                reconciled = reconcile_stars_cup_scheduler_state(
                    current,
                    state_root=self.state_root,
                    group_openid=self.group_openid,
                )
                self._reconciled_date = current.date()
                if reconciled.status == "sent":
                    self._completed_date = current.date()
                    self._retry_not_before = None
                    return SchedulerTickResult(
                        status="sent",
                        run_id=reconciled.run_id,
                        error_code=reconciled.error_code,
                    )
                if reconciled.status == "terminal_failure":
                    self._completed_date = current.date()
                    self._retry_not_before = None
                    return SchedulerTickResult(
                        status="terminal_failure",
                        run_id=reconciled.run_id,
                        error_code=reconciled.error_code,
                    )
            relationship = load_official_target_state(
                self.relationship_state_path,
                self.group_openid,
            )
            send_allowed, relationship_code = (
                official_target_send_allowed(relationship)
            )
            if not send_allowed:
                self._completed_date = current.date()
                self._retry_not_before = None
                LOGGER.warning(
                    "Stars Cup scheduled delivery blocked by "
                    "relationship state=%s",
                    relationship.status,
                )
                return SchedulerTickResult(
                    status="terminal_failure",
                    run_id=current.strftime("%Y%m%d"),
                    error_code=relationship_code,
                )
            export_result = await asyncio.to_thread(
                self.export_function,
                run_time=current,
            )
            delivery_path = (
                self.state_root
                / "deliveries"
                / "{}.json".format(export_result.run_id)
            )
            delivered = await self.delivery_function(
                export_result,
                transport,
                group_id=self.group_openid,
                delivery_state_path=delivery_path,
                dry_run=False,
                attempt_time=current,
            )
        except Exception as exc:
            status, error_code = classify_scheduler_exception(exc)
            if status == "retryable_failure":
                self._retry_not_before = current + timedelta(
                    seconds=self.retry_seconds
                )
            else:
                self._completed_date = current.date()
                self._retry_not_before = None
            LOGGER.warning(
                "Stars Cup scheduled workflow failed before receipt "
                "type=%s status=%s code=%s",
                type(exc).__name__,
                status,
                error_code,
            )
            return SchedulerTickResult(
                status=status,
                run_id=current.strftime("%Y%m%d"),
                error_code=error_code,
            )
        status, error_code = self._classify_receipts(
            tuple(delivered.receipts)
        )
        if status == "retryable_failure":
            self._retry_not_before = current + timedelta(
                seconds=self.retry_seconds
            )
        else:
            self._completed_date = current.date()
            self._retry_not_before = None
        LOGGER.info(
            "Stars Cup scheduled workflow finished run_id=%s status=%s",
            export_result.run_id,
            status,
        )
        return SchedulerTickResult(
            status=status,
            run_id=export_result.run_id,
            error_code=error_code,
        )

    async def run_forever(self, transport: BotTransport) -> None:
        while True:
            await self.tick(transport)
            await asyncio.sleep(self.poll_seconds)
