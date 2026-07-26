from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
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
from services import bot_attachment_service as attachment_service  # noqa: E402
from services import bot_private_service as bot_business  # noqa: E402
from services import replay_lock_service  # noqa: E402
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


class FakeUploader:
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
            {
                "api": api,
                "conversation_kind": conversation_kind,
                "conversation_id": conversation_id,
                "attachment": attachment,
            }
        )
        return {"file_info": "offline-file-info"}


class FakeDownloadResponse:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        payload, self.payload = self.payload, b""
        return payload


class OfficialQQAppTests(IsolatedAsyncioTestCase):
    def _event(
        self,
        *,
        content="help",
        user_openid="opaque-user",
        attachments=None,
    ):
        event = {
            "id": "incoming",
            "author": {"user_openid": user_openid},
            "content": content,
            "message_scene": {"ext": ["msg_idx=index-1"]},
        }
        if attachments is not None:
            event["attachments"] = attachments
        return event

    def _seed_bound_user(
        self,
        connection,
        *,
        user_openid,
        account_key="official_fixture_user",
        base_id=994000,
    ):
        connection.execute(
            """
            INSERT INTO players (
                id, display_name, status, created_at, updated_at
            )
            VALUES (?, ?, 'active', '2026-07-27 08:00:00', '2026-07-27 08:00:00')
            """,
            (base_id + 1, account_key),
        )
        connection.execute(
            """
            INSERT INTO player_accounts (
                id, player_id, platform_id, account_key, account_name,
                account_display_name, is_primary, metadata_json,
                created_at, updated_at
            )
            VALUES (
                ?, ?, (SELECT id FROM platforms WHERE code = '2048verse'),
                ?, ?, ?, 1, '{}',
                '2026-07-27 08:00:00', '2026-07-27 08:00:00'
            )
            """,
            (
                base_id + 2,
                base_id + 1,
                account_key,
                account_key,
                account_key,
            ),
        )
        connection.execute(
            """
            INSERT INTO bot_account_bindings (
                id, bot_platform, bot_user_id, game_platform, player_id,
                account_key, display_name, is_active, metadata_json,
                created_at, updated_at
            )
            VALUES (
                ?, 'qq_official', ?, '2048verse', ?, ?, ?, 1, '{}',
                '2026-07-27 08:00:00', '2026-07-27 08:00:00'
            )
            """,
            (
                base_id + 3,
                user_openid,
                base_id + 1,
                account_key,
                account_key,
            ),
        )
        return base_id + 1

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

    async def test_real_private_help_image_crosses_official_media_boundary(self):
        api = FakeApi()
        uploader = FakeUploader()
        transport = OfficialQQTransport(api, uploader=uploader)
        with fresh_test_connection() as connection, patch(
            "services.bot_private_service._help_image_cq",
            return_value="[CQ:image,file=/offline/help.png]",
        ):
            outcome = await process_official_event(
                connection,
                event_type="C2C_MESSAGE_CREATE",
                event=self._event(user_openid="official-help-user"),
                transport=transport,
            )

        self.assertEqual(outcome.reply.text, "")
        self.assertEqual(len(outcome.reply.attachments), 1)
        self.assertEqual(outcome.reply.attachments[0].kind, "image")
        self.assertEqual(len(uploader.calls), 1)
        self.assertEqual(
            uploader.calls[0]["conversation_kind"],
            "private",
        )
        self.assertEqual(api.calls[0]["msg_type"], 7)
        self.assertEqual(
            api.calls[0]["media"],
            {"file_info": "offline-file-info"},
        )
        self.assertEqual(outcome.receipts[0].status, "sent")

    async def test_real_private_bind_prompt_crosses_official_text_boundary(self):
        api = FakeApi()
        with fresh_test_connection() as connection:
            outcome = await process_official_event(
                connection,
                event_type="C2C_MESSAGE_CREATE",
                event=self._event(
                    content="绑定",
                    user_openid="official-bind-user",
                ),
                transport=OfficialQQTransport(api),
            )

        self.assertEqual(outcome.reply.text, "请输入verse用户名。")
        self.assertEqual(api.calls[0]["msg_type"], 0)
        self.assertEqual(api.calls[0]["openid"], "official-bind-user")

    async def test_real_private_bind_and_unbind_flow_completes_offline(self):
        api = FakeApi()
        user_openid = "official-bind-unbind-user"
        flow_key = "qq_official:{}".format(user_openid)
        bot_business.PENDING_FLOWS.pop(flow_key, None)
        try:
            with fresh_test_connection() as connection:
                prompt = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="绑定",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                pin_prompt = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="official_new_user",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                bound = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="1234",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                unbound = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="解绑",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                binding = connection.execute(
                    """
                    SELECT bot_platform, account_key, is_active, metadata_json
                    FROM bot_account_bindings
                    WHERE bot_user_id = ?
                    """,
                    (user_openid,),
                ).fetchone()

            self.assertEqual(prompt.reply.text, "请输入verse用户名。")
            self.assertEqual(pin_prompt.reply.text, "请输入4-5位绑定密码。")
            self.assertIn("已绑定 2048verse 账号：official_new_user", bound.reply.text)
            self.assertEqual(
                unbound.reply.text,
                "已解绑 2048verse 账号：official_new_user",
            )
            self.assertEqual(binding["bot_platform"], "qq_official")
            self.assertEqual(binding["account_key"], "official_new_user")
            self.assertEqual(binding["is_active"], 0)
            self.assertNotIn("1234", binding["metadata_json"])
            self.assertEqual(len(api.calls), 4)
        finally:
            bot_business.PENDING_FLOWS.pop(flow_key, None)

    async def test_real_private_registration_and_cancellation_completes_offline(self):
        api = FakeApi()
        user_openid = "official-registration-user"
        flow_key = "qq_official:{}".format(user_openid)
        bot_business.PENDING_FLOWS.pop(flow_key, None)
        try:
            with fresh_test_connection() as connection:
                player_id = self._seed_bound_user(
                    connection,
                    user_openid=user_openid,
                    account_key="official_registration_user",
                )
                connection.execute(
                    """
                    INSERT INTO events (
                        id, event_code, event_name, platform_id, variant_id,
                        event_type, competition_type, status, is_official,
                        is_rated, start_time, end_time, created_at, updated_at
                    )
                    VALUES (
                        994010, 'OFFICIAL_REG_001', 'Official Registration Event',
                        (SELECT id FROM platforms WHERE code = '2048verse'),
                        (SELECT id FROM variants WHERE code = '4x4'),
                        'single_attempt', 'classic_raw_score', 'ready', 1, 0,
                        '2027-01-01 08:00:00', '2027-01-01 10:00:00',
                        '2026-07-27 08:00:00', '2026-07-27 08:00:00'
                    )
                    """
                )
                register_menu = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="报名",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                registered = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="1",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                cancel_menu = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="取消报名",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                cancelled = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="1",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                registration = connection.execute(
                    """
                    SELECT status
                    FROM registrations
                    WHERE event_id = 994010 AND player_id = ?
                    """,
                    (player_id,),
                ).fetchone()

            self.assertIn("Official Registration Event", register_menu.reply.text)
            self.assertIn("报名成功", registered.reply.text)
            self.assertIn("Official Registration Event", cancel_menu.reply.text)
            self.assertEqual(cancelled.reply.text, "已取消报名。")
            self.assertEqual(registration["status"], "cancelled")
            self.assertEqual(len(api.calls), 4)
        finally:
            bot_business.PENDING_FLOWS.pop(flow_key, None)

    async def test_real_dashboard_edit_and_group_query_complete_offline(self):
        api = FakeApi()
        user_openid = "official-dashboard-user"
        group_openid = "official-dashboard-group"
        flow_key = "qq_official:{}".format(user_openid)
        original_enabled = bot_business.GROUP_CHAT_ENABLED
        original_whitelist = set(bot_business.GROUP_CHAT_WHITELIST)
        bot_business.PENDING_FLOWS.pop(flow_key, None)
        try:
            bot_business.GROUP_CHAT_ENABLED = True
            bot_business.GROUP_CHAT_WHITELIST.clear()
            bot_business.GROUP_CHAT_WHITELIST.add(group_openid)
            bot_business.GROUP_RATE_LIMIT_STATE.clear()
            bot_business.GROUP_GLOBAL_RATE_LIMIT_STATE.clear()
            with fresh_test_connection() as connection:
                self._seed_bound_user(
                    connection,
                    user_openid=user_openid,
                    account_key="official_dashboard_user",
                )
                edit_prompt = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="看板",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                edited = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="第一行\n第二行",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
                queried = await process_official_event(
                    connection,
                    event_type="GROUP_AT_MESSAGE_CREATE",
                    event={
                        "id": "incoming-dashboard-group",
                        "group_openid": group_openid,
                        "author": {"member_openid": "official-dashboard-viewer"},
                        "content": "official_dashboard_user",
                    },
                    transport=OfficialQQTransport(api),
                )

            self.assertIn("请直接发送个人看板完整内容", edit_prompt.reply.text)
            self.assertIn("个人看板已更新", edited.reply.text)
            self.assertEqual(
                queried.reply.text,
                "玩家: official_dashboard_user\n个人看板\n第一行\n第二行",
            )
            self.assertEqual(api.calls[-1]["group_openid"], group_openid)
            self.assertEqual(len(api.calls), 3)
        finally:
            bot_business.GROUP_CHAT_ENABLED = original_enabled
            bot_business.GROUP_CHAT_WHITELIST.clear()
            bot_business.GROUP_CHAT_WHITELIST.update(original_whitelist)
            bot_business.GROUP_RATE_LIMIT_STATE.clear()
            bot_business.GROUP_GLOBAL_RATE_LIMIT_STATE.clear()
            bot_business.PENDING_FLOWS.pop(flow_key, None)

    async def test_real_verse_query_and_disabled_score_gate_cross_official_route(self):
        api = FakeApi()
        user_openid = "official-query-score-user"
        with fresh_test_connection() as connection:
            self._seed_bound_user(
                connection,
                user_openid=user_openid,
                account_key="official_query_user",
            )
            with patch.object(
                bot_business,
                "handle_verse_query_message",
                return_value="玩家: official_query_user\n4x4 PB 131072",
            ) as verse_query:
                query = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="4pb official_query_user",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )
            with patch.object(
                bot_business,
                "BOT_SUBMIT_SCORE_ENABLED",
                False,
            ):
                submit = await process_official_event(
                    connection,
                    event_type="C2C_MESSAGE_CREATE",
                    event=self._event(
                        content="提交成绩",
                        user_openid=user_openid,
                    ),
                    transport=OfficialQQTransport(api),
                )

        self.assertEqual(
            query.reply.text,
            "玩家: official_query_user\n4x4 PB 131072",
        )
        verse_query.assert_called_once_with(
            connection,
            bot_platform="qq_official",
            bot_user_id=user_openid,
            text="4pb official_query_user",
        )
        self.assertEqual(
            submit.reply.text,
            "当前已关闭 bot 提交成绩入口，请改用 Hub 手动录入。",
        )
        self.assertEqual(len(api.calls), 2)

    async def test_enabled_score_submission_flow_crosses_official_route_offline(self):
        api = FakeApi()
        user_openid = "official-score-user"
        flow_key = "qq_official:{}".format(user_openid)
        original_enabled = bot_business.BOT_SUBMIT_SCORE_ENABLED
        bot_business.PENDING_FLOWS.pop(flow_key, None)
        try:
            bot_business.BOT_SUBMIT_SCORE_ENABLED = True
            with fresh_test_connection() as connection:
                self._seed_bound_user(
                    connection,
                    user_openid=user_openid,
                    account_key="official_score_user",
                )
                connection.execute(
                    """
                    INSERT INTO events (
                        id, event_code, event_name, platform_id, variant_id,
                        event_type, competition_type, status, is_official,
                        is_rated, start_time, end_time, created_at, updated_at
                    )
                    VALUES (
                        994020, 'OFFICIAL_SCORE_001', 'Official Score Event',
                        (SELECT id FROM platforms WHERE code = '2048verse'),
                        (SELECT id FROM variants WHERE code = '4x4'),
                        'single_attempt', 'classic_raw_score', 'active', 1, 0,
                        '2027-01-01 08:00:00', '2027-01-02 08:00:00',
                        '2026-07-27 08:00:00', '2026-07-27 08:00:00'
                    )
                    """
                )
                replies = []
                for content in (
                    "提交成绩",
                    "1",
                    "12345",
                    "2026-07-27 05:00:00",
                ):
                    replies.append(
                        await process_official_event(
                            connection,
                            event_type="C2C_MESSAGE_CREATE",
                            event=self._event(
                                content=content,
                                user_openid=user_openid,
                            ),
                            transport=OfficialQQTransport(api),
                        )
                    )
                submission = connection.execute(
                    """
                    SELECT
                        submitter_platform, submitter_account, final_score,
                        ended_at, status
                    FROM pending_score_submissions
                    WHERE event_id = 994020
                    """
                ).fetchone()

            self.assertIn("Official Score Event", replies[0].reply.text)
            self.assertIn("请发送成绩", replies[1].reply.text)
            self.assertIn("请发送终局时间", replies[2].reply.text)
            self.assertIn("成绩已提交，等待审核", replies[3].reply.text)
            self.assertEqual(submission["submitter_platform"], "qq_official")
            self.assertEqual(submission["submitter_account"], user_openid)
            self.assertEqual(submission["final_score"], 12345)
            self.assertEqual(submission["ended_at"], "2026-07-27 05:00:00")
            self.assertEqual(submission["status"], "pending")
            self.assertEqual(len(api.calls), 4)
        finally:
            bot_business.BOT_SUBMIT_SCORE_ENABLED = original_enabled
            bot_business.PENDING_FLOWS.pop(flow_key, None)

    async def test_timed_reservation_and_cancellation_cross_official_route_offline(self):
        api = FakeApi()
        user_openid = "official-reservation-user"
        flow_key = "qq_official:{}".format(user_openid)
        bot_business.PENDING_FLOWS.pop(flow_key, None)
        try:
            with fresh_test_connection() as connection:
                player_id = self._seed_bound_user(
                    connection,
                    user_openid=user_openid,
                    account_key="official_reservation_user",
                )
                connection.execute(
                    """
                    INSERT INTO events (
                        id, event_code, event_name, platform_id, variant_id,
                        event_type, competition_type, status, is_official,
                        is_rated, start_time, end_time, metadata_json,
                        created_at, updated_at
                    )
                    VALUES (
                        994030, 'OFFICIAL_RESERVE_001',
                        'Official Reservation Event',
                        (SELECT id FROM platforms WHERE code = '2048verse'),
                        (SELECT id FROM variants WHERE code = '2x4'),
                        'timed_scoring', 'timed_scoring', 'ready', 1, 0,
                        '2027-01-01 08:00:00', '2027-01-02 08:00:00',
                        '{"timed_mode":"reservation","reservation_duration_minutes":60}',
                        '2026-07-27 08:00:00', '2026-07-27 08:00:00'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO registrations (
                        id, event_id, player_id, registered_via, status,
                        metadata_json, registered_at
                    )
                    VALUES (
                        994031, 994030, ?, 'manual', 'active', '{}',
                        '2026-07-27 08:00:00'
                    )
                    """,
                    (player_id,),
                )
                replies = []
                for content in (
                    "预约限时赛",
                    "1",
                    "2027-01-01 09:00",
                    "我的限时预约",
                    "取消限时预约",
                    "1",
                ):
                    replies.append(
                        await process_official_event(
                            connection,
                            event_type="C2C_MESSAGE_CREATE",
                            event=self._event(
                                content=content,
                                user_openid=user_openid,
                            ),
                            transport=OfficialQQTransport(api),
                        )
                    )
                reservation = connection.execute(
                    """
                    SELECT
                        reserved_start_time, reserved_end_time, status
                    FROM timed_event_reservations
                    WHERE event_id = 994030 AND player_id = ?
                    """,
                    (player_id,),
                ).fetchone()

            self.assertIn("Official Reservation Event", replies[0].reply.text)
            self.assertIn("请输入预约开始时间", replies[1].reply.text)
            self.assertIn("预约成功", replies[2].reply.text)
            self.assertIn("我的限时预约", replies[3].reply.text)
            self.assertIn("Official Reservation Event", replies[4].reply.text)
            self.assertEqual(replies[5].reply.text, "已取消限时预约。")
            self.assertEqual(
                reservation["reserved_start_time"],
                "2027-01-01T09:00:00+08:00",
            )
            self.assertEqual(
                reservation["reserved_end_time"],
                "2027-01-01T10:00:00+08:00",
            )
            self.assertEqual(reservation["status"], "cancelled")
            self.assertEqual(len(api.calls), 6)
        finally:
            bot_business.PENDING_FLOWS.pop(flow_key, None)

    async def test_real_private_admin_command_keeps_existing_permission_gate(self):
        api = FakeApi()
        with fresh_test_connection() as connection:
            outcome = await process_official_event(
                connection,
                event_type="C2C_MESSAGE_CREATE",
                event=self._event(
                    content="管理员帮助",
                    user_openid="official-non-admin",
                ),
                transport=OfficialQQTransport(api),
            )

        self.assertEqual(outcome.reply.text, "你没有管理员权限。")
        self.assertEqual(outcome.receipts[0].status, "sent")

    async def test_authorized_admin_help_crosses_official_route_offline(self):
        api = FakeApi()
        user_openid = "official-admin-user"
        with fresh_test_connection() as connection, patch.object(
            bot_business,
            "is_bot_admin",
            return_value=True,
        ) as admin_gate, patch.object(
            bot_business,
            "_help_image_cq",
            return_value=None,
        ):
            outcome = await process_official_event(
                connection,
                event_type="C2C_MESSAGE_CREATE",
                event=self._event(
                    content="管理员帮助",
                    user_openid=user_openid,
                ),
                transport=OfficialQQTransport(api),
            )

        self.assertIn("管理员命令", outcome.reply.text)
        self.assertIn("待审核列表", outcome.reply.text)
        admin_gate.assert_called_once_with("qq_official", user_openid)
        self.assertEqual(outcome.receipts[0].status, "sent")

    async def test_real_private_attachment_reaches_unbound_business_gate(self):
        api = FakeApi()
        with fresh_test_connection() as connection:
            outcome = await process_official_event(
                connection,
                event_type="C2C_MESSAGE_CREATE",
                event=self._event(
                    content="",
                    user_openid="official-file-user",
                    attachments=[
                        {
                            "filename": "replay.txt",
                            "content_type": "text/plain",
                            "url": "https://qq.example.test/replay.txt",
                        }
                    ],
                ),
                transport=OfficialQQTransport(api),
            )

        self.assertEqual(
            outcome.reply.text,
            "你还没有绑定账号。请先发送：绑定",
        )
        self.assertEqual(outcome.inbound.attachments[0].kind, "file")
        self.assertEqual(outcome.receipts[0].status, "sent")

    async def test_real_private_floor_and_finish_flow_completes_offline(self):
        api = FakeApi()
        user_openid = "official-floor-finish-user"
        flow_key = "qq_official:{}".format(user_openid)
        early_payload = b"2048-replay-prefix"
        final_payload = early_payload + b"-completed"
        bot_business.PENDING_FLOWS.pop(flow_key, None)
        try:
            with TemporaryDirectory() as temp_dir, fresh_test_connection() as connection:
                temp_root = Path(temp_dir)
                connection.execute(
                    """
                    INSERT INTO players (
                        id, display_name, status, created_at, updated_at
                    )
                    VALUES (
                        995001, 'Official Flow User', 'active',
                        '2026-07-27 08:00:00', '2026-07-27 08:00:00'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO player_accounts (
                        id, player_id, platform_id, account_key, account_name,
                        account_display_name, is_primary, metadata_json,
                        created_at, updated_at
                    )
                    VALUES (
                        995002, 995001,
                        (SELECT id FROM platforms WHERE code = '2048verse'),
                        'official_flow_user', 'official_flow_user',
                        'Official Flow User', 1, '{}',
                        '2026-07-27 08:00:00', '2026-07-27 08:00:00'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO bot_account_bindings (
                        id, bot_platform, bot_user_id, game_platform, player_id,
                        account_key, display_name, is_active, metadata_json,
                        created_at, updated_at
                    )
                    VALUES (
                        995003, 'qq_official', ?, '2048verse', 995001,
                        'official_flow_user', 'Official Flow User', 1, '{}',
                        '2026-07-27 08:00:00', '2026-07-27 08:00:00'
                    )
                    """,
                    (user_openid,),
                )
                connection.execute(
                    """
                    INSERT INTO events (
                        id, event_code, event_name, platform_id, variant_id,
                        event_type, competition_type, status, is_official,
                        is_rated, start_time, end_time, created_at, updated_at
                    )
                    VALUES (
                        995004, 'OFFICIAL_FLOW_001', 'Official Flow Event',
                        (SELECT id FROM platforms WHERE code = '2048verse'),
                        (SELECT id FROM variants WHERE code = '4x4'),
                        'single_attempt', 'classic_raw_score', 'active', 1, 0,
                        '2026-07-26 08:00:00', '2026-07-28 08:00:00',
                        '2026-07-27 08:00:00', '2026-07-27 08:00:00'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO registrations (
                        id, event_id, player_id, registered_via, status,
                        metadata_json, registered_at
                    )
                    VALUES (
                        995005, 995004, 995001, 'manual', 'active', '{}',
                        '2026-07-27 08:00:00'
                    )
                    """
                )
                with patch.object(
                    attachment_service,
                    "BOT_UPLOAD_TEMP_DIR",
                    temp_root / "downloads",
                ), patch.object(
                    replay_lock_service,
                    "EVIDENCE_ROOT_DIR",
                    temp_root / "evidence",
                ), patch.object(
                    attachment_service,
                    "urlopen",
                    side_effect=[
                        FakeDownloadResponse(early_payload),
                        FakeDownloadResponse(final_payload),
                    ],
                ):
                    floor = await process_official_event(
                        connection,
                        event_type="C2C_MESSAGE_CREATE",
                        event=self._event(
                            content="floor",
                            user_openid=user_openid,
                        ),
                        transport=OfficialQQTransport(api),
                    )
                    floor_choice = await process_official_event(
                        connection,
                        event_type="C2C_MESSAGE_CREATE",
                        event=self._event(
                            content="1",
                            user_openid=user_openid,
                        ),
                        transport=OfficialQQTransport(api),
                    )
                    early = await process_official_event(
                        connection,
                        event_type="C2C_MESSAGE_CREATE",
                        event=self._event(
                            content="",
                            user_openid=user_openid,
                            attachments=[
                                {
                                    "filename": "early.vrs",
                                    "content_type": "application/octet-stream",
                                    "url": "https://qq.example.test/early.vrs",
                                }
                            ],
                        ),
                        transport=OfficialQQTransport(api),
                    )
                    finish = await process_official_event(
                        connection,
                        event_type="C2C_MESSAGE_CREATE",
                        event=self._event(
                            content="finish",
                            user_openid=user_openid,
                        ),
                        transport=OfficialQQTransport(api),
                    )
                    finish_choice = await process_official_event(
                        connection,
                        event_type="C2C_MESSAGE_CREATE",
                        event=self._event(
                            content="1",
                            user_openid=user_openid,
                        ),
                        transport=OfficialQQTransport(api),
                    )
                    final = await process_official_event(
                        connection,
                        event_type="C2C_MESSAGE_CREATE",
                        event=self._event(
                            content="",
                            user_openid=user_openid,
                            attachments=[
                                {
                                    "filename": "final.vrs",
                                    "content_type": "application/octet-stream",
                                    "url": "https://qq.example.test/final.vrs",
                                }
                            ],
                        ),
                        transport=OfficialQQTransport(api),
                    )

                session = connection.execute(
                    """
                    SELECT status, metadata_json
                    FROM attempt_sessions
                    WHERE event_id = 995004 AND player_id = 995001
                    """
                ).fetchone()
                self.assertIsNotNone(
                    session,
                    msg="\n".join(
                        outcome.reply.text
                        for outcome in (
                            floor,
                            floor_choice,
                            early,
                            finish,
                            finish_choice,
                            final,
                        )
                        if outcome.reply is not None
                    ),
                )
                metadata = json.loads(session["metadata_json"])

                self.assertIn("Official Flow Event", floor.reply.text)
                self.assertIn("请直接发送当前的 early 回放文件", floor_choice.reply.text)
                self.assertIn("已收到 early 回放", early.reply.text)
                self.assertIn("Official Flow Event", finish.reply.text)
                self.assertIn("请直接发送这局的终局回放文件", finish_choice.reply.text)
                self.assertEqual(
                    final.reply.text,
                    "已收到终局回放。\n前缀校验通过，这局锁局已完成。",
                )
                self.assertEqual(session["status"], "completed")
                self.assertEqual(metadata["prefix_check"]["status"], "passed")
                self.assertEqual(metadata["review"]["status"], "approved")
                self.assertEqual(len(api.calls), 6)
                self.assertTrue(
                    Path(metadata["early_replay"]["path"]).is_relative_to(
                        temp_root / "evidence"
                    )
                )
                self.assertTrue(
                    Path(metadata["final_replay"]["path"]).is_relative_to(
                        temp_root / "evidence"
                    )
                )
        finally:
            bot_business.PENDING_FLOWS.pop(flow_key, None)


if __name__ == "__main__":
    main()
