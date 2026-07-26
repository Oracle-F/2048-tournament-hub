from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / ".env.bot.secret", override=True)

from services.bot_connection_watchdog import (  # noqa: E402
    BOT_CONNECTION_STATE_PATH,
    mark_bot_restart_attempt,
    mark_bot_attention_notice,
    read_text_tail,
    should_attempt_restart,
    should_show_attention_notice,
)


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _resolve_state_path(value: str | None) -> Path:
    if not value:
        return BOT_CONNECTION_STATE_PATH
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _resolve_runtime_log_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _restart_scheduled_task(task_name: str) -> dict[str, object]:
    end_result = subprocess.run(
        ["schtasks", "/End", "/TN", task_name],
        capture_output=True,
        text=True,
        shell=False,
    )
    run_result = subprocess.run(
        ["schtasks", "/Run", "/TN", task_name],
        capture_output=True,
        text=True,
        shell=False,
    )
    return {
        "end_returncode": end_result.returncode,
        "end_stdout": end_result.stdout.strip(),
        "end_stderr": end_result.stderr.strip(),
        "run_returncode": run_result.returncode,
        "run_stdout": run_result.stdout.strip(),
        "run_stderr": run_result.stderr.strip(),
        "success": run_result.returncode == 0,
    }


def _task_name_candidates(task_name: str) -> list[str]:
    candidates = [task_name]
    aliases = {
        "EventScore-NapCat-Autostart": "NapCat-Autostart",
        "NapCat-Autostart": "EventScore-NapCat-Autostart",
    }
    alias = aliases.get(task_name)
    if alias:
        candidates.append(alias)
    return candidates


def _show_attention_popup(script_path: Path, *, title: str, message: str, state_path: Path, runtime_log_path: Path | None = None) -> dict[str, object]:
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-Title",
        title,
        "-Message",
        message,
        "-StatePath",
        str(state_path),
    ]
    if runtime_log_path is not None:
        command.extend(["-RuntimeLogPath", str(runtime_log_path)])
    result = subprocess.run(command, capture_output=True, text=True, shell=False)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "success": result.returncode == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch bot connection health and restart NapCat once when it stays offline.")
    parser.add_argument("--task-name", default=os.getenv("NAPCAT_TASK_NAME", "").strip(), help="Scheduled task name used to restart NapCat")
    parser.add_argument("--state-path", default=os.getenv("BOT_CONNECTION_STATE_PATH", ""), help="Connection state JSON path")
    parser.add_argument("--runtime-log-path", default=os.getenv("NAPCAT_RUNTIME_LOG_PATH", ""), help="Optional NapCat runtime log path to snapshot")
    parser.add_argument("--runtime-log-lines", type=int, default=_env_int("NAPCAT_RUNTIME_LOG_TAIL_LINES", 80))
    parser.add_argument("--runtime-log-bytes", type=int, default=_env_int("NAPCAT_RUNTIME_LOG_TAIL_BYTES", 65536))
    parser.add_argument("--popup-script", default=os.getenv("BOT_CONNECTION_ALERT_SCRIPT", "").strip(), help="Optional PowerShell script used to show a popup")
    parser.add_argument("--attention-restart-count", type=int, default=_env_int("BOT_CONNECTION_WATCHDOG_ATTENTION_RESTART_COUNT", 2))
    parser.add_argument("--attention-cooldown-seconds", type=int, default=_env_int("BOT_CONNECTION_WATCHDOG_ATTENTION_COOLDOWN_SECONDS", 900))
    parser.add_argument("--stale-seconds", type=int, default=_env_int("BOT_CONNECTION_WATCHDOG_STALE_SECONDS", 120))
    parser.add_argument("--startup-grace-seconds", type=int, default=_env_int("BOT_CONNECTION_WATCHDOG_STARTUP_GRACE_SECONDS", 90))
    parser.add_argument("--restart-cooldown-seconds", type=int, default=_env_int("BOT_CONNECTION_WATCHDOG_RESTART_COOLDOWN_SECONDS", 300))
    parser.add_argument("--dry-run", action="store_true", help="Print the decision without restarting anything")
    args = parser.parse_args()

    state_path = _resolve_state_path(args.state_path)
    runtime_log_path = _resolve_runtime_log_path(args.runtime_log_path)
    decision = should_attempt_restart(
        state_path,
        stale_seconds=args.stale_seconds,
        startup_grace_seconds=args.startup_grace_seconds,
        restart_cooldown_seconds=args.restart_cooldown_seconds,
        runtime_log_path=runtime_log_path,
        runtime_log_max_lines=args.runtime_log_lines,
        runtime_log_max_bytes=args.runtime_log_bytes,
    )
    payload = {
        "state_path": str(state_path),
        "task_name": args.task_name or None,
        "decision": decision,
    }
    if runtime_log_path is not None:
        payload["runtime_log"] = read_text_tail(
            runtime_log_path,
            max_lines=args.runtime_log_lines,
            max_bytes=args.runtime_log_bytes,
        )

    attention = should_show_attention_notice(
        state_path,
        stale_seconds=args.stale_seconds,
        startup_grace_seconds=args.startup_grace_seconds,
        attention_restart_count=args.attention_restart_count,
        attention_cooldown_seconds=args.attention_cooldown_seconds,
        runtime_log_path=runtime_log_path,
        runtime_log_max_lines=args.runtime_log_lines,
        runtime_log_max_bytes=args.runtime_log_bytes,
    )
    payload["attention"] = attention

    popup_result = None
    enable_popup = os.getenv("BOT_CONNECTION_WATCHDOG_ENABLE_POPUP", "1").strip().lower() in {"1", "true", "yes", "on"}
    if attention.get("attention_required"):
        mark_bot_attention_notice(state_path, reason=attention["reason"])
    if not decision["restart_required"]:
        if attention.get("attention_required") and args.popup_script and enable_popup:
            popup_title = "NapCat 登录状态提醒"
            popup_message_lines = [
                "NapCat 连接状态出现了需要人工介入的异常。",
            ]
            hard_alert_reason = attention.get("hard_alert_reason")
            hard_alert_line = attention.get("hard_alert_line")
            if hard_alert_reason:
                popup_message_lines.append(
                    "日志命中硬告警: {}".format(str(hard_alert_reason))
                )
            if hard_alert_line:
                popup_message_lines.append("")
                popup_message_lines.append(str(hard_alert_line))
            if runtime_log_path is not None:
                runtime_tail = payload.get("runtime_log") or {}
                excerpt = runtime_tail.get("excerpt") if isinstance(runtime_tail, dict) else None
                if excerpt:
                    popup_message_lines.append("")
                    popup_message_lines.append("最近日志：")
                    popup_message_lines.extend(str(line) for line in excerpt[-8:])
            popup_result = _show_attention_popup(
                Path(args.popup_script),
                title=popup_title,
                message="\n".join(popup_message_lines),
                state_path=state_path,
                runtime_log_path=runtime_log_path,
            )
            payload["popup_result"] = popup_result
        print(json.dumps({**payload, "action": "attention" if attention.get("attention_required") else "skip"}, ensure_ascii=False, indent=2))
        return 0

    if not args.task_name:
        print(json.dumps({**payload, "action": "skip", "reason": "missing_task_name"}, ensure_ascii=False, indent=2))
        return 2

    if args.dry_run:
        print(json.dumps({**payload, "action": "restart", "dry_run": True}, ensure_ascii=False, indent=2))
        return 0

    mark_bot_restart_attempt(state_path, reason=decision["reason"])
    restart_result = None
    attempted_task_names: list[str] = []
    for task_name in _task_name_candidates(args.task_name):
        attempted_task_names.append(task_name)
        restart_result = _restart_scheduled_task(task_name)
        if restart_result["success"]:
            payload["restart_task_name"] = task_name
            break
        if "cannot find the path specified" not in str(restart_result.get("end_stderr", "")).lower() and "cannot find the path specified" not in str(restart_result.get("run_stderr", "")).lower():
            break
    if restart_result is None:
        restart_result = {
            "success": False,
            "end_returncode": None,
            "end_stdout": "",
            "end_stderr": "no task name candidates available",
            "run_returncode": None,
            "run_stdout": "",
            "run_stderr": "",
        }
    restart_result["attempted_task_names"] = attempted_task_names
    payload["restart_result"] = restart_result

    if attention.get("attention_required") and args.popup_script and enable_popup:
        popup_title = "NapCat 登录状态提醒"
        popup_message_lines = [
            "NapCat 连接状态持续异常，已经自动重启过仍未恢复到在线状态。",
            "这通常意味着需要人工检查登录态，可能是被踢下线或其他登录问题。",
        ]
        hard_alert_reason = attention.get("hard_alert_reason")
        hard_alert_line = attention.get("hard_alert_line")
        if hard_alert_reason:
            popup_message_lines.append("")
            popup_message_lines.append("日志命中硬告警: {}".format(str(hard_alert_reason)))
        if hard_alert_line:
            popup_message_lines.append("")
            popup_message_lines.append(str(hard_alert_line))
        if runtime_log_path is not None:
            runtime_tail = payload.get("runtime_log") or {}
            excerpt = runtime_tail.get("excerpt") if isinstance(runtime_tail, dict) else None
            if excerpt:
                popup_message_lines.append("")
                popup_message_lines.append("最近日志：")
                popup_message_lines.extend(str(line) for line in excerpt[-8:])
        popup_result = _show_attention_popup(
            Path(args.popup_script),
            title=popup_title,
            message="\n".join(popup_message_lines),
            state_path=state_path,
            runtime_log_path=runtime_log_path,
        )
        payload["popup_result"] = popup_result

    print(json.dumps({**payload, "action": "restart"}, ensure_ascii=False, indent=2))
    return 0 if restart_result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
