from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from services.match_rank_image_service import validate_snapshot
from settings import EXPORTS_DIR, LOCAL_TIMEZONE, TEMP_DIR


DEFAULT_LATEST_POINTER = TEMP_DIR / "stars_cup_bot" / "latest_export.json"
DEFAULT_EXPORT_ROOT = EXPORTS_DIR / "正式榜图" / "群星杯_2026"


def _env_positive_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


DEFAULT_STALE_AFTER_SECONDS = _env_positive_int(
    "STARS_CUP_SNAPSHOT_STALE_SECONDS",
    36 * 60 * 60,
)


class StarsCupQueryError(ValueError):
    pass


class StarsCupSnapshotUnavailable(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class StarsCupQuery:
    kind: Literal["overview", "team", "player", "self"]
    selector: str = ""


@dataclass(frozen=True, slots=True)
class LoadedStarsCupSnapshot:
    snapshot: dict[str, Any]
    snapshot_path: Path
    snapshot_sha256: str
    image_paths: dict[str, Path]
    image_sha256: dict[str, str]
    run_id: str
    published_at: datetime
    age_seconds: int
    stale: bool


def parse_stars_cup_query(text: str) -> StarsCupQuery | None:
    normalized = str(text or "").strip()
    if normalized.startswith(("/", "／")):
        normalized = normalized[1:].lstrip()
    if normalized == "群星杯":
        return StarsCupQuery(kind="overview")
    if not normalized.startswith("群星杯"):
        return None
    remainder = normalized[len("群星杯") :]
    if remainder and not remainder[0].isspace():
        return None
    selectors = remainder.strip().split()
    if not selectors:
        return StarsCupQuery(kind="overview")
    if len(selectors) != 1:
        raise StarsCupQueryError("用法：/群星杯、/群星杯 A、/群星杯 玩家名、/群星杯 我")
    selector = selectors[0]
    if selector in {"我", "我的"}:
        return StarsCupQuery(kind="self")
    if len(selector) == 1 and selector.upper() in set("ABCDEF"):
        return StarsCupQuery(kind="team", selector=selector.upper())
    return StarsCupQuery(kind="player", selector=selector)


def is_stars_cup_query_message(text: str) -> bool:
    try:
        return parse_stars_cup_query(text) is not None
    except StarsCupQueryError:
        return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StarsCupSnapshotUnavailable("{} missing".format(label)) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StarsCupSnapshotUnavailable("{} unreadable".format(label)) from exc
    if not isinstance(value, dict):
        raise StarsCupSnapshotUnavailable("{} must be a JSON object".format(label))
    return value


def _resolve_artifact_path(raw_path: Any, export_root: Path) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise StarsCupSnapshotUnavailable("artifact path missing")
    root = export_root.expanduser().resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise StarsCupSnapshotUnavailable("artifact path outside export root")
    if not resolved.is_file():
        raise StarsCupSnapshotUnavailable("artifact missing")
    return resolved


def _parse_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise StarsCupSnapshotUnavailable("published_at missing")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise StarsCupSnapshotUnavailable("published_at invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def load_latest_stars_cup_snapshot(
    latest_pointer_path: Path = DEFAULT_LATEST_POINTER,
    *,
    export_root: Path = DEFAULT_EXPORT_ROOT,
    current_time: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> LoadedStarsCupSnapshot:
    pointer = _load_json_object(latest_pointer_path, "latest pointer")
    if pointer.get("schema_version") != 1:
        raise StarsCupSnapshotUnavailable("latest pointer schema unsupported")

    snapshot_entry = pointer.get("snapshot")
    images_entry = pointer.get("images")
    if not isinstance(snapshot_entry, dict) or not isinstance(images_entry, dict):
        raise StarsCupSnapshotUnavailable("latest pointer artifacts invalid")

    snapshot_path = _resolve_artifact_path(snapshot_entry.get("path"), export_root)
    snapshot_hash = str(snapshot_entry.get("sha256") or "").strip().lower()
    if not snapshot_hash or _sha256(snapshot_path) != snapshot_hash:
        raise StarsCupSnapshotUnavailable("snapshot hash mismatch")

    image_paths: dict[str, Path] = {}
    image_hashes: dict[str, str] = {}
    for kind in ("total", "detail"):
        entry = images_entry.get(kind)
        if not isinstance(entry, dict):
            raise StarsCupSnapshotUnavailable("{} image entry missing".format(kind))
        path = _resolve_artifact_path(entry.get("path"), export_root)
        expected_hash = str(entry.get("sha256") or "").strip().lower()
        if not expected_hash or _sha256(path) != expected_hash:
            raise StarsCupSnapshotUnavailable("{} image hash mismatch".format(kind))
        image_paths[kind] = path
        image_hashes[kind] = expected_hash

    try:
        snapshot = validate_snapshot(_load_json_object(snapshot_path, "snapshot"))
    except (TypeError, ValueError) as exc:
        raise StarsCupSnapshotUnavailable("snapshot contract invalid") from exc

    published_at = _parse_datetime(pointer.get("published_at"))
    run_id = str(pointer.get("run_id") or "").strip()
    if not run_id:
        raise StarsCupSnapshotUnavailable("run_id missing")
    now = current_time or datetime.now(LOCAL_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=LOCAL_TIMEZONE)
    age_seconds = max(0, int((now.astimezone(LOCAL_TIMEZONE) - published_at).total_seconds()))
    return LoadedStarsCupSnapshot(
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        snapshot_sha256=snapshot_hash,
        image_paths=image_paths,
        image_sha256=image_hashes,
        run_id=run_id,
        published_at=published_at,
        age_seconds=age_seconds,
        stale=age_seconds > max(1, int(stale_after_seconds)),
    )


def _format_integer(value: Any) -> str:
    try:
        return "{:,}".format(int(value))
    except (TypeError, ValueError):
        return "—"


def _snapshot_as_of(snapshot: dict[str, Any]) -> str:
    event = snapshot.get("event") if isinstance(snapshot.get("event"), dict) else {}
    value = str(event.get("as_of") or snapshot.get("as_of") or "未知").strip()
    if "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
            return parsed.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    return value


def _freshness_suffix(loaded: LoadedStarsCupSnapshot) -> str:
    suffix = "截至 {}".format(_snapshot_as_of(loaded.snapshot))
    if loaded.stale:
        suffix += "（数据可能已过期）"
    return suffix


def _all_players(snapshot: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    results = []
    for team in snapshot.get("teams") or []:
        if not isinstance(team, dict):
            continue
        for player in team.get("players") or []:
            if isinstance(player, dict):
                results.append((player, team))
    for player in snapshot.get("individuals") or []:
        if isinstance(player, dict):
            results.append((player, None))
    return results


def _team_label(team: dict[str, Any]) -> str:
    code = str(team.get("code") or "?").strip()
    base = "{}队".format(code)
    name = str(team.get("name") or "").strip()
    if not name or name.casefold() in {code.casefold(), base.casefold()}:
        return base
    return "{} {}".format(base, name)


def _find_player(snapshot: dict[str, Any], selector: str):
    normalized = str(selector or "").strip()
    matches = [
        item
        for item in _all_players(snapshot)
        if str(item[0].get("verse") or "").casefold() == normalized.casefold()
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    exact = [item for item in matches if str(item[0].get("verse") or "") == normalized]
    if len(exact) == 1:
        return exact[0]
    raise StarsCupQueryError("匹配到多个同名玩家，请输入名单中的精确 Verse 名称。")


def _overview_reply(loaded: LoadedStarsCupSnapshot) -> str:
    teams = sorted(
        (team for team in loaded.snapshot.get("teams") or [] if isinstance(team, dict)),
        key=lambda team: (
            team.get("rank") is None,
            int(team.get("rank") or 999),
            str(team.get("code") or ""),
        ),
    )
    lines = ["群星杯队伍榜"]
    for team in teams:
        rank = team.get("rank")
        lines.append(
            "{}. {} {}".format(
                "—" if rank is None else rank,
                _team_label(team),
                _format_integer(team.get("total_board_sum")),
            )
        )
    lines.append(_freshness_suffix(loaded))
    return "\n".join(lines)


def _team_reply(loaded: LoadedStarsCupSnapshot, selector: str) -> str:
    team = next(
        (
            item
            for item in loaded.snapshot.get("teams") or []
            if isinstance(item, dict) and str(item.get("code") or "").upper() == selector
        ),
        None,
    )
    if team is None:
        return "未找到 {} 队。".format(selector)
    completion = team.get("completion") if isinstance(team.get("completion"), dict) else {}
    players = sorted(
        (
            player
            for player in team.get("players") or []
            if isinstance(player, dict)
            and player.get("total_board_sum") is not None
        ),
        key=lambda player: (
            -(int(player.get("total_board_sum") or -1)),
            str(player.get("verse") or "").casefold(),
        ),
    )
    leaders = "；".join(
        "{} {}".format(
            str(player.get("verse") or "?"),
            _format_integer(player.get("total_board_sum")),
        )
        for player in players[:3]
    )
    rank = team.get("rank")
    return (
        "{}｜{}｜盘面和 {}｜完成 {}/{}\n"
        "队内前3：{}\n{}"
    ).format(
        _team_label(team),
        "未排名" if rank is None else "第{}名".format(rank),
        _format_integer(team.get("total_board_sum")),
        completion.get("completed", 0),
        completion.get("total", len(players)),
        leaders or "暂无成绩",
        _freshness_suffix(loaded),
    )


def _player_reply(
    loaded: LoadedStarsCupSnapshot,
    selector: str,
) -> str:
    matched = _find_player(loaded.snapshot, selector)
    if matched is None:
        return "群星杯名单中未找到玩家：{}".format(selector)
    player, team = matched
    top_scores = [
        _format_integer(value)
        for value in (player.get("top_scores") or [])
        if value is not None
    ]
    required_games = int(loaded.snapshot.get("required_games") or 3)
    team_label = "个人组" if team is None else "{}队".format(team.get("code") or "?")
    tier = player.get("tier")
    tier_rank = player.get("tier_rank")
    tier_text = ""
    if tier is not None:
        tier_text = "｜第{}档".format(tier)
        if tier_rank is not None:
            tier_text += " 档位第{}".format(tier_rank)
    return (
        "{}｜{}{}\nTop{}：{}\n总盘面和 {}｜已计 {} 局\n{}"
    ).format(
        str(player.get("verse") or selector),
        team_label,
        tier_text,
        required_games,
        " / ".join(top_scores) if top_scores else "暂无成绩",
        _format_integer(player.get("total_board_sum")),
        min(len(top_scores), required_games),
        _freshness_suffix(loaded),
    )


def build_stars_cup_query_reply(
    loaded: LoadedStarsCupSnapshot,
    query: StarsCupQuery,
    *,
    bound_verse_account: str | None = None,
) -> str:
    if query.kind == "overview":
        return _overview_reply(loaded)
    if query.kind == "team":
        return _team_reply(loaded, query.selector)
    selector = query.selector
    if query.kind == "self":
        selector = str(bound_verse_account or "").strip()
        if not selector:
            return "你还没有绑定 Verse 账号，请先私聊 Bot 发送“绑定”。"
    return _player_reply(loaded, selector)
