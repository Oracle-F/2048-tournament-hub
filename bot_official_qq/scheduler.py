"""Idempotent in-process scheduler for the 群星杯 daily image delivery.

The scheduler is inert until a connected official QQ runtime explicitly
starts :meth:`OfficialStarsCupDailyScheduler.run_forever`.  Export work runs
in a worker thread so inbound WebSocket callbacks remain responsive.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from services.bot_transport import BotTransport, SendReceipt
from services.stars_cup_daily_service import (
    DEFAULT_STATE_ROOT,
    DailyRunResult,
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


@dataclass(frozen=True, slots=True)
class SchedulerTickResult:
    status: SchedulerStatus
    run_id: str | None = None
    error_code: str | None = None


def _local_time(value: datetime | None) -> datetime:
    current = value or datetime.now(LOCAL_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LOCAL_TIMEZONE)
    return current.astimezone(LOCAL_TIMEZONE)


class OfficialStarsCupDailyScheduler:
    """Run at most one durable delivery workflow per local calendar day."""

    def __init__(
        self,
        *,
        group_openid: str,
        send_time: time,
        state_root: Path = DEFAULT_STATE_ROOT,
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
        self.poll_seconds = int(poll_seconds)
        self.retry_seconds = int(retry_seconds)
        self.export_function = export_function
        self.delivery_function = delivery_function
        self._completed_date = None
        self._retry_not_before: datetime | None = None

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
            self._retry_not_before = current + timedelta(
                seconds=self.retry_seconds
            )
            LOGGER.warning(
                "Stars Cup scheduled workflow failed before receipt type=%s",
                type(exc).__name__,
            )
            return SchedulerTickResult(
                status="retryable_failure",
                run_id=current.strftime("%Y%m%d"),
                error_code=type(exc).__name__,
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
