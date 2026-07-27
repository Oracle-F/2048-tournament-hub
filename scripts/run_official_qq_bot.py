"""Guarded entry point for the official QQ Bot runtime.

The default action only validates process environment.  A real gateway login
requires both ``OFFICIAL_QQ_BOT_ENABLED=true`` and the explicit ``--start``
argument.  This script never loads dotenv files; operators must inject secrets
through the process environment.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_official_qq.runtime import (  # noqa: E402
    OfficialRuntimeConfig,
    OfficialRuntimeConfigError,
    check_official_qq_local_readiness,
    preflight_official_qq_bot,
    run_official_qq_bot,
)


def _configure_runtime_logging(environment=None) -> None:
    values = os.environ if environment is None else environment
    level_name = str(
        values.get("OFFICIAL_QQ_LOG_LEVEL", "INFO")
    ).strip().upper()
    levels = {
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    level = levels.get(level_name)
    if level is None:
        raise OfficialRuntimeConfigError(
            "OFFICIAL_QQ_LOG_LEVEL 只允许 INFO/WARNING/ERROR/CRITICAL",
            code="invalid_log_level",
        )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验或显式启动官方 QQ Bot（默认只校验，不联网）"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--start",
        action="store_true",
        help="显式启动 WebSocket；没有此参数绝不登录或联网",
    )
    action.add_argument(
        "--check-config",
        action="store_true",
        help="显式执行默认的只读配置检查（不导入 SDK、不联网）",
    )
    action.add_argument(
        "--check-local",
        action="store_true",
        help="不要求启用标志或凭据，只读检查本地文件和目录",
    )
    action.add_argument(
        "--preflight",
        action="store_true",
        help="离线构造 SDK 并校验群星杯产物；不登录、不联网",
    )
    args = parser.parse_args(argv)
    if args.check_local:
        try:
            summary = check_official_qq_local_readiness()
        except OfficialRuntimeConfigError as exc:
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "code": exc.code,
                        "error": str(exc),
                        "network_started": False,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "network_started": False,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1
        print(
            json.dumps(
                {
                    "status": "local_ready",
                    **summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    try:
        config = OfficialRuntimeConfig.from_environment()
    except OfficialRuntimeConfigError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": exc.code,
                    "error": str(exc),
                    "network_started": False,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    if args.preflight:
        try:
            summary = preflight_official_qq_bot(config)
        except OfficialRuntimeConfigError as exc:
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "code": exc.code,
                        "error": str(exc),
                        "network_started": False,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "network_started": False,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1
        print(
            json.dumps(
                {
                    "status": "preflight_passed",
                    **summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.start:
        print(
            json.dumps(
                {
                    "status": "config_valid",
                    "network_started": False,
                    "config": config.safe_summary(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    try:
        _configure_runtime_logging()
    except OfficialRuntimeConfigError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": exc.code,
                    "error": str(exc),
                    "network_started": False,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "network_started": False,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    try:
        run_official_qq_bot(config)
    except OfficialRuntimeConfigError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": exc.code,
                    "error": str(exc),
                    "network_started": False,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "network_started": True,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
