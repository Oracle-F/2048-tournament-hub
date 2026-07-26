from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase, main
from unittest.mock import patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.bot_transport import BotTransportError, SendReceipt  # noqa: E402
from services.match_rank_image_service import build_sample_snapshot  # noqa: E402
from services.stars_cup_daily_service import (  # noqa: E402
    DailyRunResult,
    StarsCupDailyError,
    StarsCupDailyLocked,
    StarsCupDeliveryLocked,
    deliver_stars_cup_daily_images,
    run_stars_cup_daily_export,
)
from settings import LOCAL_TIMEZONE  # noqa: E402


RUN_TIME = datetime(2026, 7, 27, 18, 0, tzinfo=LOCAL_TIMEZONE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_valid_export(output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=False)
    snapshot = output_dir / "榜图数据.json"
    total = output_dir / "总榜.png"
    detail = output_dir / "六队明细.png"
    snapshot.write_text(
        json.dumps(build_sample_snapshot(), ensure_ascii=False),
        encoding="utf-8",
    )
    Image.new("RGB", (1920, 1080), "#19324a").save(total, format="PNG")
    Image.new("RGB", (1920, 1080), "#28415a").save(detail, format="PNG")
    return {
        "snapshot": str(snapshot),
        "total": str(total),
        "detail": str(detail),
    }


def _query_into_staged_cache(
    roster_path,
    *,
    cache_path,
    workers,
    full,
):
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "last_successful_query_at": RUN_TIME.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return {
        "cache": str(cache_path),
        "mode": "recent_24h",
        "workers": workers,
        "full": full,
    }


class StarsCupDailyExportTests(TestCase):
    def test_success_publishes_only_after_three_artifacts_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_root = root / "exports"
            state_root = root / "state"
            cache = root / "live_cache.json"
            latest = root / "latest.json"
            roster = root / "roster.json"
            background = root / "background.png"
            cache.write_text('{"old": true}', encoding="utf-8")
            with patch(
                "services.stars_cup_daily_service.query_live_scores",
                side_effect=_query_into_staged_cache,
            ) as query, patch(
                "services.stars_cup_daily_service.export_rank_images",
                side_effect=lambda _roster, _db, output, _background, _as_of, **_kwargs: _write_valid_export(output),
            ) as export:
                result = run_stars_cup_daily_export(
                    run_time=RUN_TIME,
                    roster_path=roster,
                    live_cache_path=cache,
                    background_path=background,
                    export_root=export_root,
                    state_root=state_root,
                    latest_pointer_path=latest,
                    workers=3,
                )

            pointer = json.loads(latest.read_text(encoding="utf-8"))
            state = json.loads(result.state_path.read_text(encoding="utf-8"))
            canonical_cache = json.loads(cache.read_text(encoding="utf-8"))

        self.assertEqual(result.run_id, "20260727")
        self.assertFalse(result.reused)
        self.assertEqual(pointer["snapshot"]["sha256"], result.snapshot_sha256)
        self.assertEqual(pointer["images"]["total"]["sha256"], result.image_sha256["total"])
        self.assertEqual(state["status"], "published")
        self.assertEqual(canonical_cache["schema_version"], 2)
        self.assertEqual(result.output_dir.name, "20260727_180000")
        query.assert_called_once()
        export.assert_called_once()

    def test_failed_render_preserves_previous_cache_and_latest_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_root = root / "exports"
            state_root = root / "state"
            cache = root / "live_cache.json"
            latest = root / "latest.json"
            cache.write_bytes(b'{"known":"good"}')
            latest.write_bytes(b'{"known":"latest"}')
            with patch(
                "services.stars_cup_daily_service.query_live_scores",
                side_effect=_query_into_staged_cache,
            ), patch(
                "services.stars_cup_daily_service.export_rank_images",
                side_effect=RuntimeError("forced render failure"),
            ):
                with self.assertRaisesRegex(
                    StarsCupDailyError,
                    "forced render failure",
                ):
                    run_stars_cup_daily_export(
                        run_time=RUN_TIME,
                        roster_path=root / "roster.json",
                        live_cache_path=cache,
                        background_path=root / "background.png",
                        export_root=export_root,
                        state_root=state_root,
                        latest_pointer_path=latest,
                    )

            failed_state = json.loads(
                (state_root / "runs" / "20260727.json").read_text(encoding="utf-8")
            )

            self.assertEqual(cache.read_bytes(), b'{"known":"good"}')
            self.assertEqual(latest.read_bytes(), b'{"known":"latest"}')
            self.assertEqual(failed_state["status"], "failed")
            self.assertEqual(failed_state["error_type"], "RuntimeError")

    def test_same_day_published_run_is_reused_without_query_or_render(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kwargs = {
                "run_time": RUN_TIME,
                "roster_path": root / "roster.json",
                "live_cache_path": root / "cache.json",
                "background_path": root / "background.png",
                "export_root": root / "exports",
                "state_root": root / "state",
                "latest_pointer_path": root / "latest.json",
            }
            with patch(
                "services.stars_cup_daily_service.query_live_scores",
                side_effect=_query_into_staged_cache,
            ), patch(
                "services.stars_cup_daily_service.export_rank_images",
                side_effect=lambda _roster, _db, output, _background, _as_of, **_kwargs: _write_valid_export(output),
            ):
                first = run_stars_cup_daily_export(**kwargs)
            with patch(
                "services.stars_cup_daily_service.query_live_scores",
                side_effect=AssertionError("query must not run"),
            ), patch(
                "services.stars_cup_daily_service.export_rank_images",
                side_effect=AssertionError("render must not run"),
            ):
                second = run_stars_cup_daily_export(
                    **{**kwargs, "run_time": RUN_TIME.replace(hour=23)}
                )

        self.assertTrue(second.reused)
        self.assertEqual(second.output_dir, first.output_dir)
        self.assertEqual(second.snapshot_sha256, first.snapshot_sha256)

    def test_existing_lock_rejects_concurrent_run_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            lock = state_root / "daily.lock"
            lock.parent.mkdir(parents=True)
            lock.write_text("other-process", encoding="utf-8")

            with self.assertRaises(StarsCupDailyLocked):
                run_stars_cup_daily_export(
                    run_time=RUN_TIME,
                    roster_path=root / "roster.json",
                    live_cache_path=root / "cache.json",
                    background_path=root / "background.png",
                    export_root=root / "exports",
                    state_root=state_root,
                    latest_pointer_path=root / "latest.json",
                )

            self.assertEqual(lock.read_text(encoding="utf-8"), "other-process")
            self.assertFalse((state_root / "runs").exists())

    def test_wrong_png_size_never_publishes_latest(self):
        def bad_export(_roster, _db, output, _background, _as_of, **_kwargs):
            paths = _write_valid_export(output)
            Image.new("RGB", (100, 100), "black").save(paths["detail"], format="PNG")
            return paths

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest.json"
            with patch(
                "services.stars_cup_daily_service.query_live_scores",
                side_effect=_query_into_staged_cache,
            ), patch(
                "services.stars_cup_daily_service.export_rank_images",
                side_effect=bad_export,
            ):
                with self.assertRaisesRegex(StarsCupDailyError, "尺寸错误"):
                    run_stars_cup_daily_export(
                        run_time=RUN_TIME,
                        roster_path=root / "roster.json",
                        live_cache_path=root / "cache.json",
                        background_path=root / "background.png",
                        export_root=root / "exports",
                        state_root=root / "state",
                        latest_pointer_path=latest,
                    )

            self.assertFalse(latest.exists())


class FakeTransport:
    transport_name = "fake"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.sent_names = []

    async def send_group(self, group_id, reply):
        self.sent_names.append(reply.attachments[0].name)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return (outcome,)


def _delivery_result(root: Path) -> DailyRunResult:
    output = root / "exports" / "20260727_180000"
    output.mkdir(parents=True)
    total = output / "总榜.png"
    detail = output / "六队明细.png"
    Image.new("RGB", (1920, 1080), "#19324a").save(total, format="PNG")
    Image.new("RGB", (1920, 1080), "#28415a").save(detail, format="PNG")
    snapshot = output / "榜图数据.json"
    snapshot.write_text("{}", encoding="utf-8")
    return DailyRunResult(
        run_id="20260727",
        published_at=RUN_TIME,
        output_dir=output,
        snapshot_path=snapshot,
        snapshot_sha256=_sha256(snapshot),
        image_paths={"total": total, "detail": detail},
        image_sha256={"total": _sha256(total), "detail": _sha256(detail)},
        state_path=root / "state.json",
        latest_pointer_path=root / "latest.json",
        query_summary={},
    )


def _sent(message_id: str) -> SendReceipt:
    return SendReceipt(
        transport="fake",
        target="group",
        status="sent",
        platform_message_id=message_id,
        sent_at=RUN_TIME,
    )


class StarsCupDailyDeliveryTests(IsolatedAsyncioTestCase):
    async def test_existing_delivery_lock_prevents_duplicate_send(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _delivery_result(root)
            state_path = root / "delivery.json"
            lock_path = root / "delivery.json.lock"
            lock_path.write_text("other-process", encoding="utf-8")
            transport = FakeTransport([_sent("should-not-send")])

            with self.assertRaises(StarsCupDeliveryLocked):
                await deliver_stars_cup_daily_images(
                    result,
                    transport,
                    group_id="opaque-group",
                    delivery_state_path=state_path,
                    dry_run=False,
                    attempt_time=RUN_TIME,
                )

            self.assertEqual(transport.sent_names, [])
            self.assertEqual(lock_path.read_text(encoding="utf-8"), "other-process")

    async def test_dry_run_sends_nothing_and_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _delivery_result(root)
            transport = FakeTransport([])
            delivery_state = root / "delivery.json"

            delivered = await deliver_stars_cup_daily_images(
                result,
                transport,
                group_id="opaque-group",
                delivery_state_path=delivery_state,
                dry_run=True,
                attempt_time=RUN_TIME,
            )

            self.assertEqual(
                [receipt.status for receipt in delivered.receipts],
                ["dry_run", "dry_run"],
            )
            self.assertEqual(transport.sent_names, [])
            self.assertFalse(delivery_state.exists())

    async def test_partial_retry_sends_only_the_second_image_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _delivery_result(root)
            state_path = root / "delivery.json"
            first_transport = FakeTransport(
                [
                    _sent("message-1"),
                    BotTransportError(
                        "quota",
                        code="quota",
                        retryable=True,
                    ),
                ]
            )

            first = await deliver_stars_cup_daily_images(
                result,
                first_transport,
                group_id="opaque-group",
                delivery_state_path=state_path,
                dry_run=False,
                attempt_time=RUN_TIME,
            )
            retry_transport = FakeTransport([_sent("message-2")])
            second = await deliver_stars_cup_daily_images(
                result,
                retry_transport,
                group_id="opaque-group",
                delivery_state_path=state_path,
                dry_run=False,
                attempt_time=RUN_TIME.replace(minute=5),
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(first_transport.sent_names, ["总榜.png", "六队明细.png"])
        self.assertEqual([receipt.status for receipt in first.receipts], ["sent", "failed"])
        self.assertEqual(retry_transport.sent_names, ["六队明细.png"])
        self.assertEqual([receipt.status for receipt in second.receipts], ["sent"])
        self.assertEqual(state["images"]["total"]["status"], "sent")
        self.assertEqual(state["images"]["detail"]["status"], "sent")
        self.assertNotIn("opaque-group", json.dumps(state))

    async def test_terminal_rejection_stops_before_second_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _delivery_result(root)
            transport = FakeTransport(
                [
                    BotTransportError(
                        "permission denied",
                        code="permission",
                        retryable=False,
                    )
                ]
            )
            state_path = root / "delivery.json"

            delivered = await deliver_stars_cup_daily_images(
                result,
                transport,
                group_id="opaque-group",
                delivery_state_path=state_path,
                dry_run=False,
                attempt_time=RUN_TIME,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(transport.sent_names, ["总榜.png"])
        self.assertEqual(delivered.receipts[0].status, "failed")
        self.assertFalse(delivered.receipts[0].retryable)
        self.assertEqual(state["images"]["total"]["status"], "failed_terminal")
        self.assertNotIn("detail", state["images"])

    async def test_interrupted_sending_state_becomes_unknown_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _delivery_result(root)
            state_path = root / "delivery.json"
            target_hash = hashlib.sha256(b"opaque-group").hexdigest()
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": result.run_id,
                        "transport": "fake",
                        "target_hash": target_hash,
                        "images": {
                            "total": {
                                "sha256": result.image_sha256["total"],
                                "status": "sending",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            transport = FakeTransport([])

            delivered = await deliver_stars_cup_daily_images(
                result,
                transport,
                group_id="opaque-group",
                delivery_state_path=state_path,
                dry_run=False,
                attempt_time=RUN_TIME,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(transport.sent_names, [])
        self.assertEqual(delivered.receipts[0].status, "unknown")
        self.assertEqual(state["images"]["total"]["status"], "unknown")


if __name__ == "__main__":
    main()
