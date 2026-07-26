from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.bot_transport import (  # noqa: E402
    BotAddress,
    BotAttachment,
    BotContractError,
    BotInboundMessage,
    BotReply,
    SendReceipt,
    bot_platform_for_transport,
    dispatch_business_message,
    normalize_legacy_reply,
)


class BotTransportContractTests(TestCase):
    def test_address_preserves_opaque_official_ids(self):
        address = BotAddress(
            transport="qq_official",
            conversation_kind="group",
            conversation_id="B2C3D4E5_openid",
            user_id="A1B2C3D4_user_openid",
        )

        self.assertEqual(address.conversation_id, "B2C3D4E5_openid")
        self.assertEqual(address.user_id, "A1B2C3D4_user_openid")
        self.assertEqual(bot_platform_for_transport(address.transport), "qq_official")
        self.assertEqual(bot_platform_for_transport("onebot_v11"), "qq")

    def test_contract_rejects_empty_address_and_source_free_attachment(self):
        with self.assertRaises(BotContractError):
            BotAddress(
                transport="",
                conversation_kind="private",
                conversation_id="user",
                user_id="user",
            )
        with self.assertRaises(BotContractError):
            BotAttachment(kind="image")
        with self.assertRaisesRegex(BotContractError, "BotAttachment"):
            BotReply(attachments=(object(),))

    def test_receipt_keeps_retry_classification(self):
        receipt = SendReceipt(
            transport="qq_official",
            target="group-openid",
            status="failed",
            error_code="40034100",
            retryable=True,
        )

        self.assertEqual(receipt.error_code, "40034100")
        self.assertTrue(receipt.retryable)

    def test_plain_legacy_reply_becomes_text_reply(self):
        reply = normalize_legacy_reply(" 群星杯数据截至 12:00 ")

        self.assertEqual(reply, BotReply(text="群星杯数据截至 12:00"))

    def test_multiple_legacy_images_keep_source_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            total = root / "总榜.png"
            detail = root / "六队明细.png"
            legacy = "今日榜图\n[CQ:image,file={}]\n[CQ:image,file={}]".format(
                total.as_uri(),
                detail.as_uri(),
            )

            reply = normalize_legacy_reply(legacy)

        self.assertEqual(reply.text, "今日榜图")
        self.assertEqual(
            [attachment.local_path for attachment in reply.attachments],
            [total, detail],
        )

    def test_http_legacy_image_becomes_url_attachment(self):
        reply = normalize_legacy_reply(
            "[CQ:image,file=https://example.test/rank.png]"
        )

        self.assertEqual(reply.text, "")
        self.assertEqual(reply.attachments[0].url, "https://example.test/rank.png")

    def test_unsupported_legacy_image_scheme_is_rejected(self):
        with self.assertRaisesRegex(BotContractError, "unsupported"):
            normalize_legacy_reply("[CQ:image,file=ftp://example.test/rank.png]")

    def test_none_and_existing_reply_are_not_rewrapped(self):
        existing = BotReply(text="ok")

        self.assertIsNone(normalize_legacy_reply(None))
        self.assertIs(normalize_legacy_reply(existing), existing)

    def test_private_dispatch_reuses_existing_handler_and_maps_file(self):
        connection = object()
        inbound = BotInboundMessage(
            address=BotAddress(
                transport="onebot_v11",
                conversation_kind="private",
                conversation_id="10001",
                user_id="10001",
            ),
            text="我的成绩",
            attachments=(
                BotAttachment(
                    kind="file",
                    name="replay.txt",
                    platform_file_id="file-1",
                ),
            ),
            message_id="message-1",
            reference_id="quoted-message",
            received_at=datetime(2026, 7, 27, 12, 0, 0),
        )

        with patch(
            "services.bot_private_service.handle_private_message",
            return_value="成绩回复",
        ) as handler:
            reply = dispatch_business_message(connection, inbound)

        self.assertEqual(reply, BotReply(text="成绩回复"))
        handler.assert_called_once_with(
            connection,
            bot_platform="qq",
            bot_user_id="10001",
            text="我的成绩",
            message_segments=[
                {"type": "reply", "data": {"id": "quoted-message"}},
                {
                    "type": "file",
                    "data": {"name": "replay.txt", "file_id": "file-1"},
                }
            ],
        )

    def test_group_dispatch_keeps_official_openids_and_mention_state(self):
        connection = object()
        inbound = BotInboundMessage(
            address=BotAddress(
                transport="qq_official",
                conversation_kind="group",
                conversation_id="group-openid",
                user_id="user-openid",
            ),
            text="/群星杯 A",
            mentioned=True,
        )

        with patch(
            "services.bot_private_service.handle_group_message",
            return_value="A队",
        ) as handler:
            reply = dispatch_business_message(connection, inbound)

        self.assertEqual(reply, BotReply(text="A队"))
        handler.assert_called_once_with(
            connection,
            bot_platform="qq_official",
            bot_user_id="user-openid",
            group_id="group-openid",
            text="/群星杯 A",
            message_segments=[],
            is_at_bot=True,
        )


if __name__ == "__main__":
    main()
