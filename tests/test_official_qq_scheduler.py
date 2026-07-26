from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, Mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_official_qq.scheduler import (  # noqa: E402
    OfficialStarsCupDailyScheduler,
    classify_scheduler_exception,
)
from bot_official_qq.relationship_state import (  # noqa: E402
    observe_official_relationship_event,
    official_relationship_event_to_update,
)
from services.bot_transport import (  # noqa: E402
    BotContractError,
    BotTransportError,
    SendReceipt,
)
from services.stars_cup_daily_service import (  # noqa: E402
    StarsCupDailyError,
    StarsCupDailyLocked,
    StarsCupDeliveryLocked,
)
from settings import LOCAL_TIMEZONE  # noqa: E402


def _receipt(*, status="sent", retryable=False, error_code=None):
    return SendReceipt(
        transport="qq_official",
        target="target-hash",
        status=status,
        retryable=retryable,
        error_code=error_code,
    )


class OfficialQQSchedulerTests(IsolatedAsyncioTestCase):
    def _scheduler(
        self,
        root: Path,
        *,
        receipts=(_receipt(),),
        retry_seconds=900,
    ):
        export_result = SimpleNamespace(
            run_id="20260727",
            receipts=(),
        )
        delivered = SimpleNamespace(
            run_id="20260727",
            receipts=tuple(receipts),
        )
        export = Mock(return_value=export_result)
        delivery = AsyncMock(return_value=delivered)
        scheduler = OfficialStarsCupDailyScheduler(
            group_openid="opaque-test-group",
            send_time=time(hour=9),
            state_root=root,
            relationship_state_path=root / "relationship.json",
            retry_seconds=retry_seconds,
            export_function=export,
            delivery_function=delivery,
        )
        return scheduler, export, delivery

    async def test_before_daily_time_has_no_export_or_send(self):
        with TemporaryDirectory() as temp_dir:
            scheduler, export, delivery = self._scheduler(Path(temp_dir))
            result = await scheduler.tick(
                object(),
                current_time=datetime(
                    2026,
                    7,
                    27,
                    8,
                    59,
                    tzinfo=LOCAL_TIMEZONE,
                ),
            )
        self.assertEqual(result.status, "not_due")
        export.assert_not_called()
        delivery.assert_not_awaited()

    async def test_due_tick_runs_export_and_two_stage_delivery_only_once(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scheduler, export, delivery = self._scheduler(root)
            current = datetime(
                2026,
                7,
                27,
                9,
                0,
                tzinfo=LOCAL_TIMEZONE,
            )
            first = await scheduler.tick(object(), current_time=current)
            second = await scheduler.tick(
                object(),
                current_time=current + timedelta(minutes=5),
            )

        self.assertEqual(first.status, "sent")
        self.assertEqual(second.status, "not_due")
        export.assert_called_once_with(run_time=current)
        delivery.assert_awaited_once()
        self.assertEqual(
            delivery.await_args.kwargs["delivery_state_path"],
            root / "deliveries" / "20260727.json",
        )
        self.assertFalse(delivery.await_args.kwargs["dry_run"])

    async def test_retryable_receipt_waits_for_retry_window(self):
        with TemporaryDirectory() as temp_dir:
            scheduler, export, delivery = self._scheduler(
                Path(temp_dir),
                receipts=(
                    _receipt(
                        status="failed",
                        retryable=True,
                        error_code="429",
                    ),
                ),
                retry_seconds=600,
            )
            current = datetime(
                2026,
                7,
                27,
                9,
                0,
                tzinfo=LOCAL_TIMEZONE,
            )
            first = await scheduler.tick(object(), current_time=current)
            waiting = await scheduler.tick(
                object(),
                current_time=current + timedelta(minutes=9),
            )
            retried = await scheduler.tick(
                object(),
                current_time=current + timedelta(minutes=10),
            )

        self.assertEqual(first.status, "retryable_failure")
        self.assertEqual(waiting.status, "not_due")
        self.assertEqual(retried.status, "retryable_failure")
        self.assertEqual(export.call_count, 2)
        self.assertEqual(delivery.await_count, 2)

    async def test_terminal_or_unknown_receipt_is_not_retried_same_day(self):
        for status in ("failed", "unknown"):
            with self.subTest(status=status), TemporaryDirectory() as temp_dir:
                scheduler, export, delivery = self._scheduler(
                    Path(temp_dir),
                    receipts=(
                        _receipt(
                            status=status,
                            error_code="permission_denied",
                        ),
                    ),
                )
                current = datetime(
                    2026,
                    7,
                    27,
                    9,
                    0,
                    tzinfo=LOCAL_TIMEZONE,
                )
                first = await scheduler.tick(
                    object(),
                    current_time=current,
                )
                second = await scheduler.tick(
                    object(),
                    current_time=current + timedelta(hours=1),
                )
                self.assertEqual(first.status, "terminal_failure")
                self.assertEqual(second.status, "not_due")
                self.assertEqual(export.call_count, 1)
                self.assertEqual(delivery.await_count, 1)

    def test_exception_classifier_honors_transport_and_lock_semantics(self):
        self.assertEqual(
            classify_scheduler_exception(
                BotTransportError(
                    "quota",
                    code="429",
                    retryable=True,
                )
            ),
            ("retryable_failure", "429"),
        )
        self.assertEqual(
            classify_scheduler_exception(
                BotTransportError(
                    "permission",
                    code="permission_denied",
                    retryable=False,
                )
            ),
            ("terminal_failure", "permission_denied"),
        )
        self.assertEqual(
            classify_scheduler_exception(StarsCupDailyLocked("busy")),
            ("retryable_failure", "daily_locked"),
        )
        self.assertEqual(
            classify_scheduler_exception(StarsCupDeliveryLocked("busy")),
            ("retryable_failure", "delivery_locked"),
        )

    def test_exception_classifier_detects_wrapped_transient_failures(self):
        try:
            try:
                raise TimeoutError("Verse timed out")
            except TimeoutError as cause:
                raise StarsCupDailyError("daily export failed") from cause
        except StarsCupDailyError as error:
            classified = classify_scheduler_exception(error)
        self.assertEqual(
            classified,
            ("retryable_failure", "network_timeout"),
        )

        try:
            try:
                raise ConnectionError("connection reset")
            except ConnectionError as cause:
                raise StarsCupDailyError("daily export failed") from cause
        except StarsCupDailyError as error:
            classified = classify_scheduler_exception(error)
        self.assertEqual(
            classified,
            ("retryable_failure", "network_connection"),
        )

    def test_exception_classifier_stops_deterministic_and_unknown_failures(self):
        self.assertEqual(
            classify_scheduler_exception(BotContractError("bad payload")),
            ("terminal_failure", "contract_error"),
        )
        self.assertEqual(
            classify_scheduler_exception(StarsCupDailyError("bad artifact")),
            ("terminal_failure", "daily_error"),
        )
        self.assertEqual(
            classify_scheduler_exception(ValueError("bad configuration")),
            ("terminal_failure", "invalid_configuration"),
        )
        self.assertEqual(
            classify_scheduler_exception(RuntimeError("unexpected")),
            ("terminal_failure", "unexpected_exception"),
        )

    async def test_terminal_exception_is_not_retried_same_day(self):
        with TemporaryDirectory() as temp_dir:
            scheduler, export, delivery = self._scheduler(Path(temp_dir))
            export.side_effect = ValueError("bad configuration")
            current = datetime(
                2026,
                7,
                27,
                9,
                0,
                tzinfo=LOCAL_TIMEZONE,
            )
            first = await scheduler.tick(object(), current_time=current)
            second = await scheduler.tick(
                object(),
                current_time=current + timedelta(hours=1),
            )

        self.assertEqual(first.status, "terminal_failure")
        self.assertEqual(first.error_code, "invalid_configuration")
        self.assertEqual(second.status, "not_due")
        self.assertEqual(export.call_count, 1)
        delivery.assert_not_awaited()

    async def test_rejected_or_removed_relationship_blocks_before_export(self):
        for event_type, expected_code in (
            ("GROUP_MSG_REJECT", "relationship_rejected"),
            ("GROUP_DEL_ROBOT", "robot_removed"),
        ):
            with self.subTest(event_type=event_type), TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                scheduler, export, delivery = self._scheduler(root)
                update = official_relationship_event_to_update(
                    event_type,
                    SimpleNamespace(group_openid="opaque-test-group"),
                    observed_at=datetime(
                        2026,
                        7,
                        27,
                        8,
                        0,
                        tzinfo=LOCAL_TIMEZONE,
                    ),
                )
                observe_official_relationship_event(
                    update,
                    "opaque-test-group",
                    root / "relationship.json",
                )
                result = await scheduler.tick(
                    object(),
                    current_time=datetime(
                        2026,
                        7,
                        27,
                        9,
                        0,
                        tzinfo=LOCAL_TIMEZONE,
                    ),
                )

                self.assertEqual(result.status, "terminal_failure")
                self.assertEqual(result.error_code, expected_code)
                export.assert_not_called()
                delivery.assert_not_awaited()

    async def test_receive_relationship_clears_block_and_allows_delivery(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scheduler, export, delivery = self._scheduler(root)
            rejected = official_relationship_event_to_update(
                "GROUP_MSG_REJECT",
                SimpleNamespace(group_openid="opaque-test-group"),
                observed_at=datetime(
                    2026,
                    7,
                    27,
                    8,
                    0,
                    tzinfo=LOCAL_TIMEZONE,
                ),
            )
            observe_official_relationship_event(
                rejected,
                "opaque-test-group",
                root / "relationship.json",
            )
            received = official_relationship_event_to_update(
                "GROUP_MSG_RECEIVE",
                SimpleNamespace(group_openid="opaque-test-group"),
                observed_at=datetime(
                    2026,
                    7,
                    27,
                    8,
                    30,
                    tzinfo=LOCAL_TIMEZONE,
                ),
            )
            observe_official_relationship_event(
                received,
                "opaque-test-group",
                root / "relationship.json",
            )
            result = await scheduler.tick(
                object(),
                current_time=datetime(
                    2026,
                    7,
                    27,
                    9,
                    0,
                    tzinfo=LOCAL_TIMEZONE,
                ),
            )

        self.assertEqual(result.status, "sent")
        export.assert_called_once()
        delivery.assert_awaited_once()

    async def test_corrupt_relationship_state_is_terminal(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "relationship.json").write_text("{", encoding="utf-8")
            scheduler, export, delivery = self._scheduler(root)
            result = await scheduler.tick(
                object(),
                current_time=datetime(
                    2026,
                    7,
                    27,
                    9,
                    0,
                    tzinfo=LOCAL_TIMEZONE,
                ),
            )

        self.assertEqual(result.status, "terminal_failure")
        self.assertEqual(result.error_code, "relationship_state_invalid")
        export.assert_not_called()
        delivery.assert_not_awaited()

    @staticmethod
    def _write_delivery_state(
        root: Path,
        *,
        statuses: dict[str, str],
        target: str = "opaque-test-group",
    ) -> None:
        images = {
            kind: {
                "sha256": "{}-hash".format(kind),
                "status": status,
            }
            for kind, status in statuses.items()
        }
        path = root / "deliveries" / "20260727.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "20260727",
                    "transport": "qq_official",
                    "target_hash": hashlib.sha256(
                        target.encode("utf-8")
                    ).hexdigest(),
                    "images": images,
                }
            ),
            encoding="utf-8",
        )

    async def test_restart_reconciles_complete_delivery_without_export(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_delivery_state(
                root,
                statuses={"total": "sent", "detail": "sent"},
            )
            scheduler, export, delivery = self._scheduler(root)
            result = await scheduler.tick(
                object(),
                current_time=datetime(
                    2026,
                    7,
                    27,
                    9,
                    0,
                    tzinfo=LOCAL_TIMEZONE,
                ),
            )

        self.assertEqual(result.status, "sent")
        self.assertEqual(result.error_code, "reconciled_sent")
        export.assert_not_called()
        delivery.assert_not_awaited()

    async def test_restart_reconciles_terminal_or_unknown_without_export(self):
        for status, expected_code in (
            ("failed_terminal", "delivery_terminal"),
            ("unknown", "delivery_unknown"),
            ("sending", "delivery_unknown"),
        ):
            with self.subTest(status=status), TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self._write_delivery_state(
                    root,
                    statuses={"total": status},
                )
                scheduler, export, delivery = self._scheduler(root)
                result = await scheduler.tick(
                    object(),
                    current_time=datetime(
                        2026,
                        7,
                        27,
                        9,
                        0,
                        tzinfo=LOCAL_TIMEZONE,
                    ),
                )
                self.assertEqual(result.status, "terminal_failure")
                self.assertEqual(result.error_code, expected_code)
                export.assert_not_called()
                delivery.assert_not_awaited()

    async def test_restart_allows_partial_or_retryable_delivery_to_resume(self):
        for statuses in (
            {"total": "sent"},
            {"total": "sent", "detail": "failed_retryable"},
        ):
            with self.subTest(statuses=statuses), TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self._write_delivery_state(root, statuses=statuses)
                scheduler, export, delivery = self._scheduler(root)
                result = await scheduler.tick(
                    object(),
                    current_time=datetime(
                        2026,
                        7,
                        27,
                        9,
                        0,
                        tzinfo=LOCAL_TIMEZONE,
                    ),
                )
                self.assertEqual(result.status, "sent")
                export.assert_called_once()
                delivery.assert_awaited_once()

    async def test_restart_rejects_corrupt_or_wrong_target_delivery_state(self):
        for corrupt_kind in ("invalid_json", "wrong_target"):
            with self.subTest(corrupt_kind=corrupt_kind), TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                if corrupt_kind == "invalid_json":
                    path = root / "deliveries" / "20260727.json"
                    path.parent.mkdir(parents=True)
                    path.write_text("{", encoding="utf-8")
                else:
                    self._write_delivery_state(
                        root,
                        statuses={"total": "sent", "detail": "sent"},
                        target="different-target",
                    )
                scheduler, export, delivery = self._scheduler(root)
                result = await scheduler.tick(
                    object(),
                    current_time=datetime(
                        2026,
                        7,
                        27,
                        9,
                        0,
                        tzinfo=LOCAL_TIMEZONE,
                    ),
                )
                self.assertEqual(result.status, "terminal_failure")
                self.assertEqual(
                    result.error_code,
                    "scheduler_state_invalid",
                )
                export.assert_not_called()
                delivery.assert_not_awaited()


if __name__ == "__main__":
    main()
