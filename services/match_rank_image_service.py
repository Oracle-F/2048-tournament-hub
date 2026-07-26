"""Render the fixed two-image match ranking format.

The renderer deliberately consumes a normalized snapshot instead of querying
the bot or the Verse service.  This keeps a published image reproducible:
one snapshot produces one pair of content layers and one pair of composites.
"""

from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


LOCAL_TIMEZONE = timezone(timedelta(hours=8))
CANVAS_SIZE = (1920, 1080)
DEFAULT_REQUIRED_GAMES = 3
DEFAULT_TEAM_SIZE = 12
DEFAULT_TOP_SINGLE_LIMIT = 20
DEFAULT_TOP_BOARD_SUM_LIMIT = 15

FONT_CANDIDATES = {
    "regular": [
        "/usr/share/fonts/google-noto-sans-cjk-vf-fonts/NotoSansCJK-VF.ttc",
        "/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ],
    "bold": [
        "/usr/share/fonts/google-noto-sans-cjk-vf-fonts/NotoSansCJK-VF.ttc",
        "/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Bold.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ],
}


def _now_local() -> datetime:
    return datetime.now(LOCAL_TIMEZONE)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES["bold" if bold else "regular"]:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            kwargs = {}
            if path.name == "NotoSansCJK-VF.ttc":
                # The Fedora variable font defaults to Thin.  Select the
                # Simplified Chinese face and an explicit readable weight.
                kwargs["index"] = 2
            font = ImageFont.truetype(str(path), size=size, **kwargs)
            if hasattr(font, "set_variation_by_name"):
                font.set_variation_by_name("Bold" if bold else "Regular")
            return font
        except OSError:
            continue
    return ImageFont.load_default()


def _font_with_fallback(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return _font(size, bold=bold)


def _value(item: Any, *keys: str) -> Any:
    if isinstance(item, dict):
        for key in keys:
            if key in item:
                return item[key]
    return None


def _number(value: Any, default: Any = None) -> Any:
    if value in (None, "", "-"):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return int(number) if number.is_integer() else number


def format_value(value: Any) -> str:
    value = _number(value)
    if value is None:
        return "—"
    if isinstance(value, float):
        return "{:.1f}".format(value)
    return str(value)


def format_integer(value: Any) -> str:
    value = _number(value)
    if value is None:
        return "—"
    return str(int(value))


def _score_value(item: Any) -> int | float | None:
    if isinstance(item, dict):
        item = _value(item, "score", "single_score", "value", "final_score")
    return _number(item)


def _rating_value(item: Any) -> int | float | None:
    if isinstance(item, dict):
        item = _value(item, "rating", "value", "rating_value")
    return _number(item)


def _team_rating_from_players(players: list[dict[str, Any]]) -> float | None:
    """Average every listed player's in-event rating when all are available."""

    values = [player.get("rating") for player in players]
    if not players or any(value is None for value in values):
        return None
    return sum(values) / len(values)


def _team_completion_rate(team: dict[str, Any], required_games: int) -> float:
    expected_players = int(team.get("expected_players") or DEFAULT_TEAM_SIZE)
    expected_games = max(int(required_games), 1)
    played_games = sum(
        min(len(player.get("top_scores") or []), expected_games)
        for player in team.get("players") or []
    )
    denominator = expected_players * expected_games
    return played_games / denominator * 100 if denominator else 0.0


def _completion_rate_text(team: dict[str, Any], required_games: int) -> str:
    return "{:.2f}%".format(_team_completion_rate(team, required_games))


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    suffix = "…"
    output = text
    while output and draw.textlength(output + suffix, font=font) > max_width:
        output = output[:-1]
    return output + suffix if output else suffix


def _date_only(value: Any) -> str:
    """Format a timestamp for display while keeping full timestamps in data."""

    text = str(value or "—").strip()
    if text == "—":
        return text
    for separator in ("T", " "):
        if separator in text:
            text = text.split(separator, 1)[0]
            break
    return text.replace("/", "-").replace(".", "-")


def _datetime_to_minute(value: Any) -> str:
    text = str(value or "—").strip()
    if text == "—":
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        match = re.match(r"^(\d{4})[./-](\d{1,2})[./-](\d{1,2})[ T](\d{1,2}):(\d{2})", text)
        if not match:
            return text
        year, month, day, hour, minute = (int(part) for part in match.groups())
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}".format(year, month, day, hour, minute)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(LOCAL_TIMEZONE)
    return parsed.strftime("%Y-%m-%d %H:%M")


def _center(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font: ImageFont.ImageFont, fill: str, anchor: str = "mm") -> None:
    x1, y1, x2, y2 = box
    draw.text(((x1 + x2) / 2, (y1 + y2) / 2), text, font=font, fill=fill, anchor=anchor)


def _rank_color(rank: int | None) -> str:
    return {1: "#d19d00", 2: "#647a90", 3: "#b46c33"}.get(rank, "#111b25")


def _team_label(team: dict[str, Any]) -> str:
    """Return a readable label without duplicating names such as ``A队``."""

    code = str(team.get("code") or "").strip()
    name = str(team.get("name") or "队伍").strip()
    if name.casefold() in {code.casefold(), (code + "队").casefold()}:
        return name
    return "{}队·{}".format(code, name)


def _normalize_top_scores(value: Any) -> list[int | float]:
    if not isinstance(value, list):
        return []
    output = []
    for item in value:
        score = _score_value(item)
        if score is not None:
            output.append(score)
    return output


def _normalize_player(player: dict[str, Any], index: int) -> dict[str, Any]:
    item = copy.deepcopy(player)
    # These fields belong to the event-hub input layer.  The published
    # snapshot only needs the normalized Top-3 result, not the manual input
    # ledger or account lookup flags.
    item.pop("manual_records", None)
    item.pop("manual_only", None)
    item.pop("manual_note", None)
    item.pop("_all_scores", None)
    item["number"] = str(item.get("number") or "{:02d}".format(index)).zfill(2)
    item["display_name"] = str(item.get("display_name") or item.get("name") or "选手{:02d}".format(index))
    item["verse"] = str(item.get("verse") or item.get("username") or item.get("account_key") or "—")
    item["top_scores"] = sorted(_normalize_top_scores(item.get("top_scores")), reverse=True)[:3]
    games_seen = _number(item.get("games_seen"))
    item["games_seen"] = (
        max(0, int(games_seen))
        if games_seen is not None
        else len(item["top_scores"])
    )
    item["total_board_sum"] = _number(item.get("total_board_sum"))
    item["rating"] = _rating_value(item.get("rating"))
    item["completion"] = item.get("completion")
    return item


def _normalize_team_tiers(players: list[dict[str, Any]], team_code: str) -> list[dict[str, Any]]:
    """Preserve old ordered snapshots while rejecting ambiguous tier data."""

    raw_tiers = [player.get("tier") for player in players]
    if all(value in (None, "") for value in raw_tiers):
        for index, player in enumerate(players, 1):
            player["tier"] = index
    else:
        tiers = []
        for player in players:
            value = _number(player.get("tier"))
            if value is None or int(value) != value or not 1 <= int(value) <= 12:
                raise ValueError("{}队存在无效或缺失的档位: {}".format(team_code, player.get("tier")))
            tiers.append(int(value))
            player["tier"] = int(value)
        if set(tiers) != set(range(1, 13)):
            raise ValueError("{}队必须完整且不重复地包含第 1–12 档".format(team_code))
    players.sort(key=lambda player: player["tier"])
    for player in players:
        player["number"] = "{:02d}".format(player["tier"])
    return players


def _assign_tier_ranks(teams: list[dict[str, Any]]) -> None:
    """Rank each player against the other teams' player in the same tier."""

    players_by_tier: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for team in teams:
        for player in team.get("players") or []:
            player["tier_rank"] = None
            tier = _number(player.get("tier"))
            if tier is None or int(tier) != tier or not 1 <= int(tier) <= 12:
                continue
            player["tier"] = int(tier)
            players_by_tier[int(tier)].append(player)

    for players in players_by_tier.values():
        ranked = [player for player in players if player.get("total_board_sum") is not None]
        ranked.sort(
            key=lambda player: (
                -player["total_board_sum"],
                _player_verse(player).casefold(),
            )
        )
        previous_board_sum = None
        current_rank = 0
        for position, player in enumerate(ranked, 1):
            board_sum = player["total_board_sum"]
            if position == 1 or board_sum != previous_board_sum:
                current_rank = position
            player["tier_rank"] = current_rank
            previous_board_sum = board_sum


def _player_verse(player: dict[str, Any]) -> str:
    return str(player.get("verse") or player.get("username") or player.get("account_key") or "—")


def _player_completion(player: dict[str, Any], required_games: int) -> bool:
    completion = player.get("completion")
    if isinstance(completion, bool):
        return completion
    if isinstance(completion, dict):
        completed = _number(_value(completion, "completed", "done"), 0)
        return completed >= required_games
    return len(player.get("top_scores") or []) >= required_games


def _normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(snapshot)
    event = data.setdefault("event", {})
    event.setdefault("title", "群星杯 · 2026 4×4 团体赛")
    event.setdefault("subtitle", "2026.07.27 — 08.24 · 2048 4×4 团体赛")
    event.setdefault("as_of", _now_local().strftime("%Y-%m-%d %H:%M:%S"))
    event.setdefault("start_time", "2026-07-27 00:00:00")
    event.setdefault("end_time", "2026-08-24 23:59:59")

    required_games = int(data.get("required_games") or DEFAULT_REQUIRED_GAMES)
    data["required_games"] = required_games
    teams = data.get("teams") or []
    if len(teams) != 6:
        raise ValueError("当前固定版式要求恰好 6 个队伍，实际收到 {} 个".format(len(teams)))

    normalized_teams = []
    seen_players: set[str] = set()
    for team_index, raw_team in enumerate(teams):
        team = copy.deepcopy(raw_team)
        code = str(team.get("code") or chr(ord("A") + team_index)).strip().upper()
        team["code"] = code
        team["name"] = str(team.get("name") or "队伍")
        raw_players = team.get("players") or []
        if len(raw_players) != 12:
            raise ValueError("{}队必须正好包含 12 名队员，实际收到 {} 名".format(code, len(raw_players)))
        team["players"] = _normalize_team_tiers(
            [_normalize_player(player, index) for index, player in enumerate(raw_players, 1)],
            code,
        )
        for player in team["players"]:
            identity = str(player.get("username") or player.get("account_key") or player.get("display_name") or player.get("verse") or "").casefold()
            if identity in seen_players:
                raise ValueError("选手重复出现在多个队伍: {}".format(player["display_name"]))
            seen_players.add(identity)
        totals = [player["total_board_sum"] for player in team["players"] if player["total_board_sum"] is not None]
        team["total_board_sum"] = sum(totals) if totals else None
        if team.get("completion") is None:
            completed = sum(_player_completion(player, required_games) for player in team["players"])
            team["completion"] = {"completed": completed, "total": len(team["players"]) or 12}
        team["team_rating"] = _team_rating_from_players(team["players"])
        for player in team["players"]:
            player.pop("display_name", None)
            player.pop("rating", None)
        team.pop("team_rating", None)
        normalized_teams.append(team)

    codes = [team["code"] for team in normalized_teams]
    if set(codes) != set("ABCDEF"):
        raise ValueError("六队明细必须完整且不重复地包含 A–F 六队，实际为: {}".format("、".join(codes)))
    normalized_teams.sort(key=lambda team: team["code"])
    ranked_teams = sorted(
        [team for team in normalized_teams if team.get("total_board_sum") is not None],
        key=lambda team: (
            -team["total_board_sum"],
            team["code"],
        ),
    )
    previous_total = None
    current_rank = 0
    for position, team in enumerate(ranked_teams, 1):
        if position == 1 or team["total_board_sum"] != previous_total:
            current_rank = position
        team["rank"] = current_rank
        previous_total = team["total_board_sum"]
    for team in normalized_teams:
        if team.get("total_board_sum") is None:
            team["rank"] = None
    _assign_tier_ranks(normalized_teams)
    # The six detail cards and the left summary are fixed slots: A, B, C, D,
    # E, F.  Only the displayed rank changes with the calculated total; the
    # team itself must not move to another visual slot.
    data["teams"] = normalized_teams

    all_players = []
    for team in data["teams"]:
        for player in team["players"]:
            all_players.append({**player, "group": team["code"]})
    for player in data.get("individuals") or []:
        normalized = _normalize_player(player, len(all_players) + 1)
        normalized["group"] = "个人组"
        identity = str(normalized.get("username") or normalized.get("account_key") or normalized.get("display_name") or normalized.get("verse") or "").casefold()
        if identity in seen_players:
            raise ValueError("选手重复出现在队伍或个人组: {}".format(normalized["display_name"]))
        seen_players.add(identity)
        normalized.pop("display_name", None)
        normalized.pop("rating", None)
        all_players.append(normalized)
    data["individuals"] = [player for player in all_players if player["group"] == "个人组"]

    if not data.get("single_game_top"):
        records = []
        for player in all_players:
            for score in player.get("top_scores") or []:
                records.append({"verse": _player_verse(player), "group": player["group"], "score": score})
        data["single_game_top"] = sorted(records, key=lambda row: row["score"], reverse=True)
    if not data.get("board_sum_top"):
        data["board_sum_top"] = [
            {"verse": _player_verse(player), "group": player["group"], "board_sum": player["total_board_sum"]}
            for player in all_players
            if player.get("total_board_sum") is not None
        ]
    data["single_game_top"] = _normalize_single_top(data.get("single_game_top"), DEFAULT_TOP_SINGLE_LIMIT)
    data["board_sum_top"] = _normalize_board_sum_top(data.get("board_sum_top"), DEFAULT_TOP_BOARD_SUM_LIMIT)
    data.pop("rating_top", None)
    return data


def _normalize_single_top(rows: Any, limit: int) -> list[dict[str, Any]]:
    output = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        score = _score_value(row)
        if score is None:
            continue
        output.append({"verse": str(row.get("verse") or row.get("username") or row.get("account_key") or "—"), "group": str(row.get("group") or "—"), "score": score})
    output.sort(key=lambda row: row["score"], reverse=True)
    return [{**row, "rank": index} for index, row in enumerate(output[:limit], 1)]


def _normalize_board_sum_top(rows: Any, limit: int) -> list[dict[str, Any]]:
    output = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        board_sum = _number(row.get("board_sum", row.get("total_board_sum")))
        if board_sum is None:
            continue
        output.append({"verse": str(row.get("verse") or row.get("username") or row.get("account_key") or "—"), "group": str(row.get("group") or "—"), "board_sum": board_sum})
    output.sort(key=lambda row: row["board_sum"], reverse=True)
    return [{**row, "rank": index} for index, row in enumerate(output[:limit], 1)]


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate the stable renderer input contract."""

    if not isinstance(snapshot, dict):
        raise ValueError("榜图快照必须是 JSON object")
    return _normalize_snapshot(snapshot)


def _draw_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill: str = "#ffffff", fill_alpha: int = 188, radius: int = 10, outline: str = "#203040", width: int = 1) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=radius, fill=(*_hex_rgb(fill), fill_alpha), outline=outline, width=width)


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _line(draw: ImageDraw.ImageDraw, coords: tuple[int, int, int, int], fill: str = "#203040", width: int = 1) -> None:
    draw.line(coords, fill=fill, width=width)


def _teams_in_rank_order(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        snapshot["teams"],
        key=lambda team: (
            team.get("rank") is None,
            team.get("rank") if team.get("rank") is not None else float("inf"),
            team.get("code") or "",
        ),
    )


def _draw_total_content(snapshot: dict[str, Any]) -> Image.Image:
    layer = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    regular_17 = _font_with_fallback(17)
    bold_18 = _font_with_fallback(18, bold=True)
    bold_22 = _font_with_fallback(22, bold=True)
    bold_30 = _font_with_fallback(30, bold=True)

    _draw_panel(draw, (10, 8, 1910, 58), fill="#ffffff", fill_alpha=45, radius=15, outline="#172536")
    _center(draw, (10, 8, 1910, 58), snapshot["event"]["title"], bold_30, "#111b25")

    # Left team summary. Six equal body rows keep every horizontal rule visible.
    left = (10, 76, 570, 1063)
    _draw_panel(draw, left, fill="#ffffff", fill_alpha=165, radius=10, outline="#203040")
    _line(draw, (10, 137, 570, 137))
    for x in (73, 201, 450):
        _line(draw, (x, 137, x, 1063))
    _center(draw, (10, 76, 570, 137), "队伍总榜", _font_with_fallback(23, bold=True), "#111b25")
    headers = [(41, "排名"), (137, "队伍"), (325, "总盘面和"), (510, "完成率")]
    for x, text in headers:
        _center(draw, (x - 50, 137, x + 50, 180), text, bold_18, "#111b25")
    row_top = 180
    row_height = (1063 - row_top) / 6
    # Only the left team leaderboard follows the calculated placement.
    # The six detail cards continue to consume snapshot["teams"] in fixed A-F
    # order so their visual positions never move.
    for index, team in enumerate(_teams_in_rank_order(snapshot), 1):
        top = int(round(row_top + (index - 1) * row_height))
        bottom = int(round(row_top + index * row_height))
        _line(draw, (10, bottom, 570, bottom))
        rank = team["rank"]
        _center(draw, (10, top, 73, bottom), str(rank) if rank is not None else "—", bold_22, _rank_color(rank))
        _center(draw, (73, top, 201, bottom), _team_label(team), bold_22, "#111b25")
        _center(draw, (201, top, 450, bottom), format_integer(team.get("total_board_sum")), _font_with_fallback(26, bold=True), "#111b25")
        _center(draw, (450, top, 570, bottom), _completion_rate_text(team, snapshot["required_games"]), _font_with_fallback(22, bold=True), "#111b25")

    # Middle single-game top 20.
    middle = (580, 76, 1235, 1063)
    _draw_panel(draw, middle, fill="#ffffff", fill_alpha=165, radius=10, outline="#203040")
    _line(draw, (580, 137, 1235, 137))
    _center(draw, (580, 76, 1235, 137), "赛时单局榜 Top20", _font_with_fallback(23, bold=True), "#111b25")
    for x in (643, 714, 967):
        _line(draw, (x, 137, x, 1063))
    for x, text in ((611, "排名"), (678, "组别"), (840, "Verse"), (1101, "单局分数")):
        _center(draw, (x - 40, 137, x + 40, 169), text, bold_18, "#111b25")
    single_top = snapshot["single_game_top"]
    single_row_top = 169
    single_height = (1063 - single_row_top) / 20
    for index in range(20):
        top = int(round(single_row_top + index * single_height))
        bottom = int(round(single_row_top + (index + 1) * single_height))
        _line(draw, (580, bottom, 1235, bottom))
        row = single_top[index] if index < len(single_top) else None
        if row:
            rank = index + 1
            _center(draw, (580, top, 643, bottom), str(rank), regular_17, _rank_color(rank))
            _center(draw, (643, top, 714, bottom), row["group"], bold_18, "#111b25")
            _center(draw, (714, top, 967, bottom), _fit_text(draw, row["verse"], regular_17, 235), regular_17, "#111b25")
            _center(draw, (967, top, 1235, bottom), format_integer(row["score"]), regular_17, "#111b25")

    # Right in-event board-sum top 15.
    right = (1245, 76, 1910, 1063)
    _draw_panel(draw, right, fill="#ffffff", fill_alpha=165, radius=10, outline="#203040")
    _line(draw, (1245, 137, 1910, 137))
    _center(draw, (1245, 76, 1910, 137), "赛时盘面和榜 Top15", _font_with_fallback(23, bold=True), "#111b25")
    for x in (1308, 1382, 1638):
        _line(draw, (x, 137, x, 1063))
    for x, text in ((1276, "排名"), (1345, "组别"), (1510, "Verse"), (1774, "赛时总盘面和")):
        _center(draw, (x - 45, 137, x + 45, 169), text, bold_18, "#111b25")
    board_sum_top = snapshot["board_sum_top"]
    board_sum_row_top = 169
    board_sum_height = (1063 - board_sum_row_top) / 15
    for index in range(15):
        top = int(round(board_sum_row_top + index * board_sum_height))
        bottom = int(round(board_sum_row_top + (index + 1) * board_sum_height))
        _line(draw, (1245, bottom, 1910, bottom))
        row = board_sum_top[index] if index < len(board_sum_top) else None
        if row:
            rank = index + 1
            _center(draw, (1245, top, 1308, bottom), str(rank), regular_17, _rank_color(rank))
            _center(draw, (1308, top, 1382, bottom), row["group"], bold_18, "#111b25")
            _center(draw, (1382, top, 1638, bottom), _fit_text(draw, row["verse"], regular_17, 240), regular_17, "#111b25")
            _center(draw, (1638, top, 1910, bottom), format_integer(row["board_sum"]), regular_17, "#111b25")
    return layer


def _draw_detail_card(draw: ImageDraw.ImageDraw, team: dict[str, Any], x: int, y: int, required_games: int) -> None:
    card = (x, y, x + 620, y + 480)
    _draw_panel(draw, card, fill="#ffffff", fill_alpha=165, radius=14, outline="#172536", width=1)
    _line(draw, (x, y + 52, x + 620, y + 52), "#172536", 1)
    _line(draw, (x, y + 86, x + 620, y + 86), "#172536", 1)
    columns = [64, 204, 254, 336, 418, 500]
    for offset in columns:
        _line(draw, (x + offset, y + 52, x + offset, y + 470), "#405266", 1)
    for row in range(13):
        _line(draw, (x, y + 86 + row * 32, x + 620, y + 86 + row * 32), "#405266", 1)

    title_font = _font_with_fallback(22, bold=True)
    rank_font = _font_with_fallback(19, bold=True)
    meta_font = _font_with_fallback(18, bold=True)
    header_font = _font_with_fallback(16, bold=True)
    body_font = _font_with_fallback(16)
    draw.text((x + 14, y + 14), _team_label(team), font=title_font, fill="#111b25")
    rank_text = "第{}名".format(team["rank"]) if team.get("rank") is not None else "未排名"
    draw.text((x + 205, y + 16), rank_text, font=rank_font, fill=_rank_color(team.get("rank")))
    draw.text(
        (x + 285, y + 18),
        _fit_text(draw, "队总盘面和 {}".format(format_integer(team.get("total_board_sum"))), meta_font, 180),
        font=meta_font,
        fill="#111b25",
    )
    draw.text(
        (x + 485, y + 18),
        _fit_text(draw, "完成率 {}".format(_completion_rate_text(team, required_games)), meta_font, 125),
        font=meta_font,
        fill="#111b25",
    )

    header_y = y + 69
    header_labels = ["队员号", "Verse", "档排", "Top1", "Top2", "Top3", "总盘面和"]
    header_centers = [32, 134, 229, 295, 377, 459, 560]
    for center, label in zip(header_centers, header_labels):
        _center(draw, (x + center - 42, header_y - 12, x + center + 42, header_y + 15), label, header_font, "#111b25")
    for index in range(12):
        player = team["players"][index] if index < len(team["players"]) else None
        top = y + 86 + index * 32
        bottom = top + 32
        scores = list(player.get("top_scores") or []) + [None, None, None] if player else [None, None, None]
        values = [
            player["number"] if player else "—",
            _player_verse(player) if player else "—",
            player.get("tier_rank") if player and player.get("tier_rank") is not None else "—",
            format_integer(scores[0]),
            format_integer(scores[1]),
            format_integer(scores[2]),
            format_integer(player.get("total_board_sum")) if player else "—",
        ]
        widths = [
            (x, x + 64),
            (x + 64, x + 204),
            (x + 204, x + 254),
            (x + 254, x + 336),
            (x + 336, x + 418),
            (x + 418, x + 500),
            (x + 500, x + 620),
        ]
        for column_index, ((left, right), value) in enumerate(zip(widths, values)):
            is_tier_rank = column_index == 2 and isinstance(value, int)
            font = header_font if is_tier_rank else body_font
            _center(draw, (left, top, right, bottom), _fit_text(draw, str(value), font, right - left - 6), font, "#111b25")


def _valid_game_count(snapshot: dict[str, Any]) -> int:
    players = [
        player
        for team in snapshot.get("teams") or []
        for player in team.get("players") or []
    ]
    players.extend(snapshot.get("individuals") or [])
    return sum(
        max(0, int(_number(player.get("games_seen"), len(player.get("top_scores") or [])) or 0))
        for player in players
    )


def _draw_detail_content(snapshot: dict[str, Any]) -> Image.Image:
    layer = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    bold_20 = _font_with_fallback(20, bold=True)
    participant_count = sum(len(team["players"]) for team in snapshot["teams"])
    completed_count = sum(
        1
        for team in snapshot["teams"]
        for player in team["players"]
        if _player_completion(player, snapshot["required_games"])
    )
    valid_game_count = _valid_game_count(snapshot)
    meta_cards = [
        (10, "比赛时间", "{} — {}".format(_date_only(snapshot["event"].get("start_time")), _date_only(snapshot["event"].get("end_time")))),
        (390, "数据截至", _datetime_to_minute(snapshot["event"].get("as_of", "—"))),
        (770, "总参赛人数", str(participant_count)),
        (1150, "完赛人数", str(completed_count)),
        (1530, "有效对局数", str(valid_game_count)),
    ]
    for x, label, value in meta_cards:
        _draw_panel(draw, (x, 22, x + 370, 78), fill="#ffffff", fill_alpha=175, radius=8, outline="#9aaabb")
        _center(draw, (x, 25, x + 370, 51), label, bold_20, "#233548")
        _center(draw, (x, 51, x + 370, 76), _fit_text(draw, value, bold_20, 350), bold_20, "#233548")
    positions = [(10, 82), (650, 82), (1290, 82), (10, 588), (650, 588), (1290, 588)]
    for team, (x, y) in zip(snapshot["teams"], positions):
        _draw_detail_card(draw, team, x, y, snapshot["required_games"])
    return layer


def _extract_background_title_overlay(background: Image.Image) -> Image.Image:
    """Keep the blue 群星杯 lettering visible above the translucent panels."""

    crop_box = (580, 450, 1320, 740)
    crop = background.crop(crop_box).convert("RGBA")
    source = crop.load()
    pixels = []
    for y in range(crop.height):
        for x in range(crop.width):
            red, green, blue, _alpha = source[x, y]
            blue_strength = blue - red
            if blue > 135 and blue_strength > 18 and blue >= green - 5:
                alpha = min(42, max(0, int(blue_strength * 0.34)))
                pixels.append((red, green, blue, alpha))
            else:
                pixels.append((0, 0, 0, 0))
    crop.putdata(pixels)
    overlay = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    overlay.alpha_composite(crop, dest=(crop_box[0], crop_box[1]))
    return overlay


def render_match_rank_images(snapshot: dict[str, Any], output_dir: Path, background_path: Path) -> dict[str, str]:
    data = validate_snapshot(snapshot)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(background_path) as source_background:
        background = source_background.convert("RGBA")
    if background.size != CANVAS_SIZE:
        background = background.resize(CANVAS_SIZE, Image.Resampling.LANCZOS)

    total_content = _draw_total_content(data)
    detail_content = _draw_detail_content(data)
    foreground_art = _extract_background_title_overlay(background)
    total_image = Image.alpha_composite(Image.alpha_composite(background, total_content), foreground_art).convert("RGB")
    detail_image = Image.alpha_composite(Image.alpha_composite(background, detail_content), foreground_art).convert("RGB")

    total_path = output_dir / "总榜.png"
    detail_path = output_dir / "六队明细.png"
    snapshot_path = output_dir / "榜图数据.json"
    temporary_paths = {
        total_path: total_path.with_name(total_path.name + ".tmp"),
        detail_path: detail_path.with_name(detail_path.name + ".tmp"),
        snapshot_path: snapshot_path.with_name(snapshot_path.name + ".tmp"),
    }
    try:
        total_image.save(temporary_paths[total_path], format="PNG")
        detail_image.save(temporary_paths[detail_path], format="PNG")
        temporary_paths[snapshot_path].write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for final_path, temporary_path in temporary_paths.items():
            temporary_path.replace(final_path)
    finally:
        for temporary_path in temporary_paths.values():
            if temporary_path.exists():
                temporary_path.unlink()
    return {
        "total": str(total_path),
        "detail": str(detail_path),
        "snapshot": str(snapshot_path),
    }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def _payload_board_sum(payload: Any) -> int | None:
    if isinstance(payload, list):
        values = []
        stack = list(payload)
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            value = _number(item)
            if value is None:
                return None
            values.append(value)
        return int(sum(values)) if values else None
    if not isinstance(payload, dict):
        return None
    for key in ("board_sum", "terminal_board_sum", "boardSum"):
        value = _number(payload.get(key))
        if value is not None:
            return int(value)
    for key in ("board_values", "boardValues"):
        values = payload.get(key)
        if isinstance(values, list) and values:
            numbers = [_number(value) for value in values]
            if all(value is not None for value in numbers):
                return int(sum(numbers))
    for key in ("board", "final_board", "terminal_board"):
        nested = payload.get(key)
        value = _payload_board_sum(nested)
        if value is not None:
            return value
    return None


def calculate_competition_rating(top_records: list[dict[str, Any]], required_games: int = DEFAULT_REQUIRED_GAMES) -> float | None:
    """Calculate in-event rating from score-selected games.

    The records passed here must already be ordered by score descending.  The
    selection metric is the score, while the rating metric is the average of
    the selected games' board sums.
    """

    selected = top_records[:required_games]
    if len(selected) < required_games:
        return None
    board_sums = [record.get("board_sum") for record in selected]
    if any(value is None or value <= 0 for value in board_sums):
        return None
    average_board_sum = sum(board_sums) / required_games
    return math.log2(average_board_sum) * 562.5 - 6000


def _db_records_by_account(connection: sqlite3.Connection, event_id: int | None = None) -> dict[str, list[dict[str, Any]]]:
    where_clauses = ["pr.is_valid_source = 1"]
    parameters: list[Any] = []
    if event_id is not None:
        where_clauses.insert(0, "ear.event_id = ?")
        parameters.append(event_id)
    rows = connection.execute(
        """
        SELECT ear.player_id, pr.started_at, pr.ended_at, pr.raw_score, pr.final_score,
               ear.derived_metric_value, ear.derived_metric_json, pr.raw_payload_json,
               p.display_name, pa.account_key, pa.account_name
        FROM event_attempt_records ear
        JOIN performance_records pr ON pr.id = ear.performance_record_id
        JOIN players p ON p.id = ear.player_id
        LEFT JOIN player_accounts pa ON pa.player_id = p.id AND pa.is_primary = 1
        WHERE {where}
        ORDER BY COALESCE(pr.ended_at, '') DESC, pr.id DESC
        """.format(where=" AND ".join(where_clauses)),
        parameters,
    ).fetchall()
    records = defaultdict(list)
    for row in rows:
        raw_payload = json.loads(row["raw_payload_json"] or "{}")
        derived_payload = json.loads(row["derived_metric_json"] or "{}")
        score = _number(row["final_score"] if row["final_score"] is not None else row["raw_score"])
        board_sum = _payload_board_sum(raw_payload) or _payload_board_sum(derived_payload)
        records[str(row["account_key"] or row["account_name"] or row["display_name"]).casefold()].append(
            {
                "player_id": row["player_id"],
                "display_name": row["display_name"],
                "score": score,
                "board_sum": board_sum,
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
            }
        )
    return records


def _roster_account_aliases(roster: dict[str, Any]) -> set[str]:
    aliases = set()
    players = []
    for team in roster.get("teams") or []:
        players.extend(team.get("players") or [])
    players.extend(roster.get("individuals") or [])
    for player in players:
        if player.get("manual_only"):
            continue
        for key in ("username", "account_key", "account_name", "display_name"):
            value = player.get(key)
            if value:
                aliases.add(str(value).casefold())
    return aliases


def _top_records_by_roster_account(
    connection: sqlite3.Connection,
    roster_aliases: set[str],
    start: datetime,
    end: datetime,
    _required_games: int,
) -> dict[str, list[dict[str, Any]]]:
    """Stream every valid event-window record for roster accounts.

    Keeping all records is required for the effective-game count and the
    global single-game Top20.  Top-3 selection still happens later per player.
    """

    rows = connection.execute(
        """
        SELECT ear.player_id, pr.id AS record_id, pr.started_at, pr.ended_at,
               pr.raw_score, pr.final_score, ear.derived_metric_json,
               pr.raw_payload_json, p.display_name, pa.account_key, pa.account_name
        FROM event_attempt_records ear
        JOIN performance_records pr ON pr.id = ear.performance_record_id
        JOIN players p ON p.id = ear.player_id
        LEFT JOIN player_accounts pa ON pa.player_id = p.id AND pa.is_primary = 1
        WHERE pr.is_valid_source = 1
        """
    )
    top_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        row_aliases = [
            str(row[key]).casefold()
            for key in ("account_key", "account_name", "display_name")
            if row[key]
        ]
        lookup_key = next((alias for alias in row_aliases if alias in roster_aliases), None)
        if lookup_key is None:
            continue
        started = _parse_time(row["started_at"])
        ended = _parse_time(row["ended_at"])
        if (
            started is None
            or ended is None
            or started > ended
            or started < start
            or ended < start
            or started > end
            or ended > end
        ):
            continue
        score = _number(row["final_score"] if row["final_score"] is not None else row["raw_score"])
        if score is None:
            continue
        raw_payload = json.loads(row["raw_payload_json"] or "{}")
        derived_payload = json.loads(row["derived_metric_json"] or "{}")
        board_sum = _payload_board_sum(raw_payload) or _payload_board_sum(derived_payload)
        top_records[lookup_key].append(
            {
                "player_id": row["player_id"],
                "display_name": row["display_name"],
                "score": score,
                "board_sum": board_sum,
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "record_id": row["record_id"],
            }
        )
    return dict(top_records)


def _build_roster_player(
    player: dict[str, Any],
    records: dict[str, list[dict[str, Any]]],
    start: datetime | None,
    end: datetime | None,
    required_games: int,
    manual_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    username = str(player.get("username") or player.get("account_key") or player.get("display_name") or "")
    candidates = []
    for record in records.get(username.casefold(), []):
        started = _parse_time(record.get("started_at"))
        ended = _parse_time(record.get("ended_at"))
        if started is None or ended is None:
            continue
        if started > ended:
            continue
        if start and (started < start or ended < start):
            continue
        if end and (started > end or ended > end):
            continue
        score = _number(record.get("score"))
        if score is None or score < 0:
            continue
        candidates.append({**record, "score": score})
    # Manual records are already event-verified input.  Timestamps are useful
    # when available, but are optional because a manual supplement may only
    # have the score and final board values.  If timestamps are present, keep
    # the same competition-window checks as queried records.
    for record in manual_records or []:
        if not isinstance(record, dict):
            continue
        started = _parse_time(record.get("started_at"))
        ended = _parse_time(record.get("ended_at"))
        if started is not None and ended is not None and started > ended:
            continue
        if started is not None and start and started < start:
            continue
        if started is not None and end and started > end:
            continue
        if ended is not None and start and ended < start:
            continue
        if ended is not None and end and ended > end:
            continue
        score = _number(record.get("score"))
        board_sum = _number(record.get("board_sum"))
        if score is None or score < 0 or board_sum is None or board_sum <= 0:
            continue
        candidates.append(
            {
                **record,
                "score": score,
                "board_sum": board_sum,
                "source": "manual",
                "started_at": record.get("started_at"),
                "ended_at": record.get("ended_at"),
            }
        )
    candidates.sort(key=lambda item: (item["score"], item.get("ended_at") or ""), reverse=True)
    top = candidates[:required_games]
    board_values = [item["board_sum"] for item in top if item.get("board_sum") is not None]
    board_sum_complete = len(board_values) == len(top)
    competition_rating = calculate_competition_rating(top, required_games)
    return {
        **player,
        "player_id": top[0].get("player_id") if top else player.get("player_id"),
        "display_name": str(player.get("display_name") or username or "—"),
        "username": username,
        "top_scores": [item["score"] for item in top],
        "_all_scores": [item["score"] for item in candidates],
        "total_board_sum": sum(board_values) if top and board_sum_complete else None,
        "completion": len(top) >= required_games,
        "games_seen": len(candidates),
        "board_sum_complete": board_sum_complete,
        "rating": competition_rating,
    }


def _build_snapshot_from_records(
    records: dict[str, list[dict[str, Any]]],
    roster: dict[str, Any],
    start: datetime,
    end: datetime,
    event_info: dict[str, Any],
) -> dict[str, Any]:
    required_games = int(roster.get("required_games") or DEFAULT_REQUIRED_GAMES)
    teams = []
    for raw_team in roster.get("teams") or []:
        players = [
            _build_roster_player(
                player,
                records,
                start,
                end,
                required_games,
                manual_records=player.get("manual_records"),
            )
            for player in raw_team.get("players") or []
        ]
        totals = [player["total_board_sum"] for player in players if player["total_board_sum"] is not None]
        teams.append(
            {
                **raw_team,
                "players": players,
                "team_rating": _team_rating_from_players(players),
                "total_board_sum": sum(totals) if totals else None,
            }
        )
    individuals = [
        _build_roster_player(
            player,
            records,
            start,
            end,
            required_games,
            manual_records=player.get("manual_records"),
        )
        for player in roster.get("individuals") or []
    ]
    incomplete_board_players = [
        _player_verse(player)
        for player in [
            *(player for team in teams for player in team["players"]),
            *individuals,
        ]
        if player.get("top_scores") and not player.get("board_sum_complete")
    ]
    if incomplete_board_players:
        raise ValueError(
            "以下玩家的高分入选局缺少盘面数据，已停止生成榜图: {}".format(
                "、".join(incomplete_board_players[:8])
            )
        )
    all_players = []
    for team in teams:
        for player in team["players"]:
            all_players.append({**player, "group": team.get("code") or "—"})
    all_players.extend({**player, "group": "个人组"} for player in individuals)
    single_top = []
    for player in all_players:
        for score in player.get("_all_scores") or player.get("top_scores") or []:
            single_top.append({"verse": _player_verse(player), "group": player["group"], "score": score})
    board_sum_top = [
        {"verse": _player_verse(player), "group": player["group"], "board_sum": player.get("total_board_sum")}
        for player in all_players
        if player.get("total_board_sum") is not None
    ]
    return _normalize_snapshot({
        "required_games": required_games,
        "event": {
            "title": roster.get("title") or event_info.get("event_name") or "年度4×4赛事",
            "subtitle": roster.get("subtitle") or "2026.07.27 — 08.24 · 2048 4×4 团体赛",
            "start_time": event_info.get("start_time") or "—",
            "end_time": event_info.get("end_time") or "—",
            "as_of": roster.get("as_of") or event_info.get("as_of") or _now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "event_code": event_info.get("event_code"),
            "variant": event_info.get("variant_code") or "4x4",
        },
        "teams": teams,
        "individuals": individuals,
        "single_game_top": single_top,
        "board_sum_top": board_sum_top,
    })


def build_snapshot_from_competition_roster(connection: sqlite3.Connection, roster: dict[str, Any]) -> dict[str, Any]:
    """Build this annual competition snapshot from one imported roster.

    This dedicated path does not look up an event code.  It reads valid source
    records, then applies the fixed competition time window and only builds
    rows for accounts present in the imported roster.
    """

    competition = roster.get("competition") if isinstance(roster.get("competition"), dict) else roster
    start_raw = competition.get("start_time") or roster.get("start_time")
    end_raw = competition.get("end_time") or roster.get("end_time")
    start = _parse_time(start_raw)
    end = _parse_time(end_raw)
    if start is None or end is None:
        raise ValueError("本次赛事名单必须包含有效的 start_time 和 end_time")
    event_code = competition.get("code") or roster.get("competition_code") or "annual_4x4_2026"
    required_games = int(roster.get("required_games") or DEFAULT_REQUIRED_GAMES)
    records = _top_records_by_roster_account(connection, _roster_account_aliases(roster), start, end, required_games)
    return build_snapshot_from_roster_records(roster, records)


def build_snapshot_from_roster_records(roster: dict[str, Any], records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Build the same snapshot from live Verse records or another source.

    ``records`` uses the normalized internal shape ``score``, ``board_sum``,
    ``started_at`` and ``ended_at``.  Keeping this builder source-agnostic
    lets the annual event hub reuse the already-tested live Verse reader from
    the original scoring program while retaining the database adapter for
    offline/replay use.
    """

    competition = roster.get("competition") if isinstance(roster.get("competition"), dict) else roster
    start_raw = competition.get("start_time") or roster.get("start_time")
    end_raw = competition.get("end_time") or roster.get("end_time")
    start = _parse_time(start_raw)
    end = _parse_time(end_raw)
    if start is None or end is None:
        raise ValueError("本次赛事名单必须包含有效的 start_time 和 end_time")
    event_code = competition.get("code") or roster.get("competition_code") or "annual_4x4_2026"
    return _build_snapshot_from_records(
        records,
        roster,
        start,
        end,
        {
            "event_code": event_code,
            "event_name": competition.get("title") or roster.get("title"),
            "start_time": start_raw,
            "end_time": end_raw,
            "as_of": competition.get("as_of") or roster.get("as_of"),
            "variant_code": "4x4",
        },
    )


def build_snapshot_from_db(connection: sqlite3.Connection, event_code: str, roster: dict[str, Any]) -> dict[str, Any]:
    """Build the renderer snapshot from the hub DB plus a stable team roster.

    The roster is intentionally external to the renderer because team
    membership is a competition decision, not a ranking formula.  It may be
    stored once as JSON and reused for every as-of snapshot.
    """

    event = connection.execute(
        """
        SELECT e.*, v.code AS variant_code
        FROM events e LEFT JOIN variants v ON v.id = e.variant_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if event is None:
        raise ValueError("赛事不存在: {}".format(event_code))
    records = _db_records_by_account(connection, event["id"])
    start = _parse_time(event["start_time"])
    end = _parse_time(event["end_time"])
    if start is None or end is None:
        raise ValueError("赛事缺少有效的 start_time 或 end_time")
    return _build_snapshot_from_records(
        records,
        roster,
        start,
        end,
        {
            "event_code": event_code,
            "event_name": event["event_name"],
            "start_time": event["start_time"],
            "end_time": event["end_time"],
            "variant_code": event["variant_code"],
        },
    )


def build_sample_snapshot() -> dict[str, Any]:
    team_specs = [
        ("A", "星河", 1520909, 1946),
        ("B", "长风", 1564804, 1948),
        ("C", "逐光", 1542897, 2043),
        ("D", "破阵", 1511094, 2076),
        ("E", "远航", 1487863, 1933),
        ("F", "凌云", 1573514, 2051),
    ]
    name_groups = [
        ["北辰", "云岚", "青桐", "子遥", "望舒", "临川", "长歌", "知微", "青禾", "凌峰", "望岳", "沧澜"],
        ["秋原", "南桥", "暮山", "白榆", "夜航", "云起", "行舟", "朝颜", "子衿", "松风", "苏芳", "清野"],
        ["南乔", "若川", "明澈", "观澜", "昕雨", "景行", "怀瑾", "昭华", "长安", "怀璧", "江月", "星垂"],
        ["初晴", "晴川", "星阑", "语棠", "安然", "泽言", "云深", "知夏", "砚舟", "清欢", "南风", "归途"],
        ["朝云", "念安", "景澄", "言川", "时安", "修远", "朗月", "知临", "晚舟", "云帆", "微澜", "临风"],
        ["望远", "静川", "星白", "清和", "延川", "川越", "清秋", "安澜", "南渡", "晓山", "默然", "子安"],
    ]
    teams = []
    for team_index, (code, name, total, rating) in enumerate(team_specs):
        players = []
        completed_players = 12 - (team_index % 3)
        base = total // completed_players
        remainder = total - base * completed_players
        for player_index, display_name in enumerate(name_groups[team_index], 1):
            board_sum = base + (1 if player_index <= remainder else 0)
            top = 840000 - team_index * 15000 - player_index * 7000
            games_available = 3 if player_index <= completed_players else 0
            players.append(
                {
                    "number": "{:02d}".format(player_index),
                    "tier": player_index,
                    "display_name": display_name,
                    "verse": "v_{:02d}".format(team_index * 12 + player_index),
                    "top_scores": [top, top - 180000, top - 320000][:games_available],
                    "games_seen": games_available + ((team_index + player_index) % 5) if games_available else 0,
                    "total_board_sum": board_sum if games_available else None,
                    "rating": rating - (player_index % 4) * 7,
                }
            )
        teams.append(
            {
                "code": code,
                "name": name,
                "rank": team_index + 1,
                "total_board_sum": total,
                "team_rating": rating,
                "completion": {"completed": completed_players, "total": 12},
                "players": players,
            }
        )
    return {
        "required_games": 3,
        "event": {
            "title": "群星杯 · 2026 4×4 团体赛",
            "subtitle": "2026.07.27 — 08.24 · 2048 4×4 团体赛",
            "start_time": "2026.07.27",
            "end_time": "2026.08.24",
            "as_of": "2026.07.27 12:00",
        },
        "teams": teams,
        "individuals": [],
    }
