from __future__ import annotations

import io
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_official_qq_bot import main as runtime_main  # noqa: E402


class OfficialQQEntrypointTests(TestCase):
    def _environment(self, database: Path):
        return {
            "OFFICIAL_QQ_BOT_ENABLED": "true",
            "OFFICIAL_QQ_APP_ID": "private-app-id",
            "OFFICIAL_QQ_APP_SECRET": "private-app-secret",
            "OFFICIAL_QQ_DATABASE_PATH": str(database),
        }

    def test_default_config_check_is_offline_and_redacted(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                self._environment(database),
                clear=True,
            ), patch(
                "scripts.run_official_qq_bot.run_official_qq_bot"
            ) as run, patch("sys.stdout", output):
                result = runtime_main([])
        self.assertEqual(result, 0)
        run.assert_not_called()
        rendered = output.getvalue()
        self.assertIn('"network_started": false', rendered)
        self.assertNotIn("private-app-id", rendered)
        self.assertNotIn("private-app-secret", rendered)

    def test_start_requires_flag_and_delegates_only_after_validation(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            with patch.dict(
                "os.environ",
                self._environment(database),
                clear=True,
            ), patch(
                "scripts.run_official_qq_bot.run_official_qq_bot"
            ) as run:
                result = runtime_main(["--start"])
        self.assertEqual(result, 0)
        run.assert_called_once()
        config = run.call_args.args[0]
        self.assertEqual(config.app_id, "private-app-id")
        self.assertEqual(config.app_secret, "private-app-secret")

    def test_disabled_start_exits_before_runtime_call(self):
        error = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), patch(
            "scripts.run_official_qq_bot.run_official_qq_bot"
        ) as run, patch("sys.stderr", error):
            result = runtime_main(["--start"])
        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertIn('"network_started": false', error.getvalue())


if __name__ == "__main__":
    main()
