from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT_TEXT = str(PROJECT_ROOT)
if PROJECT_ROOT_TEXT in sys.path:
    sys.path.remove(PROJECT_ROOT_TEXT)
sys.path.insert(0, PROJECT_ROOT_TEXT)

ENTRYPOINT_PATH = PROJECT_ROOT / "scripts" / "run_official_qq_bot.py"
SERVICE_TEMPLATE_PATH = (
    PROJECT_ROOT / "deploy" / "systemd" / "official-qq-bot.service.example"
)
ENTRYPOINT_SPEC = importlib.util.spec_from_file_location(
    "official_qq_entrypoint_under_test",
    ENTRYPOINT_PATH,
)
if ENTRYPOINT_SPEC is None or ENTRYPOINT_SPEC.loader is None:
    raise RuntimeError("Unable to load official QQ entrypoint")
entrypoint = importlib.util.module_from_spec(ENTRYPOINT_SPEC)
sys.modules[ENTRYPOINT_SPEC.name] = entrypoint
ENTRYPOINT_SPEC.loader.exec_module(entrypoint)
runtime_main = entrypoint.main


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
            ), patch.object(
                entrypoint,
                "run_official_qq_bot",
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
            ), patch.object(
                entrypoint,
                "run_official_qq_bot",
            ) as run:
                result = runtime_main(["--start"])
        self.assertEqual(result, 0)
        run.assert_called_once()
        config = run.call_args.args[0]
        self.assertEqual(config.app_id, "private-app-id")
        self.assertEqual(config.app_secret, "private-app-secret")

    def test_disabled_start_exits_before_runtime_call(self):
        error = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), patch.object(
            entrypoint,
            "run_official_qq_bot",
        ) as run, patch("sys.stderr", error):
            result = runtime_main(["--start"])
        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertIn('"network_started": false', error.getvalue())

    def test_preflight_delegates_without_starting_runtime(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                self._environment(database),
                clear=True,
            ), patch.object(
                entrypoint,
                "preflight_official_qq_bot",
                return_value={
                    "network_started": False,
                    "sdk": {"client_constructed": True},
                },
            ) as preflight, patch.object(
                entrypoint,
                "run_official_qq_bot",
            ) as run, patch("sys.stdout", output):
                result = runtime_main(["--preflight"])

        self.assertEqual(result, 0)
        preflight.assert_called_once()
        run.assert_not_called()
        self.assertIn('"status": "preflight_passed"', output.getvalue())

    def test_preflight_redacts_unexpected_exception(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            error = io.StringIO()
            with patch.dict(
                "os.environ",
                self._environment(database),
                clear=True,
            ), patch.object(
                entrypoint,
                "preflight_official_qq_bot",
                side_effect=RuntimeError(
                    "private-app-secret /internal/private/path"
                ),
            ), patch.object(
                entrypoint,
                "run_official_qq_bot",
            ) as run, patch("sys.stderr", error):
                result = runtime_main(["--preflight"])

        rendered = error.getvalue()
        self.assertEqual(result, 1)
        run.assert_not_called()
        self.assertIn('"status": "failed"', rendered)
        self.assertIn('"error_type": "RuntimeError"', rendered)
        self.assertIn('"network_started": false', rendered)
        self.assertNotIn("private-app-secret", rendered)
        self.assertNotIn("/internal/private/path", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_preflight_preserves_known_failure_code(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            error = io.StringIO()
            with patch.dict(
                "os.environ",
                self._environment(database),
                clear=True,
            ), patch.object(
                entrypoint,
                "preflight_official_qq_bot",
                side_effect=entrypoint.OfficialRuntimeConfigError(
                    "群星杯 latest 产物校验失败",
                    code="stars_cup_snapshot_invalid",
                ),
            ), patch("sys.stderr", error):
                result = runtime_main(["--preflight"])

        rendered = error.getvalue()
        self.assertEqual(result, 2)
        self.assertIn('"status": "blocked"', rendered)
        self.assertIn(
            '"code": "stars_cup_snapshot_invalid"',
            rendered,
        )
        self.assertNotIn("Traceback", rendered)

    def test_systemd_template_runs_offline_preflight_before_start(self):
        rendered = SERVICE_TEMPLATE_PATH.read_text(encoding="utf-8")
        preflight = (
            "ExecStartPre=/opt/2048-event/赛事中台/.venv/bin/python "
            "scripts/run_official_qq_bot.py --preflight"
        )
        start = (
            "ExecStart=/opt/2048-event/赛事中台/.venv/bin/python "
            "scripts/run_official_qq_bot.py --start"
        )

        self.assertIn(preflight, rendered)
        self.assertIn(start, rendered)
        self.assertLess(rendered.index(preflight), rendered.index(start))


if __name__ == "__main__":
    main()
