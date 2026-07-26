from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase, main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from tests.scripts.testing_support import fresh_test_connection  # noqa: E402
from services import bot_private_service as bot  # noqa: E402


class FlowToggleTests(TestCase):
    def test_submit_flow_opens_when_explicitly_enabled(self):
        original_submit_enabled = bot.BOT_SUBMIT_SCORE_ENABLED
        original_flows = dict(bot.PENDING_FLOWS)
        try:
            bot.BOT_SUBMIT_SCORE_ENABLED = True
            with fresh_test_connection() as connection:
                connection.execute(
                    "INSERT INTO players (id, display_name, status, created_at, updated_at) VALUES (9004, 'BoundUser', 'active', '2026-06-03 09:00:00', '2026-06-03 09:00:00')"
                )
                connection.execute(
                    """
                    INSERT INTO bot_account_bindings (
                        id, bot_platform, bot_user_id, game_platform, player_id, account_key,
                        display_name, is_active, metadata_json, created_at, updated_at
                    )
                    VALUES (
                        9904, 'qq', '10009', '2048verse', 9004, 'test_user',
                        'test_user', 1, '{}', '2026-06-03 09:00:00', '2026-06-03 09:00:00'
                    )
                    """
                )

                reply = bot.handle_private_message(
                    connection,
                    bot_platform="qq",
                    bot_user_id="10009",
                    text="提交成绩",
                )

            self.assertEqual("开始提交成绩。\n当前没有可提交成绩的赛事。", reply)
            flow = bot._get_flow("qq", "10009")
            self.assertIsNotNone(flow)
            self.assertEqual(
                {"action": "submit_score", "step": "event", "data": {}, "candidate_event_codes": []},
                {key: value for key, value in flow.items() if key != "_updated_at"},
            )
        finally:
            bot.BOT_SUBMIT_SCORE_ENABLED = original_submit_enabled
            bot.PENDING_FLOWS.clear()
            bot.PENDING_FLOWS.update(original_flows)

    def test_submit_flow_respects_disabled_toggle(self):
        original_submit_enabled = bot.BOT_SUBMIT_SCORE_ENABLED
        original_flows = dict(bot.PENDING_FLOWS)
        try:
            bot.BOT_SUBMIT_SCORE_ENABLED = False
            with fresh_test_connection() as connection:
                connection.execute(
                    "INSERT INTO players (id, display_name, status, created_at, updated_at) VALUES (9005, 'BoundUser', 'active', '2026-06-03 09:00:00', '2026-06-03 09:00:00')"
                )
                connection.execute(
                    """
                    INSERT INTO bot_account_bindings (
                        id, bot_platform, bot_user_id, game_platform, player_id, account_key,
                        display_name, is_active, metadata_json, created_at, updated_at
                    )
                    VALUES (
                        9905, 'qq', '10010', '2048verse', 9005, 'test_user',
                        'test_user', 1, '{}', '2026-06-03 09:00:00', '2026-06-03 09:00:00'
                    )
                    """
                )
                bot._set_flow(
                    "qq",
                    "10010",
                    {"action": "submit_score", "step": "event", "data": {}, "candidate_event_codes": ["EVT001"]},
                )

                reply = bot.handle_private_message(
                    connection,
                    bot_platform="qq",
                    bot_user_id="10010",
                    text="1",
                )

            self.assertEqual("当前已关闭 bot 提交成绩入口，请改用 Hub 手动录入。", reply)
            self.assertIsNone(bot._get_flow("qq", "10010"))
        finally:
            bot.BOT_SUBMIT_SCORE_ENABLED = original_submit_enabled
            bot.PENDING_FLOWS.clear()
            bot.PENDING_FLOWS.update(original_flows)

    def test_floor_flow_respects_disabled_toggle(self):
        original_lock_enabled = bot.BOT_LOCK_UPLOAD_ENABLED
        original_flows = dict(bot.PENDING_FLOWS)
        try:
            bot.BOT_LOCK_UPLOAD_ENABLED = False
            with fresh_test_connection() as connection:
                connection.execute(
                    "INSERT INTO players (id, display_name, status, created_at, updated_at) VALUES (9006, 'BoundUser', 'active', '2026-06-03 09:00:00', '2026-06-03 09:00:00')"
                )
                connection.execute(
                    """
                    INSERT INTO bot_account_bindings (
                        id, bot_platform, bot_user_id, game_platform, player_id, account_key,
                        display_name, is_active, metadata_json, created_at, updated_at
                    )
                    VALUES (
                        9906, 'qq', '10011', '2048verse', 9006, 'test_user',
                        'test_user', 1, '{}', '2026-06-03 09:00:00', '2026-06-03 09:00:00'
                    )
                    """
                )
                bot._set_flow(
                    "qq",
                    "10011",
                    {"action": "floor_select_event", "candidate_event_codes": ["EVT002"]},
                )

                reply = bot.handle_private_message(
                    connection,
                    bot_platform="qq",
                    bot_user_id="10011",
                    text="1",
                )

            self.assertEqual("当前已关闭 bot 锁局上传入口，请改用 Hub 手动处理。", reply)
            self.assertIsNone(bot._get_flow("qq", "10011"))
        finally:
            bot.BOT_LOCK_UPLOAD_ENABLED = original_lock_enabled
            bot.PENDING_FLOWS.clear()
            bot.PENDING_FLOWS.update(original_flows)


if __name__ == "__main__":
    main()
