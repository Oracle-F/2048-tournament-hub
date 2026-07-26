from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase, main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_private_qq.onebot_transport import (  # noqa: E402
    hydrate_file_segment,
    onebot_event_to_inbound,
    render_onebot_reply,
    segment_debug_summary,
    send_onebot_reply,
)
from services.bot_transport import BotAttachment, BotReply  # noqa: E402


class _Segment:
    def __init__(self, segment_type, data=None):
        self.type = segment_type
        self.data = dict(data or {})


class _Event:
    def __init__(
        self,
        *,
        user_id="10001",
        group_id=None,
        message_id="message-1",
        segments=(),
        to_me=False,
    ):
        self._user_id = user_id
        self.group_id = group_id
        self.message_id = message_id
        self.self_id = "90001"
        self.time = 1785124800
        self.to_me = to_me
        self._segments = list(segments)

    def get_user_id(self):
        return self._user_id

    def get_message(self):
        return self._segments


class _Bot:
    self_id = "90001"

    def __init__(self, *, api_results=None):
        self.api_results = list(api_results or [])
        self.api_calls = []
        self.sent = []

    async def call_api(self, api_name, **params):
        self.api_calls.append((api_name, params))
        if not self.api_results:
            return {}
        result = self.api_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def send(self, event, message):
        self.sent.append((event, message))
        return {"message_id": "sent-1"}


class OneBotTransportMappingTests(IsolatedAsyncioTestCase):
    async def test_slash_group_command_becomes_mentioned_neutral_message(self):
        bot = _Bot()
        event = _Event(
            group_id="20001",
            segments=(_Segment("text", {"text": "/群星杯 A"}),),
        )

        inbound = await onebot_event_to_inbound(bot, event, "/群星杯 A")

        self.assertEqual(inbound.address.transport, "onebot_v11")
        self.assertEqual(inbound.address.conversation_kind, "group")
        self.assertEqual(inbound.address.conversation_id, "20001")
        self.assertEqual(inbound.text, "群星杯 A")
        self.assertTrue(inbound.mentioned)

    async def test_text_mention_and_reply_are_preserved_without_sdk_objects(self):
        bot = _Bot()
        event = _Event(
            group_id="20001",
            segments=(
                _Segment("reply", {"id": "quoted-1"}),
                _Segment("text", {"text": "@maomaoBot 群星杯"}),
            ),
        )

        inbound = await onebot_event_to_inbound(
            bot,
            event,
            "@maomaoBot 群星杯",
            text_at_names=("maomaoBot",),
        )

        self.assertEqual(inbound.text, "群星杯")
        self.assertEqual(inbound.reference_id, "quoted-1")
        self.assertTrue(inbound.mentioned)

    async def test_file_segment_hydration_becomes_neutral_attachment(self):
        bot = _Bot(api_results=[{"data": {"url": "https://example.test/replay.txt"}}])
        event = _Event(
            segments=(
                _Segment(
                    "file",
                    {"file_id": "file-1", "name": "replay.txt"},
                ),
            ),
        )

        inbound = await onebot_event_to_inbound(bot, event, "")

        self.assertEqual(len(inbound.attachments), 1)
        self.assertEqual(inbound.attachments[0].kind, "file")
        self.assertEqual(inbound.attachments[0].url, "https://example.test/replay.txt")
        self.assertEqual(bot.api_calls[0], ("get_file", {"file_id": "file-1"}))

    async def test_file_hydration_timeout_falls_through_to_later_api(self):
        bot = _Bot(
            api_results=[
                asyncio.TimeoutError(),
                {"url": "https://example.test/replay.txt"},
            ]
        )

        result = await hydrate_file_segment(
            bot,
            {"type": "file", "data": {"file_id": "file-1"}},
            timeout_seconds=0.1,
        )

        self.assertEqual(result["data"]["url"], "https://example.test/replay.txt")
        self.assertEqual(len(bot.api_calls), 2)

    async def test_send_returns_receipt_and_preserves_image(self):
        bot = _Bot()
        event = _Event(group_id="20001")
        inbound = await onebot_event_to_inbound(bot, event, "/群星杯")
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "总榜.png"
            reply = BotReply(
                text="今日榜图",
                attachments=(BotAttachment(kind="image", local_path=image_path),),
            )

            receipts = await send_onebot_reply(bot, event, inbound, reply)

        self.assertEqual(receipts[0].platform_message_id, "sent-1")
        self.assertEqual(receipts[0].target, "20001")
        rendered = str(bot.sent[0][1])
        self.assertIn("今日榜图", rendered)
        self.assertIn("[CQ:image", rendered)


class OneBotTransportRenderingTests(TestCase):
    def test_debug_summary_does_not_dump_file_payloads(self):
        summary = segment_debug_summary(
            [
                {"type": "at", "data": {"qq": "90001"}},
                {"type": "text", "data": {"text": "群星杯 A"}},
                {"type": "file", "data": {"url": "https://secret.example/file"}},
            ]
        )

        self.assertEqual(summary, "at:90001|text:群星杯 A|file")
        self.assertNotIn("https://", summary)

    def test_renderer_rejects_non_contract_reply(self):
        with self.assertRaisesRegex(ValueError, "BotReply"):
            render_onebot_reply("legacy text")


if __name__ == "__main__":
    main()
