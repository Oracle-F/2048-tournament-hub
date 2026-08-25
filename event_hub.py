"""群星杯专用赛事入口。

这个入口只负责本次群星杯的固定工作流：导入前一晚确定的分队名单、
补录人工成绩，以及从 2048verse 生成榜图。直接运行本文件会进入
交互式菜单；带子命令运行时仍可用于脚本化执行。

用法示例：

    python event_hub.py import-roster /home/oracle_f/下载/群星杯分队.xlsx
    python event_hub.py supplement --player XLB --score 500000 --board-sum 3000
    python event_hub.py query
    python event_hub.py export

Excel 解析只使用 Python 标准库，因此 Fedora 上不要求额外安装
openpyxl；导入器读取第一个有内容的工作表。榜图沿用项目原有计分器的
时间窗与盘面规则。`query` 通过并发、连接复用和增量缓存读取
2048verse；`export` 只使用最后一次成功查询的缓存生成图片。只有显式
提供 `--db` 时才使用本地数据库离线生成。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import local
from time import sleep
from zipfile import ZipFile
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:  # Fedora usually provides requests; keep a stdlib fallback.
    requests = None


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_ROSTER = PROJECT_ROOT / "config" / "annual_4x4_2026_roster.json"
DEFAULT_DB = PROJECT_ROOT / "data" / "tournament_hub.sqlite3"
DEFAULT_LIVE_CACHE = PROJECT_ROOT / "data" / "tmp" / "annual_4x4_2026_live_cache.json"
DEFAULT_QUERY_FAILURE_REPORT = PROJECT_ROOT / "data" / "tmp" / "annual_4x4_2026_last_query_failure.json"
DEFAULT_BACKGROUND = WORKSPACE_ROOT / "比赛导出" / "设计草图" / "统榜_共享背景_群星杯与尚竞之勇_v8_1920x1080.png"
DEFAULT_OUTPUT_ROOT = WORKSPACE_ROOT / "比赛导出" / "正式榜图" / "群星杯_2026"
LOCAL_TIMEZONE = timezone(timedelta(hours=8))

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCORER_ROOT = WORKSPACE_ROOT / "计分器"
if str(SCORER_ROOT) not in sys.path:
    sys.path.insert(0, str(SCORER_ROOT))

from services.match_rank_image_service import (  # noqa: E402
    build_snapshot_from_competition_roster,
    build_snapshot_from_roster_records,
    render_match_rank_images,
)
from tournament_common import (  # noqa: E402
    REQUEST_TIMEOUT,
    USER_AGENT,
    build_user_url,
    fetch_json as legacy_fetch_json,
    flatten_board,
    game_is_within_window,
    parse_iso_time,
)


XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_CELL_REF_RE = re.compile(r"^([A-Z]+)([0-9]+)$")
_TIER_RE = re.compile(r"第\s*(\d+)\s*档")
_TEAM_CODE_RE = re.compile(r"^\s*([A-Za-z])\s*队\s*$")
# This is a one-off rule for the imported 群星杯 roster.  XLB has no usable
# Verse account, so an absent database record must never be treated as a
# failed lookup or a reason to remove the player from the competition.
MANUAL_ONLY_ACCOUNTS = {"xlb"}
LIVE_FETCH_WORKERS = 8
LIVE_FETCH_MAX_WORKERS = 32
LIVE_FETCH_RETRIES = 4
LIVE_FAILED_PLAYER_RETRY_ROUNDS = 2
LIVE_FAILED_RETRY_WORKERS = 2
LIVE_FETCH_PAGE_SIZE = 50
LIVE_CACHE_SCHEMA_VERSION = 2
LIVE_RECENT_QUERY_WINDOW = timedelta(hours=24)
LIVE_RECENT_QUERY_THRESHOLD = timedelta(hours=23, minutes=30)
LIVE_CATCH_UP_OVERLAP = timedelta(minutes=30)
_HTTP_THREAD_LOCAL = local()


def _local_now() -> str:
    return datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S%z")


def _parse_local_datetime(value, label: str = "时间") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{}不能为空".format(label))
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("{}格式无效: {}".format(label, value)) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def _manual_record_values(record: dict, player_label: str) -> tuple[int, int]:
    try:
        score = int(record.get("score"))
        board_sum = int(record.get("board_sum"))
    except (TypeError, ValueError) as exc:
        raise ValueError("{} 的人工成绩必须是整数".format(player_label)) from exc
    if score < 0:
        raise ValueError("{} 的人工分数不能小于 0".format(player_label))
    if board_sum <= 0:
        raise ValueError("{} 的人工盘面和必须大于 0".format(player_label))
    return score, board_sum


def _cell_column(cell_ref: str) -> int:
    match = _CELL_REF_RE.match(cell_ref.upper())
    if not match:
        raise ValueError("无法解析 Excel 单元格地址: {}".format(cell_ref))
    output = 0
    for char in match.group(1):
        output = output * 26 + ord(char) - ord("A") + 1
    return output - 1


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(text.text or "" for text in item.iter("{%s}t" % XLSX_NS))
        for item in root.findall("{%s}si" % XLSX_NS)
    ]


def _xlsx_cell_value(cell: ET.Element, strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.iter("{%s}t" % XLSX_NS))
    value = cell.find("{%s}v" % XLSX_NS)
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        index = int(value.text)
        if index >= len(strings):
            raise ValueError("Excel 共享字符串索引越界: {}".format(index))
        return strings[index]
    if cell_type == "b":
        return "是" if value.text == "1" else "否"
    return value.text


def _read_first_nonempty_xlsx_sheet(path: Path) -> list[list[str]]:
    try:
        archive = ZipFile(path)
    except Exception as exc:  # pragma: no cover - ZipFile 的具体异常随 Python 版本变化
        raise ValueError("不是可读取的 .xlsx 文件: {}".format(path)) from exc

    with archive:
        strings = _shared_strings(archive)
        sheet_names = sorted(
            name
            for name in archive.namelist()
            if re.match(r"^xl/worksheets/sheet\d+\.xml$", name)
        )
        for sheet_name in sheet_names:
            root = ET.fromstring(archive.read(sheet_name))
            row_values: dict[int, dict[int, str]] = {}
            max_column = 0
            for row in root.findall(".//{%s}sheetData/{%s}row" % (XLSX_NS, XLSX_NS)):
                row_number = int(row.attrib.get("r", len(row_values) + 1))
                values: dict[int, str] = {}
                for cell in row.findall("{%s}c" % XLSX_NS):
                    column = _cell_column(cell.attrib.get("r", ""))
                    values[column] = _xlsx_cell_value(cell, strings).strip()
                    max_column = max(max_column, column)
                row_values[row_number] = values
            if not any(value for values in row_values.values() for value in values.values()):
                continue
            output = []
            for row_number in range(1, max(row_values) + 1):
                values = row_values.get(row_number, {})
                output.append([values.get(column, "") for column in range(max_column + 1)])
            return output
    raise ValueError("Excel 中没有找到有内容的工作表: {}".format(path))


def _team_code(team_name: str, fallback_index: int) -> str:
    match = _TEAM_CODE_RE.match(team_name)
    return match.group(1).upper() if match else chr(ord("A") + fallback_index)


def _validate_roster(roster: dict) -> dict:
    if not isinstance(roster, dict):
        raise ValueError("群星杯名单必须是 JSON object")
    competition = roster.get("competition")
    if not isinstance(competition, dict):
        raise ValueError("群星杯名单缺少 competition 配置")
    event_start = _parse_local_datetime(competition.get("start_time"), "比赛开始时间")
    event_end = _parse_local_datetime(competition.get("end_time"), "比赛结束时间")
    if event_end <= event_start:
        raise ValueError("比赛结束时间必须晚于开始时间")
    try:
        required_games = int(roster.get("required_games") or 3)
    except (TypeError, ValueError) as exc:
        raise ValueError("required_games 必须是整数 3") from exc
    if required_games != 3:
        raise ValueError("本次群星杯固定按最高分三局计分，required_games 必须是 3")
    roster["required_games"] = 3
    teams = roster.get("teams")
    if not isinstance(teams, list) or len(teams) != 6:
        raise ValueError("群星杯名单必须正好包含 6 队")

    seen_players: set[str] = set()
    seen_codes: set[str] = set()
    expected_tiers = set(range(1, 13))
    for team in teams:
        if not isinstance(team, dict):
            raise ValueError("队伍条目必须是 object")
        code = str(team.get("code") or "").strip().upper()
        name = str(team.get("name") or "").strip()
        players = team.get("players")
        if code not in set("ABCDEF"):
            raise ValueError("队伍编码必须是 A–F，收到: {}".format(code or "空"))
        if code in seen_codes:
            raise ValueError("队伍编码重复: {}".format(code))
        seen_codes.add(code)
        team["code"] = code
        team["name"] = name
        if not name or not isinstance(players, list) or len(players) != 12:
            raise ValueError("{} 队必须正好包含 12 名玩家".format(name or code or "未知队伍"))
        tiers = set()
        for player in players:
            username = str(player.get("username") or player.get("verse") or "").strip()
            tier = player.get("tier")
            if not username:
                raise ValueError("{} 队存在空玩家账号".format(name))
            if not isinstance(tier, int) or tier not in expected_tiers:
                raise ValueError("{} 队的玩家 {} 档位无效: {}".format(name, username, tier))
            if tier in tiers:
                raise ValueError("{} 队重复出现第 {} 档".format(name, tier))
            tiers.add(tier)
            identity = username.casefold()
            if identity in seen_players:
                raise ValueError("玩家重复出现在多个队伍: {}".format(username))
            seen_players.add(identity)
            player["username"] = username
            verse = str(player.get("verse") or "").strip()
            player["verse"] = verse or username
            player["number"] = "{:02d}".format(tier)
            if identity in MANUAL_ONLY_ACCOUNTS:
                player["manual_only"] = True
                player.setdefault("manual_note", "2048verse 无可用账号，只能人工补录成绩")
            raw_manual_records = player.get("manual_records")
            manual_records = [] if raw_manual_records is None else raw_manual_records
            if not isinstance(manual_records, list):
                raise ValueError("{} 队的玩家 {} 的 manual_records 必须是数组".format(name, username))
            if raw_manual_records is not None or player.get("manual_only"):
                player["manual_records"] = manual_records
            for record in manual_records:
                if not isinstance(record, dict) or record.get("score") in (None, "") or record.get("board_sum") in (None, ""):
                    raise ValueError("{} 队的玩家 {} 存在不完整的人工成绩记录".format(name, username))
                score, board_sum = _manual_record_values(record, "{} 队玩家 {}".format(name, username))
                record["score"] = score
                record["board_sum"] = board_sum
                started_at = (
                    _parse_local_datetime(record["started_at"], "{} 开始时间".format(username))
                    if record.get("started_at")
                    else None
                )
                ended_at = (
                    _parse_local_datetime(record["ended_at"], "{} 结束时间".format(username))
                    if record.get("ended_at")
                    else None
                )
                if started_at and ended_at and started_at > ended_at:
                    raise ValueError("{} 的人工成绩开始时间晚于结束时间".format(username))
                for timestamp in (started_at, ended_at):
                    if timestamp and not event_start <= timestamp < event_end:
                        raise ValueError("{} 的人工成绩时间不在比赛时间内".format(username))
        if tiers != expected_tiers:
            missing = sorted(expected_tiers - tiers)
            raise ValueError("{} 队没有完整覆盖第 1–12 档，缺少: {}".format(name, missing))
        team["players"].sort(key=lambda player: player["tier"])
    if seen_codes != set("ABCDEF"):
        raise ValueError("群星杯名单必须完整包含 A–F 六队")
    roster["teams"].sort(key=lambda team: team["code"])
    return roster


def roster_from_xlsx(path: Path) -> dict:
    """把群星杯分队 Excel 转成稳定的专用 JSON 名单。"""

    rows = _read_first_nonempty_xlsx_sheet(path)
    if len(rows) < 3 or not rows[0] or not rows[0][0]:
        raise ValueError("Excel 第一行必须包含赛事标题")
    headers = rows[1][1:13]
    tiers = []
    for header in headers:
        match = _TIER_RE.search(header)
        if not match:
            raise ValueError("Excel 第二行必须是第 1–12 档，无法识别: {}".format(header))
        tiers.append(int(match.group(1)))
    if tiers != list(range(1, 13)):
        raise ValueError("Excel 档位必须按第 1–12 档顺序排列，实际为: {}".format(tiers))

    data_rows = [row for row in rows[2:] if row and row[0].strip()]
    if len(data_rows) != 6:
        raise ValueError("Excel 必须有 6 行队伍，实际找到 {} 行".format(len(data_rows)))

    teams = []
    for team_index, row in enumerate(data_rows):
        team_name = row[0].strip()
        player_values = row[1:13]
        if len(player_values) != 12 or any(not value.strip() for value in player_values):
            raise ValueError("{} 必须完整填写 12 名玩家".format(team_name))
        code = _team_code(team_name, team_index)
        players = [
            {
                "number": "{:02d}".format(index),
                "tier": tier,
                "username": username.strip(),
                "verse": username.strip(),
                **(
                    {
                        "manual_only": True,
                        "manual_note": "2048verse 无可用账号，只能人工补录成绩",
                        "manual_records": [],
                    }
                    if username.casefold() in MANUAL_ONLY_ACCOUNTS
                    else {}
                ),
            }
            for index, (tier, username) in enumerate(zip(tiers, player_values), 1)
        ]
        teams.append(
            {
                "code": code,
                "name": team_name,
                "expected_players": 12,
                "players": players,
            }
        )

    roster = {
        "competition": {
            "code": "annual_4x4_2026",
            "title": "群星杯 · 2026 4×4 团体赛",
            "subtitle": "2026.07.27 — 08.24 · 2048 4×4 团体赛",
            "start_time": "2026-07-27 00:00:00+08:00",
            "end_time": "2026-08-24T00:00:00+08:00",
        },
        "as_of": "",
        "required_games": 3,
        "roster_source": {
            "file_name": path.name,
            "format": "xlsx",
            "imported_at": _local_now(),
            "tier_rule": "每队第 1–12 档各一人",
        },
        "teams": teams,
        "individuals": [],
    }
    return _validate_roster(roster)


def load_roster(path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError("名单文件不存在: {}".format(path))
    if path.suffix.lower() == ".xlsx":
        return roster_from_xlsx(path)
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8") as handle:
            return _validate_roster(json.load(handle))
    raise ValueError("当前群星杯入口支持 .xlsx 或标准 .json 名单，收到: {}".format(path.suffix or "无扩展名"))


def write_roster(roster: dict, output: Path, force: bool = False) -> Path:
    output = output.expanduser().resolve()
    if output.exists() and not force:
        raise FileExistsError("名单文件已存在；确认覆盖时请加 --force: {}".format(output))
    roster = _validate_roster(roster)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(roster, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def _merge_existing_manual_records(roster: dict, existing_path: Path) -> dict:
    """Keep verified supplements when a refreshed roster is imported."""

    if not existing_path.exists():
        return roster
    existing = _load_json(existing_path)

    def is_organizer_screenshot(record: dict) -> bool:
        return (
            isinstance(record, dict)
            and str(record.get("source") or "").strip()
            == "organizer_approved_screenshot"
        )

    def record_identity(record: dict) -> str:
        # Organizer-approved screenshots carry a stable source id.  Keep the
        # old full-payload fallback for legacy XLB/manual entries that predate
        # the idempotency field.
        source_record_id = record.get("source_record_id") if isinstance(record, dict) else None
        if source_record_id not in (None, ""):
            return "source_record_id:{}".format(source_record_id)
        return "payload:{}".format(json.dumps(record, ensure_ascii=False, sort_keys=True))

    old_records = {
        str(player.get("username") or player.get("verse") or "").casefold(): list(
            player.get("manual_records") or []
        )
        for team in existing.get("teams") or []
        for player in team.get("players") or []
        if player.get("manual_only")
        or any(is_organizer_screenshot(record) for record in player.get("manual_records") or [])
    }
    for team in roster.get("teams") or []:
        for player in team.get("players") or []:
            key = str(player.get("username") or player.get("verse") or "").casefold()
            if key not in old_records:
                continue
            merged = []
            seen = set()
            for record in [*(old_records[key]), *(player.get("manual_records") or [])]:
                identity = record_identity(record)
                if identity in seen:
                    continue
                seen.add(identity)
                merged.append(record)
            player["manual_records"] = merged
    return roster


def import_roster(source: Path, output: Path, force: bool = False) -> tuple[dict, Path]:
    roster = load_roster(source)
    resolved_output = output.expanduser().resolve()
    if resolved_output.exists() and force:
        roster = _merge_existing_manual_records(roster, resolved_output)
    return roster, write_roster(roster, resolved_output, force=force)


def supplement_manual_record(
    roster_path: Path,
    player_name: str,
    score: int,
    board_sum: int,
    started_at: str | None = None,
    ended_at: str | None = None,
    note: str | None = None,
) -> Path:
    """Append one verified manual result to a manual-only roster player."""

    roster = _load_json(roster_path)
    target = None
    for team in roster.get("teams") or []:
        for player in team.get("players") or []:
            username = str(player.get("username") or player.get("verse") or "")
            if username.casefold() == player_name.casefold():
                target = player
                break
        if target is not None:
            break
    if target is None:
        raise ValueError("名单中不存在玩家: {}".format(player_name))
    if not target.get("manual_only"):
        raise ValueError("{} 不是人工补录玩家；普通玩家应由 Verse 自动查询".format(player_name))
    score, board_sum = _manual_record_values(
        {"score": score, "board_sum": board_sum},
        player_name,
    )
    parsed_started = _parse_local_datetime(started_at, "开始时间") if started_at else None
    parsed_ended = _parse_local_datetime(ended_at, "结束时间") if ended_at else None
    if parsed_started and parsed_ended and parsed_started > parsed_ended:
        raise ValueError("人工成绩开始时间不能晚于结束时间")
    competition = roster["competition"]
    event_start = _parse_local_datetime(competition["start_time"], "比赛开始时间")
    event_end = _parse_local_datetime(competition["end_time"], "比赛结束时间")
    for timestamp in (parsed_started, parsed_ended):
        if timestamp and not event_start <= timestamp < event_end:
            raise ValueError("人工成绩时间不在比赛时间内")
    duplicate_key = (score, board_sum, started_at or "", ended_at or "")
    for existing in target.get("manual_records") or []:
        existing_key = (
            int(existing["score"]),
            int(existing["board_sum"]),
            str(existing.get("started_at") or ""),
            str(existing.get("ended_at") or ""),
        )
        if existing_key == duplicate_key:
            raise ValueError("{} 已存在相同的人工成绩，未重复写入".format(player_name))
    record = {
        "score": score,
        "board_sum": board_sum,
        "source": "manual",
        "recorded_at": _local_now(),
    }
    if started_at:
        record["started_at"] = started_at
    if ended_at:
        record["ended_at"] = ended_at
    if note:
        record["note"] = note
    target.setdefault("manual_records", []).append(record)
    return write_roster(roster, roster_path, force=True)


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return _validate_roster(json.load(handle))


def _read_only_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError("数据库不存在: {}".format(resolved))
    connection = sqlite3.connect("file:{}?mode=ro".format(resolved), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _default_output_dir() -> Path:
    return DEFAULT_OUTPUT_ROOT / datetime.now(LOCAL_TIMEZONE).strftime("%Y%m%d_%H%M%S")


def _parse_event_datetime(value: str) -> datetime:
    return _parse_local_datetime(value)


def _board_sum_from_verse_game(game: dict) -> int | None:
    try:
        values = flatten_board(game.get("board") or [])
    except (TypeError, ValueError):
        return None
    return sum(values) if values else None


def _verse_game_record(game: dict) -> dict | None:
    if not isinstance(game, dict):
        return None
    score = None
    for key in ("score", "final_score", "raw_score"):
        value = game.get(key)
        if value not in (None, ""):
            try:
                score = int(value)
            except (TypeError, ValueError):
                score = None
            if score is not None:
                break
    board_sum = _board_sum_from_verse_game(game)
    ended_at = game.get("played_at") or game.get("ended_at") or game.get("end_time")
    if score is None or score < 0 or board_sum is None or board_sum <= 0 or not ended_at:
        return None
    started_at = None
    for key in ("started_at", "created_at", "start_time", "startedAt", "createdAt"):
        if game.get(key):
            started_at = game[key]
            break
    # Verse may omit a start timestamp.  Keep the existing end-time candidate
    # behavior so the leaderboard remains compatible with the working scorer,
    # but do not treat the fallback as proof that the game started in-window;
    # human review must record any confirmed/unsupported exclusion below.
    started_at = started_at or ended_at
    return {
        "record_id": game.get("id"),
        "score": score,
        "board_sum": board_sum,
        "started_at": started_at,
        "ended_at": ended_at,
        "source": "verse_live",
    }


class LiveFetchError(RuntimeError):
    """Raised when fresh Verse data cannot be read safely."""


def _http_session():
    if requests is None:
        return None
    session = getattr(_HTTP_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )
        _HTTP_THREAD_LOCAL.session = session
    return session


def _reset_http_session() -> None:
    session = getattr(_HTTP_THREAD_LOCAL, "session", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
        _HTTP_THREAD_LOCAL.session = None


def _retry_delay(attempt: int, retry_after=None) -> float:
    if retry_after not in (None, ""):
        try:
            return max(0.5, min(float(retry_after), 30.0))
        except (TypeError, ValueError):
            pass
    return min(0.75 * (2**attempt) + random.uniform(0.15, 0.65), 10.0)


def _fetch_json_fast(url: str) -> dict:
    """Fetch one Verse page with per-worker connection reuse and retries."""

    last_error = None
    for attempt in range(LIVE_FETCH_RETRIES):
        session = _http_session()
        if session is None:
            payload = legacy_fetch_json(url)
            if isinstance(payload, dict):
                return payload
            last_error = "2048verse 未返回可用数据"
            if attempt + 1 < LIVE_FETCH_RETRIES:
                sleep(_retry_delay(attempt))
            continue
        try:
            response = session.get(url, timeout=(5, REQUEST_TIMEOUT))
            status = int(response.status_code)
            if status in {408, 425, 429} or 500 <= status < 600:
                last_error = "HTTP {}".format(status)
                retry_after = response.headers.get("Retry-After")
                _reset_http_session()
                if attempt + 1 < LIVE_FETCH_RETRIES:
                    sleep(_retry_delay(attempt, retry_after))
                    continue
                break
            if status >= 400:
                raise LiveFetchError("2048verse 返回 HTTP {}".format(status))
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("返回内容不是 JSON object")
            return payload
        except LiveFetchError:
            raise
        except Exception as exc:
            last_error = exc
            _reset_http_session()
            if attempt + 1 < LIVE_FETCH_RETRIES:
                sleep(_retry_delay(attempt))
    raise LiveFetchError("2048verse 请求失败: {}".format(last_error))


def _payload_total_games(payload: dict) -> int | None:
    value = payload.get("totalGames")
    if value in (None, ""):
        value = payload.get("total_games")
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _fetch_games_for_window_fast(
    username: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Read date-sorted pages, stopping as soon as older games are reached."""

    page = 1
    collected = []
    seen_ids = set()
    seen_game_count = 0

    while True:
        payload = _fetch_json_fast(build_user_url(username, "4x4", page))
        games = payload.get("games")
        if not isinstance(games, list):
            raise LiveFetchError("{} 的 games 字段格式异常".format(username))
        if not games:
            break

        reached_before_window = False
        new_game_count = 0
        for game in games:
            if not isinstance(game, dict):
                continue
            game_id = game.get("id")
            identity = (
                "id:{}".format(game_id)
                if game_id not in (None, "")
                else "fallback:{}:{}:{}".format(
                    game.get("played_at"),
                    game.get("score"),
                    game.get("final_score"),
                )
            )
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            seen_game_count += 1
            new_game_count += 1

            include, started_at, ended_at = game_is_within_window(game, start, end)
            if include and started_at is not None and ended_at is not None and started_at > ended_at:
                raise LiveFetchError(
                    "{} 存在开始时间晚于结束时间的对局，已保留旧缓存".format(username)
                )
            if include:
                collected.append(game)
            if ended_at is not None and ended_at < start:
                reached_before_window = True

        total_games = _payload_total_games(payload)
        if reached_before_window:
            break
        if total_games is not None and seen_game_count >= total_games:
            break
        if total_games is None and len(games) < LIVE_FETCH_PAGE_SIZE:
            break
        if new_game_count == 0:
            raise LiveFetchError(
                "{} 第 {} 页未返回新对局，已停止翻页以避免无限查询".format(username, page)
            )
        page += 1

    collected.sort(key=lambda item: parse_iso_time(item["played_at"]))
    return collected


def _fetch_one_live_player_records(username: str, start: datetime, end: datetime) -> list[dict]:
    games = _fetch_games_for_window_fast(username, start, end)
    normalized = []
    for game in games or []:
        record = _verse_game_record(game)
        if record is None:
            raise LiveFetchError(
                "{} 存在无法解析分数、盘面或结束时间的比赛内对局，已保留旧缓存".format(username)
            )
        normalized.append(record)
    return normalized


def _live_cache_identity(competition: dict) -> dict:
    return {
        "competition_code": str(competition.get("code") or ""),
        "variant": "4x4",
        "start_time": _parse_event_datetime(competition.get("start_time")).isoformat(),
        "end_time": _parse_event_datetime(competition.get("end_time")).isoformat(),
    }


def _empty_live_cache(competition: dict) -> dict:
    return {
        "schema_version": LIVE_CACHE_SCHEMA_VERSION,
        **_live_cache_identity(competition),
        "updated_at": "",
        "last_successful_query_at": "",
        "query_history": [],
        "excluded_records": [],
        "players": {},
    }


def _normalize_excluded_records(value) -> list[dict]:
    """Return a stable, de-duplicated list of manually excluded API records."""

    if not isinstance(value, list):
        return []
    normalized = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        record_id = entry.get("record_id")
        if record_id in (None, ""):
            continue
        identity = str(record_id)
        if identity in seen:
            continue
        seen.add(identity)
        item = dict(entry)
        item["record_id"] = record_id
        normalized.append(item)
    return normalized


def _excluded_record_ids(value) -> set[str]:
    return {
        str(entry["record_id"])
        for entry in _normalize_excluded_records(value)
        if entry.get("record_id") not in (None, "")
    }


def _upgrade_live_cache(payload: dict, expected: dict) -> dict | None:
    if payload.get("schema_version") != 1:
        return None
    for key in ("competition_code", "variant", "start_time", "end_time"):
        if payload.get(key) != expected.get(key):
            return None
    last_query = str(payload.get("updated_at") or "").strip()
    players = payload.get("players") if isinstance(payload.get("players"), dict) else {}
    if not last_query:
        checked = [
            str(entry.get("checked_through") or "").strip()
            for entry in players.values()
            if isinstance(entry, dict) and entry.get("checked_through")
        ]
        last_query = max(checked, default="")
    upgraded = {
        **expected,
        "updated_at": str(payload.get("updated_at") or last_query),
        "last_successful_query_at": last_query,
        "excluded_records": _normalize_excluded_records(payload.get("excluded_records")),
        "players": players,
    }
    if last_query:
        upgraded["query_history"] = [
            {
                "mode": "legacy_cache_upgrade",
                "range_start": expected["start_time"],
                "range_end": last_query,
                "query_started_at": last_query,
                "query_completed_at": last_query,
                "player_count": len(players),
                "fetched_record_count": None,
                "cached_record_count": sum(
                    len(entry.get("records") or [])
                    for entry in players.values()
                    if isinstance(entry, dict)
                ),
            }
        ]
    return upgraded


def _cache_records_fit_end(payload: dict, event_end: datetime) -> bool:
    """Confirm every cached record is strictly before a migrated event end."""

    players = payload.get("players")
    if not isinstance(players, dict):
        return False
    for entry in players.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("records"), list):
            return False
        for record in entry["records"]:
            if not isinstance(record, dict):
                return False
            ended_at = _record_end_time(record)
            if ended_at is None or ended_at >= event_end:
                return False
    return True


def _load_live_cache(path: Path | None, competition: dict) -> dict:
    expected = _empty_live_cache(competition)
    if path is None or not path.exists():
        return expected
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print("[缓存] 无法读取旧缓存，将完整刷新: {}".format(exc))
        return expected
    if not isinstance(payload, dict):
        print("[缓存] 格式不匹配，将完整刷新。")
        return expected
    if payload.get("schema_version") != LIVE_CACHE_SCHEMA_VERSION:
        upgraded = _upgrade_live_cache(payload, expected)
        if upgraded is not None:
            print("[缓存] 已兼容旧版缓存；下次成功查询后会升级格式。")
            return upgraded
        print("[缓存] 版本不匹配，将完整刷新。")
        return expected
    identity_keys = ("competition_code", "variant", "start_time")
    if all(payload.get(key) == expected.get(key) for key in identity_keys):
        if payload.get("end_time") != expected.get("end_time"):
            new_end = _cache_datetime(expected.get("end_time"))
            if new_end is not None and _cache_records_fit_end(payload, new_end):
                migrated = dict(payload)
                migrated["end_time"] = expected["end_time"]
                print("[缓存] 仅比赛结束边界变化；已保留原玩家记录、查询历史和 exclusions。")
                return migrated
            print("[缓存] 结束边界变化且旧记录无法确认均在新边界前，将完整刷新。")
            return expected
    else:
        print("[缓存] 比赛配置已变化，将完整刷新。")
        return expected
    if not isinstance(payload.get("players"), dict):
        return expected
    if not isinstance(payload.get("query_history"), list):
        payload["query_history"] = []
    payload["excluded_records"] = _normalize_excluded_records(payload.get("excluded_records"))
    return payload


def _record_identity(record: dict) -> str:
    record_id = record.get("record_id")
    if record_id not in (None, ""):
        return "id:{}".format(record_id)
    return "fallback:{}:{}:{}".format(
        record.get("ended_at"),
        record.get("score"),
        record.get("board_sum"),
    )


def _record_end_time(record: dict) -> datetime | None:
    value = record.get("ended_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _parse_event_datetime(value)
    except ValueError:
        return None


def _merge_live_records(
    cached: list[dict],
    refreshed: list[dict],
    event_start: datetime,
    event_end: datetime,
    excluded_record_ids: set[str] | None = None,
) -> list[dict]:
    merged = {}
    excluded = {str(value) for value in (excluded_record_ids or set())}
    for record in [*(cached or []), *(refreshed or [])]:
        if not isinstance(record, dict):
            continue
        record_id = record.get("record_id")
        if record_id not in (None, "") and str(record_id) in excluded:
            continue
        ended_at = _record_end_time(record)
        if ended_at is None or ended_at < event_start or ended_at >= event_end:
            continue
        merged[_record_identity(record)] = dict(record)
    return sorted(
        merged.values(),
        key=lambda record: (
            _record_end_time(record) or event_start,
            _record_identity(record),
        ),
    )


def _cache_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _parse_event_datetime(value)
    except ValueError:
        return None


def _select_live_query_window(
    cache: dict,
    event_start: datetime,
    event_end: datetime,
    *,
    full: bool = False,
    now: datetime | None = None,
) -> tuple[datetime, datetime, str]:
    query_end = min(
        (now or datetime.now(LOCAL_TIMEZONE)).astimezone(LOCAL_TIMEZONE),
        event_end,
    )
    if query_end < event_start:
        raise ValueError("比赛尚未开始，当前没有可查询的比赛时间窗")

    last_query = _cache_datetime(cache.get("last_successful_query_at"))
    if full or last_query is None:
        return event_start, query_end, "full"
    if last_query > query_end:
        raise ValueError(
            "当前时间早于上次成功查询的数据截至时间，请检查系统时间；旧缓存未改动"
        )
    elapsed = query_end - last_query
    if elapsed <= LIVE_RECENT_QUERY_THRESHOLD:
        return (
            max(event_start, query_end - LIVE_RECENT_QUERY_WINDOW),
            query_end,
            "recent_24h",
        )
    return (
        max(event_start, last_query - LIVE_CATCH_UP_OVERLAP),
        query_end,
        "catch_up",
    )


def _save_live_cache(
    path: Path,
    competition: dict,
    usernames: dict[str, str],
    records: dict[str, list[dict]],
    previous_cache: dict,
    query_entry: dict,
) -> None:
    history = list(previous_cache.get("query_history") or [])
    history.append(query_entry)
    payload = {
        **_empty_live_cache(competition),
        "updated_at": query_entry["query_completed_at"],
        "last_successful_query_at": query_entry["range_end"],
        "query_history": history,
        "excluded_records": _normalize_excluded_records(previous_cache.get("excluded_records")),
        "players": {
            key: {
                "username": usernames[key],
                "records": records.get(key, []),
            }
            for key in sorted(usernames)
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _live_roster_usernames(roster: dict) -> dict[str, str]:
    players = [player for team in roster.get("teams") or [] for player in team.get("players") or []]
    players.extend(roster.get("individuals") or [])
    usernames = {}
    for player in players:
        username = str(player.get("username") or player.get("verse") or "").strip()
        if not username or player.get("manual_only"):
            continue
        usernames[username.casefold()] = username
    return usernames


def _load_cached_live_records(
    roster: dict,
    cache_path: Path,
) -> tuple[dict[str, list[dict]], dict]:
    competition = roster.get("competition") if isinstance(roster.get("competition"), dict) else roster
    cache = _load_live_cache(cache_path, competition)
    last_success = cache.get("last_successful_query_at")
    if not last_success:
        raise ValueError("尚无成功查询记录，请先执行“查询最新成绩”")
    if _cache_datetime(last_success) is None:
        raise ValueError("查询缓存的数据截至时间损坏，请重新查询后再生成榜图")
    cached_players = cache.get("players") if isinstance(cache.get("players"), dict) else {}
    usernames = _live_roster_usernames(roster)
    invalid = []
    for key, username in usernames.items():
        entry = cached_players.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("records"), list):
            invalid.append(username)
            continue
        if any(not isinstance(record, dict) for record in entry["records"]):
            invalid.append(username)
    if invalid:
        raise ValueError(
            "缓存缺少或损坏 {} 名玩家（{}），请重新查询后再生成榜图".format(
                len(invalid),
                "、".join(invalid[:5]),
            )
        )
    excluded_record_ids = _excluded_record_ids(cache.get("excluded_records"))
    records = {
        key: [
            record
            for record in list(cached_players[key].get("records") or [])
            if not (
                isinstance(record, dict)
                and record.get("record_id") not in (None, "")
                and str(record.get("record_id")) in excluded_record_ids
            )
        ]
        for key in usernames
    }
    return records, cache


def _query_failure_report_path(cache_path: Path | None) -> Path | None:
    if cache_path is None:
        return None
    if cache_path == DEFAULT_LIVE_CACHE:
        return DEFAULT_QUERY_FAILURE_REPORT
    return cache_path.with_name(cache_path.stem + "_last_failure.json")


def _save_query_failure_report(
    cache_path: Path | None,
    query_start: datetime,
    query_end: datetime,
    workers: int,
    failures: list[tuple[tuple, str]],
) -> Path | None:
    path = _query_failure_report_path(cache_path)
    if path is None:
        return None
    payload = {
        "failed_at": datetime.now(LOCAL_TIMEZONE).isoformat(),
        "range_start": query_start.isoformat(),
        "range_end": query_end.isoformat(),
        "initial_workers": workers,
        "retry_rounds": LIVE_FAILED_PLAYER_RETRY_ROUNDS,
        "failures": [
            {
                "username": task[1],
                "error": error,
            }
            for task, error in failures
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _clear_query_failure_report(cache_path: Path | None) -> None:
    path = _query_failure_report_path(cache_path)
    if path is not None and path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _query_live_records_from_roster(
    roster: dict,
    *,
    cache_path: Path | None = DEFAULT_LIVE_CACHE,
    workers: int = LIVE_FETCH_WORKERS,
    full: bool = False,
    now: datetime | None = None,
) -> tuple[dict[str, list[dict]], dict]:
    """Query all roster players and atomically update the saved data cache."""

    competition = roster.get("competition") if isinstance(roster.get("competition"), dict) else roster
    start = _parse_event_datetime(competition.get("start_time"))
    end = _parse_event_datetime(competition.get("end_time"))
    if workers < 1 or workers > LIVE_FETCH_MAX_WORKERS:
        raise ValueError("并发数必须在 1–{} 之间".format(LIVE_FETCH_MAX_WORKERS))
    cache = _load_live_cache(cache_path, competition)
    excluded_record_ids = _excluded_record_ids(cache.get("excluded_records"))
    query_started = (now or datetime.now(LOCAL_TIMEZONE)).astimezone(LOCAL_TIMEZONE)
    query_start, query_end, mode = _select_live_query_window(
        cache,
        start,
        end,
        full=full,
        now=query_started,
    )
    cached_players = cache.get("players") if isinstance(cache.get("players"), dict) else {}
    records: dict[str, list[dict]] = {}
    usernames = _live_roster_usernames(roster)
    tasks = []
    for index, (key, username) in enumerate(usernames.items(), 1):
        cached_entry = cached_players.get(key) if isinstance(cached_players.get(key), dict) else {}
        cached_records = cached_entry.get("records") if isinstance(cached_entry.get("records"), list) else []
        if mode == "full":
            cached_records = []
        tasks.append((index, username, key, cached_records))

    total = len(tasks)
    worker_count = min(workers, total) if total else 0
    fetched_record_count = 0
    mode_label = {
        "full": "全部时间",
        "recent_24h": "近24小时",
        "catch_up": "断点续查",
    }[mode]

    def run_batch(batch: list[tuple], batch_workers: int, label: str) -> list[tuple[tuple, str]]:
        nonlocal fetched_record_count
        batch_failures = []
        with ThreadPoolExecutor(max_workers=max(1, min(batch_workers, len(batch)))) as executor:
            futures = {
                executor.submit(_fetch_one_live_player_records, task[1], query_start, query_end): task
                for task in batch
            }
            completed = 0
            for future in as_completed(futures):
                task = futures[future]
                index, username, key, cached_records = task
                completed += 1
                try:
                    refreshed = future.result()
                    # ``query_end`` is the current-time cutoff, so a record
                    # ending exactly at that instant is observable.  Keep the
                    # competition's real end boundary half-open.
                    merge_end = (
                        query_end
                        if query_end >= end
                        else query_end + timedelta(microseconds=1)
                    )
                    normalized = _merge_live_records(
                        cached_records,
                        refreshed,
                        start,
                        merge_end,
                        excluded_record_ids=excluded_record_ids,
                    )
                except Exception as exc:
                    batch_failures.append((task, str(exc)))
                    print(
                        "[{} {}/{}] {} 读取失败: {}".format(
                            label,
                            completed,
                            len(batch),
                            username,
                            exc,
                        )
                    )
                    continue
                records[key] = normalized
                fetched_record_count += len(refreshed)
                print(
                    "[{} {}/{}] {}：{}查询，累计 {} 局有效记录".format(
                        label,
                        completed,
                        len(batch),
                        username,
                        mode_label,
                        len(normalized),
                    )
                )
        return batch_failures

    failures = run_batch(tasks, worker_count or 1, "首轮")
    for retry_round in range(1, LIVE_FAILED_PLAYER_RETRY_ROUNDS + 1):
        if not failures:
            break
        retry_tasks = [task for task, _ in failures]
        retry_workers = min(LIVE_FAILED_RETRY_WORKERS, len(retry_tasks))
        print(
            "首轮仍有 {} 名玩家失败；等待后以 {} 路并发进行第 {} 次失败重试。".format(
                len(retry_tasks),
                retry_workers,
                retry_round,
            )
        )
        sleep(_retry_delay(retry_round))
        failures = run_batch(
            retry_tasks,
            retry_workers,
            "重试{}".format(retry_round),
        )

    if failures:
        names = "、".join(task[1] for task, _ in failures[:8])
        if len(failures) > 8:
            names += " 等 {} 人".format(len(failures))
        report_path = _save_query_failure_report(
            cache_path,
            query_start,
            query_end,
            workers,
            failures,
        )
        report_hint = "；失败详情: {}".format(report_path) if report_path else ""
        raise LiveFetchError(
            "{} 名玩家多轮重试后仍读取失败（{}），为避免错误榜图已停止生成；旧缓存未改动{}。".format(
                len(failures),
                names,
                report_hint,
            )
        )
    query_completed = datetime.now(LOCAL_TIMEZONE)
    query_entry = {
        "mode": mode,
        "range_start": query_start.isoformat(),
        "range_end": query_end.isoformat(),
        "query_started_at": query_started.isoformat(),
        "query_completed_at": query_completed.isoformat(),
        "player_count": total,
        "fetched_record_count": fetched_record_count,
        "cached_record_count": sum(len(items) for items in records.values()),
    }
    if cache_path is not None:
        _save_live_cache(
            cache_path,
            competition,
            usernames,
            records,
            cache,
            query_entry,
        )
        _clear_query_failure_report(cache_path)
    return records, query_entry


def query_live_scores(
    roster_path: Path,
    *,
    cache_path: Path = DEFAULT_LIVE_CACHE,
    workers: int = LIVE_FETCH_WORKERS,
    full: bool = False,
) -> dict:
    roster = _load_json(roster_path)
    records, query_entry = _query_live_records_from_roster(
        roster,
        cache_path=cache_path,
        workers=workers,
        full=full,
    )
    return {
        "cache": str(cache_path),
        **query_entry,
        "cached_record_count": sum(len(items) for items in records.values()),
    }


def export_rank_images(
    roster_path: Path,
    db_path: Path | None,
    output_dir: Path | None,
    background: Path,
    as_of: str | None,
    source: str | None = None,
    live_cache_path: Path | None = DEFAULT_LIVE_CACHE,
) -> dict[str, str]:
    roster = _load_json(roster_path)
    if as_of:
        roster = dict(roster)
        roster["as_of"] = as_of
    source = source or ("db" if db_path is not None else "live")
    if source == "live":
        if live_cache_path is None:
            raise ValueError("生成榜图需要已保存的 Verse 查询缓存")
        records, cache = _load_cached_live_records(roster, live_cache_path)
        if not as_of:
            roster = dict(roster)
            roster["as_of"] = cache["last_successful_query_at"]
        snapshot = build_snapshot_from_roster_records(roster, records)
        return render_match_rank_images(snapshot, output_dir or _default_output_dir(), background)
    if source != "db":
        raise ValueError("未知成绩来源: {}".format(source))
    connection = _read_only_connection(db_path or DEFAULT_DB)
    try:
        snapshot = build_snapshot_from_competition_roster(connection, roster)
        return render_match_rank_images(snapshot, output_dir or _default_output_dir(), background)
    finally:
        connection.close()


def _prompt(label: str, default: str | None = None) -> str:
    suffix = " [{}]".format(default) if default else ""
    value = input("{}{}: ".format(label, suffix)).strip()
    return value or (default or "")


def _prompt_int(label: str) -> int:
    while True:
        value = _prompt(label)
        try:
            return int(value)
        except ValueError:
            print("请输入整数。")


def _prompt_yes_no(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input("{} [{}]: ".format(label, hint)).strip().casefold()
    if not value:
        return default
    return value in {"y", "yes", "是", "1"}


def _interactive_menu() -> int:
    """Run the terminal menu used when Fedora users launch the file directly."""

    print("\n群星杯赛事 Hub")
    print("工作目录: {}".format(PROJECT_ROOT))
    while True:
        print("\n请选择操作：")
        print("  1. 导入/更新比赛名单")
        print("  2. 补录人工成绩（目前主要用于 XLB）")
        print("  3. 查询最新 Verse 成绩并保存")
        print("  4. 从最后一次查询结果生成榜图")
        print("  5. 查询比赛全部时间并覆盖保存")
        print("  0. 退出")
        try:
            choice = input("请输入选项: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0

        if choice in {"0", "q", "quit", "退出"}:
            print("已退出。")
            return 0
        try:
            if choice == "1":
                source = Path(_prompt("Excel 或 JSON 名单路径", "/home/oracle_f/下载/群星杯分队.xlsx"))
                output = Path(_prompt("标准名单输出路径", str(DEFAULT_ROSTER)))
                force = output.exists() and _prompt_yes_no("标准名单已存在，是否覆盖", default=False)
                roster, output_path = import_roster(source, output, force=force)
                print("导入成功：{} 队，{} 人；每队第 1–12 档各一人。".format(len(roster["teams"]), sum(len(team["players"]) for team in roster["teams"])))
                print("已保存: {}".format(output_path))
            elif choice == "2":
                roster_path = Path(_prompt("标准名单路径", str(DEFAULT_ROSTER)))
                player = _prompt("玩家名称", "XLB")
                score = _prompt_int("该局分数")
                board_sum = _prompt_int("该局盘面和")
                started_at = _prompt("开始时间（可留空）")
                ended_at = _prompt("结束时间（可留空）")
                note = _prompt("备注（可留空）")
                output_path = supplement_manual_record(
                    roster_path,
                    player,
                    score,
                    board_sum,
                    started_at=started_at or None,
                    ended_at=ended_at or None,
                    note=note or None,
                )
                print("补录成功，已保存: {}".format(output_path))
            elif choice == "3":
                print("正在按上次成功查询时间增量读取 2048verse；本操作只更新数据，不生成图片。")
                summary = query_live_scores(DEFAULT_ROSTER)
                print("查询成功并已保存：")
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            elif choice == "4":
                print("正在使用最后一次成功查询的数据生成榜图；本操作不会访问 2048verse。")
                paths = export_rank_images(
                    DEFAULT_ROSTER,
                    None,
                    None,
                    DEFAULT_BACKGROUND,
                    None,
                )
                print("榜图生成成功：")
                print(json.dumps(paths, ensure_ascii=False, indent=2))
            elif choice == "5":
                print("正在重新查询比赛开始至今的全部时间；成功后会替换旧成绩缓存。")
                summary = query_live_scores(DEFAULT_ROSTER, full=True)
                print("完整查询成功并已保存：")
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                print("无效选项，请输入 1、2、3、4、5 或 0。")
        except (FileNotFoundError, FileExistsError, ValueError, LiveFetchError, sqlite3.Error) as exc:
            print("操作失败: {}".format(exc))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="群星杯专用名单导入与榜图生成入口")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-roster", help="导入 Excel 分队名单为标准 JSON")
    import_parser.add_argument("source", type=Path, help="群星杯分队 .xlsx 或标准名单 .json")
    import_parser.add_argument("--output", type=Path, default=DEFAULT_ROSTER, help="标准名单输出路径")
    import_parser.add_argument("--force", action="store_true", help="允许覆盖已有标准名单")

    supplement_parser = subparsers.add_parser("supplement", help="为人工补录玩家追加一局成绩")
    supplement_parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER, help="标准 JSON 名单")
    supplement_parser.add_argument("--player", required=True, help="名单中的 Verse 名称，例如 XLB")
    supplement_parser.add_argument("--score", required=True, type=int, help="该局分数")
    supplement_parser.add_argument("--board-sum", required=True, type=int, help="该局盘面和")
    supplement_parser.add_argument("--started-at", help="可选：该局开始时间")
    supplement_parser.add_argument("--ended-at", help="可选：该局结束时间")
    supplement_parser.add_argument("--note", help="可选：人工记录备注")

    query_parser = subparsers.add_parser("query", help="查询 Verse 成绩并更新本地缓存，不生成图片")
    query_parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER, help="标准 JSON 名单")
    query_parser.add_argument("--cache", type=Path, default=DEFAULT_LIVE_CACHE, help="Verse 查询缓存")
    query_parser.add_argument(
        "--workers",
        type=int,
        default=LIVE_FETCH_WORKERS,
        help="Verse 并发读取数，默认 %(default)s，允许 1–{}".format(LIVE_FETCH_MAX_WORKERS),
    )
    query_parser.add_argument(
        "--full",
        action="store_true",
        help="查询比赛开始至今的全部时间并替换旧缓存记录",
    )

    export_parser = subparsers.add_parser("export", help="从最后一次查询缓存或本地数据库生成两张榜图")
    export_parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER, help="标准 JSON 名单")
    export_parser.add_argument("--source", choices=("live", "db"), help="成绩来源；默认无 --db 时使用 live")
    export_parser.add_argument("--db", type=Path, help="--source db 时使用的只读赛事数据库")
    export_parser.add_argument("--live-cache", type=Path, default=DEFAULT_LIVE_CACHE, help="最后一次成功的 Verse 查询缓存")
    export_parser.add_argument("--background", type=Path, default=DEFAULT_BACKGROUND, help="榜图背景 PNG")
    export_parser.add_argument("--output-dir", type=Path, help="输出目录；默认使用带时间戳的正式榜图目录")
    export_parser.add_argument("--as-of", help="覆盖榜图数据截至时间，例如 2026-07-27 12:00:00")
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return _interactive_menu()
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "import-roster":
            roster, output = import_roster(args.source, args.output, force=args.force)
            summary = {
                "roster": str(output),
                "teams": len(roster["teams"]),
                "players": sum(len(team["players"]) for team in roster["teams"]),
                "tiers_per_team": 12,
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.command == "supplement":
            output = supplement_manual_record(
                args.roster,
                args.player,
                args.score,
                args.board_sum,
                started_at=args.started_at,
                ended_at=args.ended_at,
                note=args.note,
            )
            print(json.dumps({"roster": str(output), "player": args.player, "source": "manual"}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "query":
            summary = query_live_scores(
                args.roster,
                cache_path=args.cache,
                workers=args.workers,
                full=args.full,
            )
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        paths = export_rank_images(
            args.roster,
            args.db,
            args.output_dir,
            args.background,
            args.as_of,
            source=args.source,
            live_cache_path=args.live_cache,
        )
        print(json.dumps(paths, ensure_ascii=False, indent=2))
        return 0
    except (FileNotFoundError, FileExistsError, ValueError, LiveFetchError, sqlite3.Error) as exc:
        print("错误: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
