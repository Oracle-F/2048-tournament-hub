"""Generate one validated 群星杯 daily ranking package.

This command never starts a scheduler and never sends a QQ message.  Its
default operation queries Verse into a staged cache, renders immutable
artifacts, validates them and atomically publishes the local latest pointer.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from event_hub import (  # noqa: E402
    DEFAULT_BACKGROUND,
    DEFAULT_LIVE_CACHE,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_ROSTER,
)
from services.stars_cup_bot_service import DEFAULT_LATEST_POINTER  # noqa: E402
from services.stars_cup_daily_service import (  # noqa: E402
    DEFAULT_STATE_ROOT,
    run_stars_cup_daily_export,
)


def _parse_run_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--run-time 必须是 ISO 8601 时间") from exc


def _summary(result) -> dict:
    return {
        "status": "published",
        "run_id": result.run_id,
        "published_at": result.published_at.isoformat(),
        "reused": result.reused,
        "output_dir": str(result.output_dir),
        "snapshot": {
            "path": str(result.snapshot_path),
            "sha256": result.snapshot_sha256,
        },
        "images": {
            kind: {
                "path": str(result.image_paths[kind]),
                "sha256": result.image_sha256[kind],
            }
            for kind in ("total", "detail")
        },
        "latest_pointer": str(result.latest_pointer_path),
        "state": str(result.state_path),
        "query_summary": result.query_summary,
        "delivery": "disabled",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成并校验一次群星杯每日榜图（不发送 QQ 消息）"
    )
    parser.add_argument("--run-time", help="固定运行时刻，ISO 8601；默认当前新加坡时间")
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    parser.add_argument("--live-cache", type=Path, default=DEFAULT_LIVE_CACHE)
    parser.add_argument("--background", type=Path, default=DEFAULT_BACKGROUND)
    parser.add_argument("--export-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--latest-pointer", type=Path, default=DEFAULT_LATEST_POINTER)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--full", action="store_true", help="强制完整 Verse 查询")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers 必须大于 0")
    try:
        run_time = _parse_run_time(args.run_time)
        result = run_stars_cup_daily_export(
            run_time=run_time,
            roster_path=args.roster,
            live_cache_path=args.live_cache,
            background_path=args.background,
            export_root=args.export_root,
            state_root=args.state_root,
            latest_pointer_path=args.latest_pointer,
            workers=args.workers,
            full=args.full,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(_summary(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
