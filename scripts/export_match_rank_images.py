"""Generate the fixed two-image match ranking package.

Examples:
    python scripts/export_match_rank_images.py --sample
    python scripts/export_match_rank_images.py \
        --snapshot path/to/榜图数据.json \
        --output-dir ../比赛导出/正式榜图/STAR_2026_4X4/20260727_120000
    python scripts/export_match_rank_images.py \
        --db data/tournament_hub.sqlite3 --event-code STAR_2026_4X4 \
        --roster config/star_2026_4x4_teams.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.match_rank_image_service import (  # noqa: E402
    build_sample_snapshot,
    build_snapshot_from_db,
    render_match_rank_images,
)


DEFAULT_BACKGROUND = WORKSPACE_ROOT / "比赛导出" / "设计草图" / "统榜_共享背景_群星杯与尚竞之勇_v8_1920x1080.png"


def _safe_name(value: str) -> str:
    return "".join("_" if char in '\\/:*?"<>|' else char for char in value)


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("{} 必须是 JSON object".format(path))
    return value


def _read_only_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError("数据库不存在: {}".format(resolved))
    connection = sqlite3.connect("file:{}?mode=ro".format(resolved), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _default_output_dir(snapshot: dict) -> Path:
    event_code = snapshot.get("event", {}).get("event_code") or "match_rank"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return WORKSPACE_ROOT / "比赛导出" / "正式榜图" / _safe_name(str(event_code)) / stamp


def main() -> int:
    parser = argparse.ArgumentParser(description="生成比赛总榜和六队明细两张榜图")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sample", action="store_true", help="使用内置样例数据生成预览")
    source.add_argument("--snapshot", type=Path, help="规范化榜图快照 JSON")
    source.add_argument("--db", type=Path, help="只读赛事数据库；需同时指定 --event-code 和 --roster")
    parser.add_argument("--event-code", help="--db 模式下的赛事编码")
    parser.add_argument("--roster", type=Path, help="--db 模式下的队伍/成员名单 JSON")
    parser.add_argument("--background", type=Path, default=DEFAULT_BACKGROUND, help="独立背景 PNG")
    parser.add_argument("--output-dir", type=Path, help="输出目录；不指定时使用比赛导出/正式榜图/赛事编码/时间戳")
    args = parser.parse_args()

    connection = None
    try:
        if args.sample:
            snapshot = build_sample_snapshot()
            default_dir = WORKSPACE_ROOT / "比赛导出" / "设计草图" / "动态榜图样例_v1"
        elif args.snapshot:
            snapshot = _load_json(args.snapshot)
            default_dir = _default_output_dir(snapshot)
        else:
            if not args.event_code or not args.roster:
                parser.error("--db 模式必须同时指定 --event-code 和 --roster")
            roster = _load_json(args.roster)
            connection = _read_only_connection(args.db)
            snapshot = build_snapshot_from_db(connection, args.event_code, roster)
            default_dir = _default_output_dir(snapshot)

        output_dir = args.output_dir or default_dir
        paths = render_match_rank_images(snapshot, output_dir, args.background)
        print(json.dumps(paths, ensure_ascii=False, indent=2))
        return 0
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
