from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_official_qq.app import process_official_event  # noqa: E402
from bot_official_qq.transport import (  # noqa: E402
    OfficialEventDeduplicator,
    OfficialQQTransport,
)
from services import bot_private_service as bot_business  # noqa: E402
from services.bot_transport import BotReply  # noqa: E402
from services.match_rank_image_service import (  # noqa: E402
    build_sample_snapshot,
    validate_snapshot,
)
from services.stars_cup_bot_service import LoadedStarsCupSnapshot  # noqa: E402
from settings import LOCAL_TIMEZONE  # noqa: E402
from tests.scripts.testing_support import fresh_test_connection  # noqa: E402


class FakeApi:
    def __init__(self):
        self.calls = []

    async def post_c2c_message(self, **fields):
        self.calls.append(fields)
        return {
            "id": "outgoing",
            "timestamp": "2026-07-27T10:00:00+08:00",
        }

    async def post_group_message(self, **fields):
        self.calls.append(fields)
        return {
            "id": "outgoing-group",
            "timestamp": "2026-07-27T10:00:00+08:00",
        }


class OfficialQQAppTests(IsolatedAsyncioTestCase):
    def _event(self):
        return {
            "id": "incoming",
            "author": {"user_openid": "opaque-user"},
            "content": "help",
            "message_scene": {"ext": ["msg_idx=index-1"]},
        }

    async def test_processor_dispatches_through_neutral_business_boundary(self):
        api = FakeApi()
        transport = OfficialQQTransport(api)
        connection = object()
        with patch(
            "bot_official_qq.app.dispatch_business_message",
            return_value=BotReply(text="官方回复"),
        ) as dispatch:
            outcome = await process_official_event(
                connection,
                event_type="C2C_MESSAGE_CREATE",
                event=self._event(),
                transport=transport,
            )

        self.assertEqual(outcome.inbound.address.transport, "qq_official")
        self.assertEqual(outcome.reply.text, "官方回复")
        self.assertEqual(outcome.receipts[0].status, "sent")
        dispatch.assert_called_once_with(connection, outcome.inbound)
        self.assertEqual(api.calls[0]["openid"], "opaque-user")
        self.assertEqual(api.calls[0]["msg_id"], "incoming")

    async def test_duplicate_is_ignored_before_business_or_send(self):
        api = FakeApi()
        transport = OfficialQQTransport(api)
        deduplicator = OfficialEventDeduplicator()
        event = self._event()
        with patch(
            "bot_official_qq.app.dispatch_business_message",
            return_value=BotReply(text="reply"),
        ) as dispatch:
            first = await process_official_event(
                object(),
                event_type="C2C_MESSAGE_CREATE",
                event=event,
                transport=transport,
                deduplicator=deduplicator,
            )
            second = await process_official_event(
                object(),
                event_type="C2C_MESSAGE_CREATE",
                event=event,
                transport=transport,
                deduplicator=deduplicator,
            )

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertTrue(second.ignored)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(len(api.calls), 1)

    async def test_unsupported_event_is_ignored_without_side_effects(self):
        api = FakeApi()
        with patch(
            "bot_official_qq.app.dispatch_business_message"
        ) as dispatch:
            outcome = await process_official_event(
                object(),
                event_type="READY",
                event={"id": "event"},
                transport=OfficialQQTransport(api),
            )

        self.assertTrue(outcome.ignored)
        self.assertIsNone(outcome.inbound)
        dispatch.assert_not_called()
        self.assertEqual(api.calls, [])

    async def test_group_business_error_uses_existing_silent_policy(self):
        api = FakeApi()
        event = {
            "id": "incoming",
            "group_openid": "opaque-group",
            "author": {"member_openid": "opaque-member"},
            "content": "help",
        }
        with patch(
            "bot_official_qq.app.dispatch_business_message",
            side_effect=RuntimeError("sensitive internal path"),
        ):
            outcome = await process_official_event(
                object(),
                event_type="GROUP_AT_MESSAGE_CREATE",
                event=event,
                transport=OfficialQQTransport(api),
            )

        self.assertIsNone(outcome.reply)
        self.assertEqual(outcome.receipts, ())
        self.assertEqual(api.calls, [])

    async def test_official_group_event_reaches_real_stars_cup_snapshot_query(self):
        api = FakeApi()
        event = {
            "id": "incoming-stars-cup",
            "group_openid": "opaque-stars-group",
            "author": {"member_openid": "opaque-member"},
            "content": "/群星杯 A",
            "timestamp": "2026-07-27T10:00:00+08:00",
        }
        loaded = LoadedStarsCupSnapshot(
            snapshot=validate_snapshot(build_sample_snapshot()),
            snapshot_path=Path("/exports/榜图数据.json"),
            snapshot_sha256="a" * 64,
            image_paths={
                "total": Path("/exports/总榜.png"),
                "detail": Path("/exports/六队明细.png"),
            },
            image_sha256={"total": "b" * 64, "detail": "c" * 64},
            run_id="20260727",
            published_at=datetime(
                2026,
                7,
                27,
                9,
                0,
                tzinfo=LOCAL_TIMEZONE,
            ),
            age_seconds=3600,
            stale=False,
        )
        original_enabled = bot_business.GROUP_CHAT_ENABLED
        original_whitelist = set(bot_business.GROUP_CHAT_WHITELIST)
        try:
            bot_business.GROUP_CHAT_ENABLED = True
            bot_business.GROUP_CHAT_WHITELIST.clear()
            bot_business.GROUP_CHAT_WHITELIST.add("opaque-stars-group")
            bot_business.GROUP_RATE_LIMIT_STATE.clear()
            bot_business.GROUP_GLOBAL_RATE_LIMIT_STATE.clear()
            with fresh_test_connection() as connection, patch(
                "services.bot_private_service.load_latest_stars_cup_snapshot",
                return_value=loaded,
            ):
                outcome = await process_official_event(
                    connection,
                    event_type="GROUP_AT_MESSAGE_CREATE",
                    event=event,
                    transport=OfficialQQTransport(api),
                )
        finally:
            bot_business.GROUP_CHAT_ENABLED = original_enabled
            bot_business.GROUP_CHAT_WHITELIST.clear()
            bot_business.GROUP_CHAT_WHITELIST.update(original_whitelist)
            bot_business.GROUP_RATE_LIMIT_STATE.clear()
            bot_business.GROUP_GLOBAL_RATE_LIMIT_STATE.clear()

        self.assertIn("A队 星河", outcome.reply.text)
        self.assertEqual(outcome.receipts[0].status, "sent")
        self.assertEqual(api.calls[0]["group_openid"], "opaque-stars-group")
        self.assertEqual(api.calls[0]["msg_id"], "incoming-stars-cup")
        self.assertEqual(api.calls[0]["msg_type"], 0)


if __name__ == "__main__":
    main()
