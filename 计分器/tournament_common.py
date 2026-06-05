import csv
import hashlib
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from PIL import Image, ImageDraw, ImageFont


LOCAL_TIMEZONE = timezone(timedelta(hours=8))
API_BASE_URL = "https://backend.2048verse.com/leaderboard/user"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.124 Safari/537.36"
)
REQUEST_TIMEOUT = 15
CHECK_INTERVAL = 3
HTML_REFRESH_SECONDS = 5
RESULT_SECRET = "2048verse-tournament-lite-v1"
NOTIFICATION_SECONDS = 5


@dataclass
class ScoreRule:
    points: int
    label: str
    required_tiles: Tuple[int, ...]
    full_board_level: int = 0


@dataclass
class EventInfo:
    event_id: str
    event_name: str
    source: str
    variant: str
    format_name: str
    start_time: datetime
    end_time: datetime
    duration: timedelta
    seal_time: Optional[datetime] = None


@dataclass
class ScoredGame:
    game_id: int
    username: str
    display_name: str
    variant: str
    raw_score: int
    points: int
    label: str
    end_time: datetime
    start_time: Optional[datetime]
    offset: timedelta
    board: List[List[int]]
    full_board_level: int


@dataclass
class PlayerResult:
    username: str
    display_name: str
    variant: str
    total_points: int
    scoring_game_count: int
    all_window_game_count: int
    total_full_boards: int
    games: List[ScoredGame]


SCORING_RULES = {
    "2x4": [
        ScoreRule(18, "512+256+128+64", (512, 256, 128, 64), 4),
        ScoreRule(17, "四阶满盘", (512, 256, 128, 32, 16, 8, 4, 2), 4),
        ScoreRule(15, "512+256+128", (512, 256, 128), 3),
        ScoreRule(14, "三阶满盘", (512, 256, 64, 32, 16, 8, 4, 2), 3),
        ScoreRule(12, "512+256+64", (512, 256, 64), 2),
        ScoreRule(11, "512+256", (512, 256), 2),
        ScoreRule(10, "二阶满盘", (512, 128, 64, 32, 16, 8, 4, 2), 2),
        ScoreRule(8, "512+128+64", (512, 128, 64), 1),
        ScoreRule(7, "512+128", (512, 128), 1),
        ScoreRule(6, "512", (512,), 1),
        ScoreRule(5, "一阶满盘", (256, 128, 64, 32, 16, 8, 4, 2), 1),
        ScoreRule(3, "256+128+64", (256, 128, 64)),
        ScoreRule(2, "256+128", (256, 128)),
        ScoreRule(1, "256", (256,)),
    ],
    "3x3": [
        ScoreRule(51, "四阶满盘", (1024, 512, 256, 64, 32, 16, 8, 4, 2), 4),
        ScoreRule(47, "1024+512+256+64", (1024, 512, 256, 64), 3),
        ScoreRule(45, "1024+512+256", (1024, 512, 256), 3),
        ScoreRule(43, "三阶满盘", (1024, 512, 128, 64, 32, 16, 8, 4, 2), 3),
        ScoreRule(39, "1024+512+128+64", (1024, 512, 128, 64), 2),
        ScoreRule(37, "1024+512+128", (1024, 512, 128), 2),
        ScoreRule(35, "1024+512+64", (1024, 512, 64), 2),
        ScoreRule(34, "1024+512", (1024, 512), 2),
        ScoreRule(32, "二阶满盘", (1024, 256, 128, 64, 32, 16, 8, 4, 2), 2),
        ScoreRule(28, "1024+256+128+64", (1024, 256, 128, 64), 1),
        ScoreRule(26, "1024+256+128", (1024, 256, 128), 1),
        ScoreRule(24, "1024+256+64", (1024, 256, 64), 1),
        ScoreRule(23, "1024+256", (1024, 256), 1),
        ScoreRule(21, "1024+128+64", (1024, 128, 64), 1),
        ScoreRule(20, "1024+128", (1024, 128), 1),
        ScoreRule(19, "1024", (1024,), 1),
        ScoreRule(17, "一阶满盘", (512, 256, 128, 64, 32, 16, 8, 4, 2), 1),
        ScoreRule(13, "512+256+128+64", (512, 256, 128, 64)),
        ScoreRule(11, "512+256+128", (512, 256, 128)),
        ScoreRule(9, "512+256+64", (512, 256, 64)),
        ScoreRule(8, "512+256", (512, 256)),
        ScoreRule(6, "512+128+64", (512, 128, 64)),
        ScoreRule(5, "512+128", (512, 128)),
        ScoreRule(4, "512", (512,)),
        ScoreRule(2, "256+128+64", (256, 128, 64)),
        ScoreRule(1, "256+128", (256, 128,)),
    ],
}


def now_local():
    return datetime.now(LOCAL_TIMEZONE)


def parse_iso_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(LOCAL_TIMEZONE)


def parse_start_time(value):
    value = value.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%H:%M:%S",
        "%H:%M",
    ]
    for time_format in formats:
        try:
            parsed = datetime.strptime(value, time_format)
            if time_format.startswith("%H"):
                today = now_local().date()
                parsed = datetime.combine(today, parsed.time())
            return parsed.replace(tzinfo=LOCAL_TIMEZONE)
        except ValueError:
            continue
    raise ValueError("时间格式无效，请输入 HH:MM、HH:MM:SS 或 YYYY-MM-DD HH:MM[:SS]")


def parse_duration_hours(value):
    hours = float(value.strip())
    if hours <= 0:
        raise ValueError("比赛总时长必须大于 0")
    return timedelta(hours=hours)


def parse_optional_seal_minutes(value, start_time):
    text = value.strip()
    if not text:
        return None
    minutes = float(text)
    if minutes <= 0:
        raise ValueError("封榜时间必须大于 0 分钟")
    return start_time + timedelta(minutes=minutes)


def format_datetime(value):
    return value.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def format_clock(delta):
    total_seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)


def format_offset(delta):
    total_seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{}:{:02d}:{:02d}".format(hours, minutes, seconds)


def format_compact_time(value):
    return value.astimezone(LOCAL_TIMEZONE).strftime("%Y%m%d_%H%M")


def build_event_info(variant, start_time, duration, seal_time=None):
    end_time = start_time + duration
    event_id = "{}_{}_{}".format(start_time.strftime("%Y%m%d"), variant, start_time.strftime("%H%M"))
    return EventInfo(
        event_id=event_id,
        event_name="2048 {} 比赛".format(variant),
        source="2048verse",
        variant=variant,
        format_name="timed_scoring",
        start_time=start_time,
        end_time=end_time,
        duration=duration,
        seal_time=seal_time,
    )


def get_status_text(now, start_time, end_time, duration):
    if now < start_time:
        return "未开始 {}".format(format_clock(start_time - now))
    if now <= end_time:
        return "进行中 {} / {}".format(format_clock(now - start_time), format_clock(duration))
    return "已结束"


def get_remaining_time_text(now, start_time, end_time):
    if now < start_time:
        return "--"
    if now <= end_time:
        return format_clock(end_time - now)
    return "00:00:00"


def get_seal_status_text(now, seal_time):
    if seal_time is None:
        return "本场比赛不进行封榜"
    if now < seal_time:
        return "将在 {} 封榜".format(format_datetime(seal_time)[11:19])
    return "已封榜"


def flatten_board(board):
    values = []
    for row in board or []:
        for cell in row:
            if cell:
                values.append(int(cell))
    values.sort(reverse=True)
    return values


def _tiles_sum(values):
    return sum(int(value) for value in values)


def _normalize_tiles(values):
    return tuple(sorted((int(value) for value in values if value), reverse=True))


def _first_full_board_peak(rules):
    full_board_rules = [rule for rule in rules if rule.full_board_level == 1]
    if not full_board_rules:
        return 0
    return min(max(int(value) for value in rule.required_tiles) for rule in full_board_rules)


def _collapse_tiles(values):
    counts = Counter(int(value) for value in values if value)
    if not counts:
        return ()

    max_value = max(counts)
    value = 2
    while value <= max_value:
        count = counts.get(value, 0)
        pairs, remainder = divmod(count, 2)
        if remainder:
            counts[value] = remainder
        else:
            counts.pop(value, None)
        if pairs:
            counts[value * 2] += pairs
            if value * 2 > max_value:
                max_value = value * 2
        value *= 2

    collapsed = []
    for tile, count in counts.items():
        collapsed.extend([tile] * count)
    collapsed.sort(reverse=True)
    return tuple(collapsed)


def _iter_subset_tiles(values, target_sum):
    values = [int(value) for value in values if value]
    subset_count = 1 << len(values)
    for mask in range(1, subset_count):
        subset = []
        subset_sum = 0
        for index, value in enumerate(values):
            if mask & (1 << index):
                subset_sum += value
                if subset_sum > target_sum:
                    break
                subset.append(value)
        if subset_sum == target_sum:
            yield tuple(subset)


def _is_fake_hit(values, required_tiles):
    target_tiles = _normalize_tiles(required_tiles)
    target_sum = _tiles_sum(target_tiles)
    found_exact = False
    found_fake = False

    for subset in _iter_subset_tiles(values, target_sum):
        normalized_subset = _normalize_tiles(subset)
        if normalized_subset == target_tiles:
            found_exact = True
            continue
        if _collapse_tiles(subset) == target_tiles:
            found_fake = True

    return found_fake and not found_exact


def _score_values(values, rules):
    total_sum = _tiles_sum(values)
    actual_max = max((int(value) for value in values), default=0)
    full_board_peak = _first_full_board_peak(rules)
    candidate_rule = None
    for rule in rules:
        rule_peak = max(int(value) for value in rule.required_tiles)
        if rule_peak > full_board_peak and actual_max < rule_peak:
            continue
        if total_sum >= _tiles_sum(rule.required_tiles):
            candidate_rule = rule
            break

    if candidate_rule is None:
        return 0, "", 0

    effective_sum = total_sum
    candidate_sum = _tiles_sum(candidate_rule.required_tiles)
    if _is_fake_hit(values, candidate_rule.required_tiles):
        effective_sum = candidate_sum - 1

    for rule in rules:
        rule_peak = max(int(value) for value in rule.required_tiles)
        if rule_peak > full_board_peak and actual_max < rule_peak:
            continue
        if effective_sum >= _tiles_sum(rule.required_tiles):
            return rule.points, rule.label, rule.full_board_level

    return 0, "", 0


def score_board(board, variant):
    return _score_values(flatten_board(board), SCORING_RULES[variant])


def get_display_name(username):
    return username


def build_user_url(username, variant, page):
    safe_username = quote(username, safe="")
    return "{}?username={}&sort=date&variant={}&page={}&desc=true".format(
        API_BASE_URL, safe_username, variant, page
    )


def fetch_json(url):
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as error:
        print("[网络错误] {}".format(error))
    except json.JSONDecodeError as error:
        print("[JSON解析错误] {}".format(error))
    return None


def get_game_start_time(game):
    keys = ("started_at", "created_at", "start_time", "startedAt", "createdAt")
    for key in keys:
        value = game.get(key)
        if isinstance(value, str) and value.strip():
            try:
                return parse_iso_time(value)
            except ValueError:
                continue
    return None


def game_is_within_window(game, start_time, end_time):
    end_value = game.get("played_at")
    if not isinstance(end_value, str):
        return False, None, None

    try:
        end_dt = parse_iso_time(end_value)
    except ValueError:
        return False, None, None

    start_dt = get_game_start_time(game)
    if end_dt < start_time or end_dt > end_time:
        return False, start_dt, end_dt
    if start_dt is not None and start_dt < start_time:
        return False, start_dt, end_dt
    return True, start_dt, end_dt


def fetch_games_for_window(username, variant, start_time, end_time):
    page = 1
    collected = []
    seen_ids = set()

    while True:
        data = fetch_json(build_user_url(username, variant, page))
        if not data:
            break

        games = data.get("games", [])
        if not games:
            break

        reached_before_window = False
        for game in games:
            game_id = game.get("id")
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)

            include, _, end_dt = game_is_within_window(game, start_time, end_time)
            if include:
                collected.append(game)
            if end_dt is not None and end_dt < start_time:
                reached_before_window = True

        if reached_before_window:
            break
        page += 1

    collected.sort(key=lambda item: parse_iso_time(item["played_at"]))
    return collected


def score_games(username, display_name, variant, games, start_time, end_time):
    scored_games = []
    all_window_game_count = 0

    for game in games:
        include, start_dt, end_dt = game_is_within_window(game, start_time, end_time)
        if not include or end_dt is None:
            continue

        all_window_game_count += 1
        board = game.get("board") or []
        points, label, full_board_level = score_board(board, variant)
        if points <= 0:
            continue

        scored_games.append(
            ScoredGame(
                game_id=int(game.get("id", 0)),
                username=username,
                display_name=display_name,
                variant=variant,
                raw_score=int(game.get("score", 0)),
                points=points,
                label=label,
                end_time=end_dt,
                start_time=start_dt,
                offset=end_dt - start_time,
                board=board,
                full_board_level=full_board_level,
            )
        )

    scored_games.sort(key=lambda item: item.end_time, reverse=True)
    total_points = sum(game.points for game in scored_games)
    total_full_boards = sum(game.full_board_level for game in scored_games)
    return PlayerResult(
        username=username,
        display_name=display_name,
        variant=variant,
        total_points=total_points,
        scoring_game_count=len(scored_games),
        all_window_game_count=all_window_game_count,
        total_full_boards=total_full_boards,
        games=scored_games,
    )


def _read_csv_players(file_path):
    rows = []
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for row_index, row in enumerate(reader):
            if row_index == 0:
                continue
            username = row[0].strip() if len(row) >= 1 else ""
            rows.append({"username": username})
    return rows


def _xlsx_column_index(cell_ref):
    letters = re.match(r"[A-Z]+", cell_ref).group(0)
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - ord("A") + 1
    return value - 1


def _read_xlsx_players(file_path):
    with zipfile.ZipFile(file_path) as workbook:
        shared_strings = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            shared_root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            for node in shared_root.findall(".//{*}si"):
                shared_strings.append("".join(text.text or "" for text in node.findall(".//{*}t")))

        workbook_root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
        workbook_rels = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        first_sheet = workbook_root.find(".//{*}sheet")
        if first_sheet is None:
            return []

        relationship_id = first_sheet.attrib.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        target = None
        for relation in workbook_rels.findall(".//{*}Relationship"):
            if relation.attrib.get("Id") == relationship_id:
                target = relation.attrib.get("Target")
                break
        if not target:
            return []

        sheet_path = "xl/" + target.lstrip("/")
        sheet_root = ElementTree.fromstring(workbook.read(sheet_path))
        rows = []
        for row in sheet_root.findall(".//{*}row"):
            current = {}
            for cell in row.findall("{*}c"):
                cell_ref = cell.attrib.get("r", "A1")
                index = _xlsx_column_index(cell_ref)
                cell_type = cell.attrib.get("t")
                value_node = cell.find("{*}v")
                if value_node is None:
                    inline_node = cell.find(".//{*}t")
                    cell_text = inline_node.text if inline_node is not None else ""
                elif cell_type == "s":
                    cell_text = shared_strings[int(value_node.text)]
                else:
                    cell_text = value_node.text or ""
                current[index] = cell_text.strip()
            if current:
                size = max(current) + 1
                rows.append([current.get(i, "") for i in range(size)])

    if not rows:
        return []

    parsed_rows = []
    for row in rows[1:]:
        username = row[0].strip() if len(row) >= 1 else ""
        parsed_rows.append({"username": username})
    return parsed_rows


def load_players(file_path):
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError("名单文件不存在: {}".format(path))

    suffix = path.suffix.lower()
    if suffix == ".csv":
        rows = _read_csv_players(path)
    elif suffix == ".xlsx":
        rows = _read_xlsx_players(path)
    else:
        raise ValueError("名单文件只支持 .xlsx 或 .csv")

    seen = set()
    players = []
    for row in rows:
        username = (row.get("username") or "").strip()
        if not username or username in seen:
            continue
        seen.add(username)
        players.append({"username": username})

    if not players:
        raise ValueError("名单文件中没有可用的 verse 用户名")
    return players


def event_output_dir(base_dir, event_info):
    directory = base_dir / event_info.event_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def sanitize_filename(value):
    return re.sub(r'[\\/:*?"<>|]+', "_", value)


def sort_results(results):
    return sorted(
        results,
        key=lambda item: (-item.total_points, -item.total_full_boards, item.scoring_game_count, item.username.lower()),
    )


def summarize_results(results):
    total_points = sum(item.total_points for item in results)
    total_games = sum(item.scoring_game_count for item in results)
    total_full_boards = sum(item.total_full_boards for item in results)
    return total_points, total_games, total_full_boards


def get_top_full_board_players(results):
    if not results:
        return set(), 0
    max_full_boards = max(item.total_full_boards for item in results)
    if max_full_boards <= 0:
        return set(), 0
    top_players = set(item.username for item in results if item.total_full_boards == max_full_boards)
    return top_players, max_full_boards


def freeze_results_at(results, seal_time):
    frozen = []
    for result in results:
        frozen_games = [game for game in result.games if game.end_time <= seal_time]
        frozen.append(
            PlayerResult(
                username=result.username,
                display_name=result.display_name,
                variant=result.variant,
                total_points=sum(game.points for game in frozen_games),
                scoring_game_count=len(frozen_games),
                all_window_game_count=result.all_window_game_count,
                total_full_boards=sum(game.full_board_level for game in frozen_games),
                games=frozen_games,
            )
        )
    return sort_results(frozen)


def get_signature(payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256("{}|{}".format(RESULT_SECRET, canonical).encode("utf-8")).hexdigest()
    return digest


def build_ranking_payload(event_info, results):
    payload = {
        "event": {
            "event_id": event_info.event_id,
            "event_name": event_info.event_name,
            "source": event_info.source,
            "variant": event_info.variant,
            "format": event_info.format_name,
            "start_time": format_datetime(event_info.start_time),
            "end_time": format_datetime(event_info.end_time),
            "duration": format_clock(event_info.duration),
            "seal_time": format_datetime(event_info.seal_time) if event_info.seal_time else None,
        },
        "ranking": [],
    }

    for index, item in enumerate(results, start=1):
        payload["ranking"].append(
            {
                "rank": index,
                "username": item.username,
                "display_name": item.display_name,
                "primary_label": "总分",
                "primary_value": item.total_points,
                "secondary_label": "总满盘数",
                "secondary_value": item.total_full_boards,
                "extra": {
                    "计分局数": item.scoring_game_count,
                    "时间窗内总局数": item.all_window_game_count,
                },
            }
        )

    payload["signature"] = get_signature(payload)
    return payload


def export_player_text(result, event_info, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_path = output_dir / "{}.txt".format(sanitize_filename(result.username))
    lines = [
        "玩家: {}".format(result.display_name),
        "用户名: {}".format(result.username),
        "模式: {}".format(result.variant),
        "比赛时间: {} - {}".format(format_datetime(event_info.start_time), format_datetime(event_info.end_time)),
        "比赛总时长: {}".format(format_clock(event_info.duration)),
        "总比赛分: {}".format(result.total_points),
        "计分局数: {}".format(result.scoring_game_count),
        "总满盘数: {}".format(result.total_full_boards),
        "",
        "计分明细（按结束时间倒序）",
    ]
    if result.games:
        for game in result.games:
            lines.append(
                "{} | {} | {}分 | 满盘+{} | 原始分数 {}".format(
                    format_offset(game.offset),
                    game.label,
                    game.points,
                    game.full_board_level,
                    game.raw_score,
                )
            )
    else:
        lines.append("无计分局")
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    return txt_path


def export_ranking_files(results, event_info, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_ranking_payload(event_info, results)

    ranking_json = output_dir / "总排名.json"
    ranking_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    top_full_board_players, max_full_boards = get_top_full_board_players(results)
    lines = [
        "比赛名称: {}".format(event_info.event_name),
        "模式: {}".format(event_info.variant),
        "比赛时间: {} - {}".format(format_datetime(event_info.start_time), format_datetime(event_info.end_time)),
        "比赛总时长: {}".format(format_clock(event_info.duration)),
        "封榜时间: {}".format(format_datetime(event_info.seal_time) if event_info.seal_time else "不封榜"),
        "校验码: {}".format(payload["signature"]),
        "",
        "排名 | 玩家 | 总分 | 计分局数 | 总满盘数",
    ]
    for index, item in enumerate(results, start=1):
        marker = " ★最多满盘" if item.username in top_full_board_players and max_full_boards > 0 else ""
        lines.append(
            "{} | {}{} | {} | {} | {}".format(
                index,
                item.display_name,
                marker,
                item.total_points,
                item.scoring_game_count,
                item.total_full_boards,
            )
        )
    (output_dir / "总排名.txt").write_text("\n".join(lines), encoding="utf-8")
    export_ranking_image(results, event_info, output_dir / "总排名.png")


def _load_font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def export_ranking_image(results, event_info, image_path):
    width = 1180
    header_height = 160
    row_height = 48
    footer_height = 56
    body_rows = max(1, len(results))
    height = header_height + row_height * (body_rows + 1) + footer_height

    image = Image.new("RGB", (width, height), "#f6efe3")
    draw = ImageDraw.Draw(image)

    title_font = _load_font(34, bold=True)
    body_font = _load_font(22)
    strong_font = _load_font(22, bold=True)
    small_font = _load_font(16)

    draw.rounded_rectangle((24, 20, width - 24, height - 20), radius=24, fill="#fffaf1", outline="#d8c8ad", width=2)
    draw.text((48, 40), "2048 比赛总排名", fill="#2b2115", font=title_font)
    subtitle = "{} | {} - {}".format(
        event_info.variant,
        format_datetime(event_info.start_time),
        format_datetime(event_info.end_time),
    )
    draw.text((48, 88), subtitle, fill="#6b5d49", font=body_font)

    table_top = header_height
    draw.rounded_rectangle((40, table_top, width - 40, table_top + row_height), radius=12, fill="#ead9bf")
    columns = [
        ("排名", 60),
        ("玩家", 150),
        ("总分", 610),
        ("计分局数", 770),
        ("总满盘数", 930),
    ]
    for label, x in columns:
        draw.text((x, table_top + 12), label, fill="#3a2c1c", font=strong_font)

    top_full_board_players, max_full_boards = get_top_full_board_players(results)
    medal_colors = {1: "#c58a13", 2: "#8d96a0", 3: "#b36d3f"}

    for index, item in enumerate(results, start=1):
        top = table_top + row_height * index
        if index == 1:
            fill = "#f7e6a8"
        elif index == 2:
            fill = "#e3e8ef"
        elif index == 3:
            fill = "#edd0b8"
        else:
            fill = "#fffdf9" if index % 2 else "#f7efe3"
        draw.rectangle((40, top, width - 40, top + row_height), fill=fill)
        rank_color = medal_colors.get(index, "#3a2c1c")
        draw.text((60, top + 11), str(index), fill=rank_color, font=strong_font)
        name_text = item.display_name
        if item.username in top_full_board_players and max_full_boards > 0:
            name_text += " ★最多满盘"
        draw.text((150, top + 11), name_text, fill="#2f261b", font=body_font)
        draw.text((610, top + 11), str(item.total_points), fill="#2f261b", font=body_font)
        draw.text((770, top + 11), str(item.scoring_game_count), fill="#2f261b", font=body_font)
        draw.text((930, top + 11), str(item.total_full_boards), fill="#2f261b", font=body_font)

    footer_text = "由主办方程序自动生成 | {}".format(format_datetime(now_local()))
    draw.text((48, height - 48), footer_text, fill="#746754", font=small_font)
    image.save(image_path)


def build_full_board_notifications(results, seen_ids):
    notifications = []
    new_seen_ids = set(seen_ids)
    games = []
    for result in results:
        for game in result.games:
            if game.full_board_level > 0:
                games.append(game)
    games.sort(key=lambda item: item.end_time)
    for game in games:
        notification_id = "{}:{}".format(game.username, game.game_id)
        if notification_id in new_seen_ids:
            continue
        new_seen_ids.add(notification_id)
        notifications.append(
            {
                "id": notification_id,
                "message": "{} 达成 {}！".format(game.display_name, game.label),
                "level": game.full_board_level,
                "ended_at": format_datetime(game.end_time),
            }
        )
    return notifications, new_seen_ids
