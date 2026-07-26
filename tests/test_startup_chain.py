from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase, main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class StartupChainScriptTests(TestCase):
    def test_startup_stack_launches_napcat_directly_and_hidden(self):
        script_text = (PROJECT_ROOT / "scripts" / "start_eventscore_stack.ps1").read_text(encoding="utf-8")

        self.assertIn("Resolve-NapCatBatPath", script_text)
        self.assertIn("Read-BotConnectionState", script_text)
        self.assertIn("Test-NapCatProcessRunning", script_text)
        self.assertIn("NapCat process already running; skipping launch.", script_text)
        self.assertIn("run_napcat_with_log.ps1", script_text)
        self.assertIn("-ConsoleLike -HideLauncherWindow", script_text)
        self.assertNotIn("eventscore_napcat_launch.ps1", script_text)

    def test_napcat_launcher_blocks_duplicate_process_by_default(self):
        script_text = (PROJECT_ROOT / "scripts" / "run_napcat_with_log.ps1").read_text(encoding="utf-8")

        self.assertIn('[switch]$ForceLaunch', script_text)
        self.assertIn('Get-Process -Name "NapCatWinBootMain"', script_text)
        self.assertIn('NapCat launch skipped: process already running', script_text)
        self.assertIn('if (-not $ForceLaunch)', script_text)

    def test_watchdog_prefers_pythonw_runtime(self):
        script_text = (PROJECT_ROOT / "scripts" / "setup_bot_watchdog.ps1").read_text(encoding="utf-8")

        self.assertIn("Resolve-PythonwExecutablePath", script_text)
        self.assertIn("pythonw.exe", script_text)
        self.assertIn("-NoLogo", script_text)
        self.assertIn("-NonInteractive", script_text)
        self.assertIn("Watchdog runtime: pythonw.exe", script_text)

    def test_napcat_autostart_task_uses_hidden_powershell(self):
        script_text = (PROJECT_ROOT / "scripts" / "setup_napcat_logged_autostart.ps1").read_text(encoding="utf-8")

        self.assertIn("-NoLogo", script_text)
        self.assertIn("-NonInteractive", script_text)
        self.assertIn("-WindowStyle Hidden", script_text)

    def test_console_like_napcat_output_is_tee_logged_for_watchdog(self):
        script_text = (PROJECT_ROOT / "scripts" / "run_napcat_with_log.ps1").read_text(encoding="utf-8")

        self.assertIn("New-NapCatTeeCommand", script_text)
        self.assertIn("New-NapCatEncodedTeeCommand", script_text)
        self.assertIn("Tee-Object -FilePath", script_text)
        self.assertIn("-NoExit", script_text)
        self.assertIn("-EncodedCommand", script_text)
        self.assertIn("console_output=tee_to_runtime_log", script_text)

    def test_private_debug_log_is_opt_in(self):
        app_text = (PROJECT_ROOT / "bot_private_qq" / "app.py").read_text(encoding="utf-8")

        self.assertIn('BOT_PRIVATE_DEBUG_LOG_ENABLED = _env_flag("BOT_PRIVATE_DEBUG_LOG_ENABLED", False)', app_text)
        self.assertIn("if not BOT_PRIVATE_DEBUG_LOG_ENABLED:", app_text)

    def test_submit_score_is_opt_in_by_default(self):
        service_text = (PROJECT_ROOT / "services" / "bot_private_service.py").read_text(encoding="utf-8")

        self.assertIn('BOT_SUBMIT_SCORE_ENABLED = _env_flag("BOT_SUBMIT_SCORE_ENABLED", False)', service_text)


if __name__ == "__main__":
    main()
