import json
import os
from http.client import RemoteDisconnected
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen
from pathlib import Path
from time import sleep

from settings import (
    LOCAL_TIMEZONE,
    VERSE_API_BASE_URL,
    VERSE_LEADERBOARD_API_BASE_URL,
    VERSE_REQUEST_TIMEOUT,
    VERSE_USER_AGENT,
)


VERSE_REQUEST_RETRY_COUNT = max(1, int(str(os.getenv("VERSE_REQUEST_RETRY_COUNT", "3")).strip() or "3"))
VERSE_REQUEST_RETRY_BACKOFF_SECONDS = max(
    0.05,
    float(str(os.getenv("VERSE_REQUEST_RETRY_BACKOFF_SECONDS", "0.2")).strip() or "0.2"),
)


def now_local():
    return datetime.now(LOCAL_TIMEZONE)


def parse_verse_time(value: str):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(LOCAL_TIMEZONE)


def parse_local_time(value: str):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def build_user_url(username: str, variant: str, page: int, *, sort: str = "date", desc: bool = True):
    safe_username = quote(username, safe="")
    return "{}?username={}&sort={}&variant={}&page={}&desc={}".format(
        VERSE_API_BASE_URL,
        safe_username,
        quote(str(sort or "date"), safe=""),
        variant,
        page,
        "true" if desc else "false",
    )


def build_leaderboard_url(variant: str, time_key: str, page: int):
    return "{}?time={}&variant={}&page={}".format(
        VERSE_LEADERBOARD_API_BASE_URL,
        quote(str(time_key or "all"), safe=""),
        quote(str(variant or ""), safe=""),
        int(page),
    )


def _snapshot_dir():
    raw = str(os.getenv("VERSE_API_SNAPSHOT_DIR", "")).strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _first_query_value(query: dict[str, list[str]], key: str):
    values = query.get(key)
    if not values:
        return None
    value = values[0]
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _snapshot_file_names_for_url(url: str):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    query = parse_qs(parsed.query)
    if path.endswith("/leaderboard/all"):
        variant = _first_query_value(query, "variant")
        time_key = _first_query_value(query, "time") or "all"
        candidates = []
        if variant:
            candidates.append("leaderboard_response_{}_{}.json".format(variant, time_key))
            candidates.append("leaderboard_response_{}.json".format(variant))
        candidates.append("leaderboard_response_{}.json".format(time_key))
        candidates.append("leaderboard_response.json")
        return candidates
    if path.endswith("/leaderboard/user"):
        username = _first_query_value(query, "username")
        variant = _first_query_value(query, "variant")
        candidates = []
        if username and variant:
            candidates.append("player_profile_response_{}_{}.json".format(username, variant))
        if variant:
            candidates.append("player_profile_response_{}.json".format(variant))
        if username:
            candidates.append("player_profile_response_{}.json".format(username))
        candidates.append("player_profile_response.json")
        return candidates
    return []


def _load_snapshot_json(url: str):
    snapshot_dir = _snapshot_dir()
    if snapshot_dir is None:
        return None
    file_names = _snapshot_file_names_for_url(url)
    for file_name in file_names:
        snapshot_path = snapshot_dir / file_name
        if not snapshot_path.exists():
            continue
        try:
            return json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def fetch_json(url: str):
    snapshot = _load_snapshot_json(url)
    if snapshot is not None:
        return snapshot
    request = Request(url, headers={"User-Agent": VERSE_USER_AGENT})
    last_error = None
    for attempt in range(VERSE_REQUEST_RETRY_COUNT):
        try:
            with urlopen(request, timeout=VERSE_REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (RemoteDisconnected, ConnectionResetError, TimeoutError, BrokenPipeError, HTTPError, URLError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
            if attempt + 1 >= VERSE_REQUEST_RETRY_COUNT:
                break
            sleep(VERSE_REQUEST_RETRY_BACKOFF_SECONDS * float(attempt + 1))
    return None


def get_game_start_time(game):
    for key in ("started_at", "created_at", "start_time", "startedAt", "createdAt"):
        value = game.get(key)
        if isinstance(value, str) and value.strip():
            try:
                return parse_verse_time(value)
            except ValueError:
                continue
    return None


def get_game_end_time(game):
    value = game.get("played_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_verse_time(value)
    except ValueError:
        return None


def _extract_ints_from_board_payload(payload, output):
    if isinstance(payload, int):
        output.append(payload)
        return
    if isinstance(payload, str):
        text = payload.strip()
        if text.isdigit():
            output.append(int(text))
        return
    if isinstance(payload, dict):
        for value in payload.values():
            _extract_ints_from_board_payload(value, output)
        return
    if isinstance(payload, list):
        for item in payload:
            _extract_ints_from_board_payload(item, output)
        return


def get_game_terminal_board_sum(game):
    if not isinstance(game, dict):
        return None
    board = game.get("board")
    if board is None:
        return None
    values = []
    _extract_ints_from_board_payload(board, values)
    if not values:
        return None
    return int(sum(values))


def fetch_recent_games(username: str, variant: str, not_before, max_pages: int = 10, stop_when_before: bool = True):
    collected = []
    seen_ids = set()
    for page in range(1, max_pages + 1):
        data = fetch_json(build_user_url(username, variant, page))
        if not data:
            break
        games = data.get("games", [])
        if not games:
            break

        stop_after_page = False
        for game in games:
            game_id = game.get("id")
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)

            start_dt = get_game_start_time(game)
            end_dt = get_game_end_time(game)
            if start_dt is not None and start_dt >= not_before:
                collected.append(game)
                continue
            if start_dt is None and end_dt is not None and end_dt >= not_before:
                collected.append(game)
                continue
            if stop_when_before and end_dt is not None and end_dt < not_before and (start_dt is None or start_dt < not_before):
                stop_after_page = True

        if stop_after_page:
            break

    collected.sort(
        key=lambda item: (
            get_game_start_time(item) or get_game_end_time(item) or now_local(),
            get_game_end_time(item) or now_local(),
        )
    )
    return collected


def find_replay_like_fields(game):
    if not isinstance(game, dict):
        return []
    matches = []
    for key in game.keys():
        text = str(key).lower()
        if "replay" in text or "vrs" in text:
            matches.append(str(key))
    return matches


def inspect_recent_game_fields(username: str, variant: str, not_before, max_pages: int = 2):
    games = fetch_recent_games(username, variant, not_before, max_pages=max_pages, stop_when_before=False)
    key_counts = {}
    replay_like_keys = set()
    for game in games:
        if not isinstance(game, dict):
            continue
        for key in game.keys():
            key_counts[str(key)] = key_counts.get(str(key), 0) + 1
        for key in find_replay_like_fields(game):
            replay_like_keys.add(key)
    return {
        "game_count": len(games),
        "key_counts": key_counts,
        "replay_like_keys": sorted(replay_like_keys),
        "sample_game": games[0] if games else None,
    }
