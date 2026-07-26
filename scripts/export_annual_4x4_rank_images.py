"""Generate the one-off annual 4x4 competition ranking package.

The roster file is prepared the night before the competition.  This entry
does not look up an event code: it uses the fixed time window in that roster,
reads the database read-only, and only emits players listed in the roster.

Example:
    python scripts/export_annual_4x4_rank_images.py \
        --roster config/annual_4x4_2026_roster.template.json \
        --output-dir ../比赛导出/正式榜图/年度4x4_2026/20260727_120000
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
    build_snapshot_from_competition_roster,
    render_match_rank_images,
)


DEFAULT_DB = PROJECT_ROOT / "data" / "tournament_hub.sqlite3"
DEFAULT_BACKGROUND = WORKSPACE_ROOT / "比赛导出" / "设计草图" / "统榜_共享背景_群星杯与尚竞之勇_v8_1920x1080.png"


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


def _safe_name(value: str) -> str:
    return "".join("_" if char in '\\/:*?"<>|' else char for char in value)


def _default_output_dir(roster: dict) -> Path:
    competition = roster.get("competition") if isinstance(roster.get("competition"), dict) else roster
    code = competition.get("code") or roster.get("competition_code") or "annual_4x4_2026"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return WORKSPACE_ROOT / "比赛导出" / "正式榜图" / _safe_name(str(code)) / stamp


def main() -> int:
    parser = argparse.ArgumentParser(description="生成本次年度4×4赛事的总榜和六队明细榜图")
    parser.add_argument("--roster", type=Path, required=True, help="前一晚导入的本次比赛名单与时间窗 JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="只读赛事数据库")
    parser.add_argument("--background", type=Path, default=DEFAULT_BACKGROUND, help="独立背景 PNG")
    parser.add_argument("--output-dir", type=Path, help="输出目录；不指定时使用赛事编码/时间戳")
    args = parser.parse_args()

    roster = _load_json(args.roster)
    output_dir = args.output_dir or _default_output_dir(roster)
    connection = _read_only_connection(args.db)
    try:
        snapshot = build_snapshot_from_competition_roster(connection, roster)
        paths = render_match_rank_images(snapshot, output_dir, args.background)
    finally:
        connection.close()
    print(json.dumps(paths, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
