from __future__ import annotations

import sys
import shutil
from pathlib import Path
from unittest import TestCase, main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from services import bot_private_service as bot  # noqa: E402


class GroupDebugLogTests(TestCase):
    def test_group_debug_log_is_opt_in(self):
        original_enabled = bot.BOT_GROUP_DEBUG_LOG_ENABLED
        original_path = bot.GROUP_DEBUG_LOG_PATH
        temp_dir = PROJECT_ROOT / "data" / "tmp" / "tests" / "group_debug_log"
        temp_path = temp_dir / "group_debug.log"
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            bot.GROUP_DEBUG_LOG_PATH = temp_path

            bot.BOT_GROUP_DEBUG_LOG_ENABLED = False
            bot._append_group_debug("should stay silent")
            self.assertFalse(temp_path.exists())

            bot.BOT_GROUP_DEBUG_LOG_ENABLED = True
            bot._append_group_debug("should be written")
            self.assertTrue(temp_path.exists())
            self.assertIn("should be written", temp_path.read_text(encoding="utf-8"))
        finally:
            bot.BOT_GROUP_DEBUG_LOG_ENABLED = original_enabled
            bot.GROUP_DEBUG_LOG_PATH = original_path
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
