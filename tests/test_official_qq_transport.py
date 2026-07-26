from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase, main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_official_qq.transport import (  # noqa: E402
    BotpyUrlMediaUploader,
    OfficialEventDeduplicator,
    OfficialQQTransport,
    official_event_dedupe_key,
    official_event_to_inbound,
    send_official_proactive_group,
    send_official_reply,
)
from services.bot_transport import (  # noqa: E402
    BotAttachment,
    BotContractError,
    BotReply,
)


class OfficialEventMappingTests(TestCase):
    def test_group_at_event_maps_opaque_ids_text_time_and_attachments(self):
        inbound = official_event_to_inbound(
            "GROUP_AT_MESSAGE_CREATE",
            {
                "id": "outer-event",
                "d": {
                    "id": "group-message",
                    "group_openid": "opaque-group",
                    "author": {
                        "id": "fallback-id",
                        "member_openid": "opaque-member",
                    },
                    "content": " /群星杯 A ",
                    "timestamp": "2026-07-27T10:00:00+08:00",
                    "attachments": [
                        {
                            "filename": "board.png",
                            "content_type": "image/png",
                            "url": "https://example.test/board.png",
                        },
                        {
                            "filename": "voice.silk",
                            "content_type": "voice",
                            "url": "https://example.test/voice.silk",
                            "voice_wav_url": "https://example.test/voice.wav",
                        },
                    ],
                },
            },
        )

        self.assertEqual(inbound.address.transport, "qq_official")
        self.assertEqual(inbound.address.conversation_kind, "group")
        self.assertEqual(inbound.address.conversation_id, "opaque-group")
        self.assertEqual(inbound.address.user_id, "opaque-member")
        self.assertEqual(inbound.text, " /群星杯 A ")
        self.assertTrue(inbound.mentioned)
        self.assertEqual(inbound.message_id, "group-message")
        self.assertEqual(inbound.event_id, "outer-event")
        self.assertEqual(
            [(item.kind, item.url) for item in inbound.attachments],
            [
                ("image", "https://example.test/board.png"),
                ("audio", "https://example.test/voice.wav"),
            ],
        )
        self.assertEqual(inbound.received_at.isoformat(), "2026-07-27T10:00:00+08:00")

    def test_c2c_event_maps_user_openid_and_explicit_reference_only(self):
        inbound = official_event_to_inbound(
            "c2c_message_create",
            {
                "id": "c2c-message",
                "author": {"user_openid": "opaque-user"},
                "content": "我的成绩",
                "timestamp": "2026-07-27T10:00:00+08:00",
                "message_reference": {"message_id": "quoted-message"},
                "message_scene": {
                    "ext": ["msg_idx=opaque-index", "ref_msg_idx=another-index"]
                },
            },
        )

        self.assertEqual(inbound.address.conversation_kind, "private")
        self.assertEqual(inbound.address.conversation_id, "opaque-user")
        self.assertEqual(inbound.address.user_id, "opaque-user")
        self.assertFalse(inbound.mentioned)
        self.assertEqual(inbound.reference_id, "quoted-message")
        self.assertNotEqual(inbound.reference_id, "another-index")

    def test_unsupported_event_is_ignored_and_missing_openid_is_rejected(self):
        self.assertIsNone(official_event_to_inbound("READY", {"id": "event"}))
        with self.assertRaisesRegex(BotContractError, "OpenID"):
            official_event_to_inbound(
                "GROUP_AT_MESSAGE_CREATE",
                {"id": "message", "author": {}},
            )

    def test_deduplicator_uses_message_id_and_message_index_with_a_bound(self):
        deduplicator = OfficialEventDeduplicator(max_entries=2)
        first = {
            "id": "message-1",
            "message_scene": {"ext": ["msg_idx=index-1"]},
        }
        same_id_new_index = {
            "id": "message-1",
            "message_scene": {"ext": ["msg_idx=index-2"]},
        }
        second = {"id": "message-2"}
        third = {"id": "message-3"}

        self.assertEqual(
            official_event_dedupe_key("C2C_MESSAGE_CREATE", first),
            "C2C_MESSAGE_CREATE:message-1:index-1",
        )
        self.assertTrue(deduplicator.accept("C2C_MESSAGE_CREATE", first))
        self.assertFalse(deduplicator.accept("C2C_MESSAGE_CREATE", first))
        self.assertTrue(
            deduplicator.accept("C2C_MESSAGE_CREATE", same_id_new_index)
        )
        self.assertTrue(deduplicator.accept("C2C_MESSAGE_CREATE", second))
        self.assertTrue(deduplicator.accept("C2C_MESSAGE_CREATE", third))
        self.assertTrue(deduplicator.accept("C2C_MESSAGE_CREATE", first))


class FakeApiError(RuntimeError):
    def __init__(self, code):
        super().__init__("api error {}".format(code))
        self.code = code


class FakeApi:
    def __init__(self):
        self.calls = []
        self.outcomes = []

    def queue(self, *outcomes):
        self.outcomes.extend(outcomes)

    async def _call(self, method, fields):
        self.calls.append((method, fields))
        outcome = self.outcomes.pop(0) if self.outcomes else {
            "id": "message-{}".format(len(self.calls)),
            "timestamp": "2026-07-27T10:00:00+08:00",
        }
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def post_group_message(self, **fields):
        return await self._call("post_group_message", fields)

    async def post_c2c_message(self, **fields):
        return await self._call("post_c2c_message", fields)

    async def post_group_file(self, **fields):
        return await self._call("post_group_file", fields)

    async def post_c2c_file(self, **fields):
        return await self._call("post_c2c_file", fields)


class FakeLocalUploader:
    def __init__(self):
        self.calls = []

    async def upload(
        self,
        api,
        *,
        conversation_kind,
        conversation_id,
        attachment,
    ):
        self.calls.append(
            (conversation_kind, conversation_id, attachment.local_path)
        )
        return {"file_info": "uploaded-{}".format(attachment.local_path.name)}


class OfficialSendTests(IsolatedAsyncioTestCase):
    async def test_group_passive_reply_sends_text_then_image_with_unique_sequences(self):
        api = FakeApi()
        uploader = FakeLocalUploader()
        inbound = official_event_to_inbound(
            "GROUP_AT_MESSAGE_CREATE",
            {
                "id": "incoming-message",
                "group_openid": "opaque-group",
                "author": {"member_openid": "opaque-member"},
                "content": "help",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "help.png"
            image.write_bytes(b"image")
            receipts = await send_official_reply(
                api,
                inbound,
                BotReply(
                    text="帮助",
                    attachments=(
                        BotAttachment(kind="image", local_path=image),
                    ),
                    reference_id="quoted-message",
                ),
                uploader=uploader,
            )

        self.assertEqual([receipt.status for receipt in receipts], ["sent", "sent"])
        self.assertEqual(
            [call[0] for call in api.calls],
            ["post_group_message", "post_group_message"],
        )
        first = api.calls[0][1]
        second = api.calls[1][1]
        self.assertEqual(first["group_openid"], "opaque-group")
        self.assertEqual(first["msg_type"], 0)
        self.assertEqual(first["msg_id"], "incoming-message")
        self.assertEqual(first["msg_seq"], 1)
        self.assertEqual(first["message_reference"], {"message_id": "quoted-message"})
        self.assertEqual(second["msg_type"], 7)
        self.assertEqual(second["msg_seq"], 2)
        self.assertEqual(second["media"], {"file_info": "uploaded-help.png"})
        self.assertEqual(uploader.calls[0][:2], ("group", "opaque-group"))

    async def test_c2c_url_upload_uses_c2c_scene_and_never_direct_sends(self):
        api = FakeApi()
        api.queue(
            {"file_info": "c2c-file-info"},
            {
                "id": "sent-c2c-image",
                "timestamp": "2026-07-27T10:00:00+08:00",
            },
        )
        inbound = official_event_to_inbound(
            "C2C_MESSAGE_CREATE",
            {
                "id": "incoming-c2c",
                "author": {"user_openid": "opaque-user"},
                "content": "图",
            },
        )

        receipts = await send_official_reply(
            api,
            inbound,
            BotReply(
                attachments=(
                    BotAttachment(
                        kind="image",
                        url="https://example.test/rank.png",
                    ),
                )
            ),
            uploader=BotpyUrlMediaUploader(),
        )

        self.assertEqual(receipts[0].status, "sent")
        self.assertEqual(
            [call[0] for call in api.calls],
            ["post_c2c_file", "post_c2c_message"],
        )
        upload = api.calls[0][1]
        self.assertEqual(upload["openid"], "opaque-user")
        self.assertEqual(upload["file_type"], 1)
        self.assertFalse(upload["srv_send_msg"])
        self.assertEqual(api.calls[1][1]["media"], {"file_info": "c2c-file-info"})

    async def test_proactive_group_image_has_no_passive_message_or_event_id(self):
        api = FakeApi()
        uploader = FakeLocalUploader()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "总榜.png"
            image.write_bytes(b"image")
            transport = OfficialQQTransport(api, uploader=uploader)
            receipts = await transport.send_group(
                "opaque-group",
                BotReply(
                    attachments=(
                        BotAttachment(kind="image", local_path=image),
                    )
                ),
            )

        fields = api.calls[0][1]
        self.assertEqual(receipts[0].status, "sent")
        self.assertEqual(fields["group_openid"], "opaque-group")
        self.assertEqual(fields["msg_type"], 7)
        self.assertNotIn("msg_id", fields)
        self.assertNotIn("event_id", fields)
        self.assertEqual(fields["media"], {"file_info": "uploaded-总榜.png"})

    async def test_local_media_without_injected_uploader_fails_before_api_send(self):
        api = FakeApi()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "rank.png"
            image.write_bytes(b"image")
            receipts = await send_official_proactive_group(
                api,
                "opaque-group",
                BotReply(
                    attachments=(
                        BotAttachment(kind="image", local_path=image),
                    )
                ),
            )

        self.assertEqual(receipts[0].status, "failed")
        self.assertEqual(
            receipts[0].error_code,
            "local_media_upload_unconfigured",
        )
        self.assertFalse(receipts[0].retryable)
        self.assertEqual(api.calls, [])

    async def test_quota_is_retryable_permission_is_terminal_and_unknown_is_unknown(self):
        async def one_outcome(outcome):
            api = FakeApi()
            api.queue(outcome)
            return await send_official_proactive_group(
                api,
                "opaque-group",
                BotReply(text="daily"),
            )

        quota = await one_outcome(FakeApiError(40034100))
        permission = await one_outcome(FakeApiError(40034105))
        unknown = await one_outcome(RuntimeError("connection dropped"))

        self.assertEqual(quota[0].status, "failed")
        self.assertTrue(quota[0].retryable)
        self.assertEqual(permission[0].status, "failed")
        self.assertFalse(permission[0].retryable)
        self.assertEqual(unknown[0].status, "unknown")
        self.assertFalse(unknown[0].retryable)

    async def test_missing_passive_context_is_rejected_without_api_call(self):
        api = FakeApi()
        inbound = official_event_to_inbound(
            "C2C_MESSAGE_CREATE",
            {
                "author": {"user_openid": "opaque-user"},
                "content": "hello",
            },
        )

        with self.assertRaisesRegex(BotContractError, "msg_id or event_id"):
            await send_official_reply(api, inbound, BotReply(text="reply"))

        self.assertEqual(api.calls, [])

    async def test_passive_part_limit_is_checked_before_partial_send(self):
        api = FakeApi()
        inbound = official_event_to_inbound(
            "C2C_MESSAGE_CREATE",
            {
                "id": "incoming-c2c",
                "author": {"user_openid": "opaque-user"},
                "content": "many",
            },
        )
        attachments = tuple(
            BotAttachment(
                kind="image",
                platform_file_id="file-{}".format(index),
            )
            for index in range(4)
        )

        receipts = await send_official_reply(
            api,
            inbound,
            BotReply(text="text-part", attachments=attachments),
        )

        self.assertEqual(receipts[0].status, "failed")
        self.assertEqual(
            receipts[0].error_code,
            "reply_part_limit_exceeded",
        )
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    main()
