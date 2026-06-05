import json
import math
import os
import re
import threading
from statistics import NormalDist
from datetime import datetime, timedelta
from time import perf_counter

from services.verse_adapter import build_leaderboard_url, build_user_url, fetch_json, get_game_end_time, parse_local_time, parse_verse_time
from settings import LOCAL_TIMEZONE


MODE_TOKEN_TO_VARIANT = {
    "44": "4x4",
    "34": "3x4",
    "33": "3x3",
    "24": "2x4",
}

VARIANT_TO_MODE_LABEL = {
    "4x4": "4x4",
    "3x4": "3x4",
    "3x3": "3x3",
    "2x4": "2x4",
}

RATING_BUCKET_BY_VARIANT = {
    "4x4": "classic_4x4_raw_score",
    "3x4": "points_series_3x4",
    "3x3": "timed_3x3",
    "2x4": "timed_2x4",
}

# Matches the reservation scoring rules definition for timed modes.
TIMED_SCORING_RULES = {
    "2x4": [
        (18, (512, 256, 128, 64), 4),
        (17, (512, 256, 128, 32, 16, 8, 4, 2), 4),
        (15, (512, 256, 128), 3),
        (14, (512, 256, 64, 32, 16, 8, 4, 2), 3),
        (12, (512, 256, 64), 2),
        (11, (512, 256), 2),
        (10, (512, 128, 64, 32, 16, 8, 4, 2), 2),
        (8, (512, 128, 64), 1),
        (7, (512, 128), 1),
        (6, (512,), 1),
        (5, (256, 128, 64, 32, 16, 8, 4, 2), 1),
        (3, (256, 128, 64), 0),
        (2, (256, 128), 0),
        (1, (256,), 0),
    ],
    "3x3": [
        (25, (1024, 512, 256, 128, 64), 5),
        (24, (1024, 512, 256, 128, 32, 16, 8, 4, 2), 5),
        (22, (1024, 512, 256, 128), 4),
        (21, (1024, 512, 256, 64, 32, 16, 8, 4, 2), 4),
        (19, (1024, 512, 256, 64), 3),
        (18, (1024, 512, 256), 3),
        (17, (1024, 512, 128, 64, 32, 16, 8, 4, 2), 3),
        (15, (1024, 512, 128, 64), 2),
        (14, (1024, 512, 128), 2),
        (13, (1024, 512), 2),
        (12, (1024, 256, 128, 64, 32, 16, 8, 4, 2), 2),
        (10, (1024, 256, 128, 64), 1),
        (9, (1024, 256, 128), 1),
        (8, (1024, 256), 1),
        (7, (1024,), 1),
        (6, (512, 256, 128, 64, 32, 16, 8, 4, 2), 1),
        (4, (512, 256, 128, 64), 0),
        (3, (512, 256, 128), 0),
        (2, (512, 256), 0),
        (1, (512,), 0),
    ],
}


TOKEN_SET = {
    "8ks",
    "16ks",
    "32ks",
    "65ks",
    "8/16",
    "16/32",
    "24/32",
    "28/32",
    "30/32",
    "31/32",
    "f512",
    "f256",
    "f128",
    "4ks",
    "2/4",
    "3/4",
    "24满盘",
    "33满盘",
    "24满盘x",
    "33满盘x",
    "512s",
    "768s",
    "1024s",
    "1536s",
    "32k综率",
    "4x4",
    "3x4",
    "3x3",
    "2x4",
}


def _env_positive_int(name, default_value):
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default_value
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return value if value > 0 else default_value


VERSE_QUERY_MAX_PAGES = _env_positive_int("VERSE_QUERY_MAX_PAGES", 1000)
VERSE_QUERY_MAX_PAGES_LIGHT = _env_positive_int("VERSE_QUERY_MAX_PAGES_LIGHT", 20)
VERSE_QUERY_MAX_PAGES_MEDIUM = _env_positive_int("VERSE_QUERY_MAX_PAGES_MEDIUM", 80)
VERSE_QUERY_CACHE_TTL_SECONDS = _env_positive_int("VERSE_QUERY_CACHE_TTL_SECONDS", 120)
VERSE_QUERY_FETCH_BUDGET_SECONDS = _env_positive_int("VERSE_QUERY_FETCH_BUDGET_SECONDS", 20)
VERSE_QUERY_HEAVY_FETCH_BUDGET_SECONDS = _env_positive_int("VERSE_QUERY_HEAVY_FETCH_BUDGET_SECONDS", 180)
VERSE_QUERY_HEAVY_QUERY_CACHE_TTL_SECONDS = _env_positive_int("VERSE_QUERY_HEAVY_QUERY_CACHE_TTL_SECONDS", 45)
VERSE_QUERY_RAWR_SCAN_LIMIT = _env_positive_int("VERSE_QUERY_RAWR_SCAN_LIMIT", 20)
VERSE_QUERY_SCORE_SCAN_EMPTY_PAGES = _env_positive_int("VERSE_QUERY_SCORE_SCAN_EMPTY_PAGES", 1)
VERSE_QUERY_4X4_32K_RATIO_SCORE_FLOOR = _env_positive_int("VERSE_QUERY_4X4_32K_RATIO_SCORE_FLOOR", 430000)
VERSE_QUERY_REPLY_CACHE_TTL_SECONDS = _env_positive_int("VERSE_QUERY_REPLY_CACHE_TTL_SECONDS", 5)
VERSE_QUERY_INFLIGHT_WAIT_TIMEOUT_SECONDS = _env_positive_int("VERSE_QUERY_INFLIGHT_WAIT_TIMEOUT_SECONDS", 30)
VERSE_QUERY_PAGE_SIZE = 50
VERSE_QUERY_GAME_CACHE = {}
VERSE_QUERY_PROFILE_CACHE = {}
VERSE_QUERY_LEADERBOARD_CACHE = {}
VERSE_QUERY_HEAVY_QUERY_CACHE = {}
VERSE_QUERY_REPLY_CACHE = {}
VERSE_QUERY_INFLIGHT = {}
VERSE_QUERY_CACHE_LOCK = threading.Lock()


def _normalize_query_token(token):
    return str(token or "").strip().lower().replace("×", "x")


def _normalize_cache_key_component(value):
    if value is None:
        return "-"
    text = str(value).strip()
    return text.lower() if text else "-"


def _verse_reply_cache_key(bot_platform, bot_user_id, parsed, variant_code):
    username = _normalize_cache_key_component(parsed.get("username"))
    token = _normalize_cache_key_component(parsed.get("token"))
    variant_override = _normalize_cache_key_component(parsed.get("variant_override"))
    lookup_variant = _normalize_cache_key_component(variant_code or "4x4")
    shared_query = username != "-" or token in {"wr", "rawr"}
    key_parts = [
        _normalize_cache_key_component(bot_platform),
        username,
        token,
        variant_override,
        lookup_variant,
    ]
    if not shared_query:
        key_parts.insert(1, _normalize_cache_key_component(bot_user_id))
    return tuple(key_parts)


def _verse_reply_cache_get(cache_key):
    now = datetime.now(LOCAL_TIMEZONE)
    with VERSE_QUERY_CACHE_LOCK:
        cached = VERSE_QUERY_REPLY_CACHE.get(cache_key)
        if not isinstance(cached, dict):
            return None
        expires_at = cached.get("expires_at")
        if not isinstance(expires_at, datetime) or expires_at <= now:
            VERSE_QUERY_REPLY_CACHE.pop(cache_key, None)
            return None
        return cached.get("reply")


def _verse_reply_cache_set(cache_key, reply):
    with VERSE_QUERY_CACHE_LOCK:
        VERSE_QUERY_REPLY_CACHE[cache_key] = {
            "reply": reply,
            "expires_at": datetime.now(LOCAL_TIMEZONE) + timedelta(seconds=VERSE_QUERY_REPLY_CACHE_TTL_SECONDS),
        }


def _verse_reply_inflight_acquire(cache_key):
    with VERSE_QUERY_CACHE_LOCK:
        entry = VERSE_QUERY_INFLIGHT.get(cache_key)
        if entry is not None:
            return entry, False
        entry = {
            "event": threading.Event(),
            "reply": None,
            "error": None,
            "finished": False,
        }
        VERSE_QUERY_INFLIGHT[cache_key] = entry
        return entry, True


def _verse_reply_inflight_release(cache_key, entry, *, reply=None, error=None):
    with VERSE_QUERY_CACHE_LOCK:
        entry["reply"] = reply
        entry["error"] = error
        entry["finished"] = True
        entry["event"].set()
        VERSE_QUERY_INFLIGHT.pop(cache_key, None)


def _verse_reply_run_once(cache_key, builder):
    entry, is_leader = _verse_reply_inflight_acquire(cache_key)
    if not is_leader:
        if entry["event"].wait(timeout=float(VERSE_QUERY_INFLIGHT_WAIT_TIMEOUT_SECONDS)):
            if entry.get("error") is not None:
                raise entry["error"]
            return entry.get("reply")
        return builder()

    try:
        cached = _verse_reply_cache_get(cache_key)
        if cached is not None:
            _verse_reply_inflight_release(cache_key, entry, reply=cached)
            return cached
        reply = builder()
        _verse_reply_cache_set(cache_key, reply)
        _verse_reply_inflight_release(cache_key, entry, reply=reply)
        return reply
    except Exception as exc:
        _verse_reply_inflight_release(cache_key, entry, error=exc)
        raise


def _heavy_query_cache_key(*parts):
    return "::".join(_normalize_cache_key_component(part) for part in parts)


def _heavy_query_cache_get(cache_key):
    now = datetime.now(LOCAL_TIMEZONE)
    with VERSE_QUERY_CACHE_LOCK:
        cached = VERSE_QUERY_HEAVY_QUERY_CACHE.get(cache_key)
        if not isinstance(cached, dict):
            return None
        expires_at = cached.get("expires_at")
        if not isinstance(expires_at, datetime) or expires_at <= now:
            return None
        value = cached.get("value")
        return list(value) if isinstance(value, list) else value


def _heavy_query_cache_get_stale(cache_key):
    with VERSE_QUERY_CACHE_LOCK:
        cached = VERSE_QUERY_HEAVY_QUERY_CACHE.get(cache_key)
        if not isinstance(cached, dict):
            return None
        value = cached.get("value")
        return list(value) if isinstance(value, list) else value


def _heavy_query_cache_set(cache_key, value):
    with VERSE_QUERY_CACHE_LOCK:
        VERSE_QUERY_HEAVY_QUERY_CACHE[cache_key] = {
            "expires_at": datetime.now(LOCAL_TIMEZONE) + timedelta(seconds=VERSE_QUERY_HEAVY_QUERY_CACHE_TTL_SECONDS),
            "value": list(value) if isinstance(value, list) else value,
        }


def _token_is_supported(token):
    if not token:
        return False
    lowered = _normalize_query_token(token)
    if lowered in TOKEN_SET:
        return True
    if re.match(r"^(44|33|24|34)ra$", lowered):
        return True
    if re.match(r"^(44|34)mra$", lowered):
        return True
    if re.match(r"^(44|33|24|34)pb$", lowered):
        return True
    if re.match(r"^(44|34)r5$", lowered):
        return True
    if re.match(r"^(42|43)[a-z0-9/]+$", lowered):
        return True
    if re.match(r"^(24|33)满盘\d+$", lowered):
        return True
    return False


def parse_verse_query_message(text):
    message = str(text or "").strip()
    if not message:
        return None
    parts = message.split()
    if len(parts) == 1:
        token = _normalize_query_token(parts[0])
        if _token_is_supported(token):
            return {"username": None, "token": token}
        return None
    if len(parts) == 2:
        first = _normalize_query_token(parts[0])
        token = _normalize_query_token(parts[1])
        variant_override = first if first in {"4x4", "3x4", "3x3", "2x4"} else MODE_TOKEN_TO_VARIANT.get(first)
        if variant_override and token in {"wr", "rawr"}:
            return {"username": None, "token": token, "variant_override": variant_override}
        username = parts[0].strip()
        if _token_is_supported(token):
            return {"username": username, "token": token}
        return None
    return None


def is_verse_query_message(text):
    return parse_verse_query_message(text) is not None


def _parse_json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return parse_local_time(text)
    except Exception:
        pass
    try:
        return parse_verse_time(text)
    except Exception:
        return None


def _collect_board_values(payload, output):
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
            _collect_board_values(value, output)
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_board_values(item, output)


def _deep_find_value(payload, candidate_keys):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in candidate_keys and value not in (None, ""):
                return value
        for value in payload.values():
            nested = _deep_find_value(value, candidate_keys)
            if nested not in (None, ""):
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _deep_find_value(item, candidate_keys)
            if nested not in (None, ""):
                return nested
    return None


def _safe_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _game_max_tile(game_payload, board_values):
    if board_values:
        return max(int(value) for value in board_values if value)
    return _safe_int(
        _deep_find_value(
            game_payload,
            {
                "max_tile",
                "maxtile",
                "max_tile_value",
                "tile",
                "tile_value",
                "top_tile",
                "largest_tile",
                "max",
            },
        )
    )


def _extract_board_values(row_payload):
    values = []
    board_payload = row_payload.get("board_state")
    if board_payload is not None:
        _collect_board_values(board_payload, values)
    game_payload = row_payload.get("game_payload") or {}
    if isinstance(game_payload, dict):
        for key in ("board", "board_state", "grid"):
            board = game_payload.get(key)
            if board is not None:
                _collect_board_values(board, values)
    return [int(value) for value in values if isinstance(value, int) and value > 0]


def _normalize_game_row(row):
    raw_payload = _parse_json(row["raw_payload_json"])
    board_state = _parse_json(row["board_state_json"])
    row_payload = {
        "game_payload": raw_payload if isinstance(raw_payload, dict) else {},
        "board_state": board_state,
    }
    board_values = _extract_board_values(row_payload)
    score = row["final_score"] if row["final_score"] is not None else row["raw_score"]
    if score is None and isinstance(raw_payload, dict):
        score = _safe_int(raw_payload.get("score"))
    ended_at = _parse_dt(row["ended_at"])
    if ended_at is None and isinstance(raw_payload, dict):
        ended_at = _parse_dt(raw_payload.get("played_at"))
    return {
        "id": row["id"],
        "score": score,
        "ended_at": ended_at,
        "board_values": board_values,
        "board_sum": sum(board_values) if board_values else None,
        "max_tile": _game_max_tile(raw_payload, board_values),
    }


def _normalize_live_game(game_payload, fallback_id):
    payload = game_payload if isinstance(game_payload, dict) else {}
    row_payload = {"game_payload": payload, "board_state": None}
    board_values = _extract_board_values(row_payload)
    score = _safe_int(payload.get("score"))
    if score is None:
        score = _safe_int(payload.get("final_score"))
    if score is None:
        score = _safe_int(payload.get("raw_score"))
    ended_at = get_game_end_time(payload) or _parse_dt(payload.get("played_at"))
    game_id = payload.get("id")
    return {
        "id": game_id if game_id not in (None, "") else fallback_id,
        "score": score,
        "ended_at": ended_at,
        "board_values": board_values,
        "board_sum": sum(board_values) if board_values else None,
        "max_tile": _game_max_tile(payload, board_values),
    }


def _live_cache_key(username, variant_code):
    return "{}::{}".format(str(username or "").strip().lower(), str(variant_code or "").strip().lower())


def _fetch_user_page(username, variant_code, page, *, sort="date", desc=True):
    return fetch_json(build_user_url(username, variant_code, int(page), sort=sort, desc=desc))


def _cached_complete_games(key, *, expected_total_games=None):
    now = datetime.now(LOCAL_TIMEZONE)
    cached = VERSE_QUERY_GAME_CACHE.get(key)
    if not isinstance(cached, dict):
        return None
    expires_at = cached.get("expires_at")
    if not isinstance(expires_at, datetime) or expires_at <= now:
        return None
    if not cached.get("complete"):
        return None
    if expected_total_games is not None and cached.get("total_games") not in {None, expected_total_games}:
        return None
    return list(cached.get("games") or [])


def _store_game_cache(key, games, *, total_games=None, pages_fetched=0, complete=False):
    VERSE_QUERY_GAME_CACHE[key] = {
        "expires_at": datetime.now(LOCAL_TIMEZONE) + timedelta(seconds=VERSE_QUERY_CACHE_TTL_SECONDS),
        "games": list(games or []),
        "total_games": total_games,
        "pages_fetched": int(pages_fetched or 0),
        "complete": bool(complete),
        "fetched_at": datetime.now(LOCAL_TIMEZONE),
    }


def _collect_live_games(username, variant_code, *, total_pages, sort="date", desc=True, budget_seconds=None, first_payload=None):
    collected = []
    seen_ids = set()
    first_page_ok = False
    started = perf_counter()
    pages_fetched = 0
    complete = True
    for page in range(1, int(total_pages) + 1):
        if budget_seconds is not None and perf_counter() - started > float(budget_seconds):
            complete = False
            break
        if page == 1 and isinstance(first_payload, dict):
            payload = first_payload
        else:
            payload = _fetch_user_page(username, variant_code, page, sort=sort, desc=desc)
        if not isinstance(payload, dict):
            if not first_page_ok:
                return None, 0, False
            complete = False
            break
        first_page_ok = True
        pages_fetched += 1
        games = payload.get("games")
        if not isinstance(games, list) or not games:
            break
        for index, game in enumerate(games):
            normalized = _normalize_live_game(game, fallback_id="p{}-{}".format(page, index))
            game_id = str(normalized["id"])
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)
            collected.append(normalized)
        if len(games) < VERSE_QUERY_PAGE_SIZE:
            break
    collected.sort(
        key=lambda item: (
            item.get("ended_at") or datetime.min.replace(tzinfo=LOCAL_TIMEZONE),
            str(item.get("id")),
        ),
        reverse=True,
    )
    return collected, pages_fetched, complete


def _load_live_games(username, variant_code, profile_snapshot=None):
    key = _live_cache_key(username, variant_code)
    total_games = None if profile_snapshot is None else _safe_int(profile_snapshot.get("total_games"))
    cached_games = _cached_complete_games(key, expected_total_games=total_games)
    if cached_games is not None:
        return cached_games

    first_payload = None
    if total_games is None:
        profile_snapshot = _load_live_rating_snapshot(username, variant_code)
        total_games = None if profile_snapshot is None else _safe_int(profile_snapshot.get("total_games"))
    if total_games is None:
        first_payload = _fetch_user_page(username, variant_code, 1)
        if not isinstance(first_payload, dict):
            return None
        total_games = _safe_int(first_payload.get("totalGames"))
    total_pages = 1 if not total_games or total_games <= 0 else max(1, int(math.ceil(float(total_games) / float(VERSE_QUERY_PAGE_SIZE))))
    fetch_pages = min(total_pages, int(VERSE_QUERY_MAX_PAGES))
    collected, pages_fetched, complete = _collect_live_games(
        username,
        variant_code,
        total_pages=fetch_pages,
        sort="date",
        desc=True,
        budget_seconds=VERSE_QUERY_HEAVY_FETCH_BUDGET_SECONDS,
        first_payload=first_payload,
    )
    if collected is None:
        stale_games = _cached_complete_games(key, expected_total_games=total_games)
        if stale_games is not None:
            return stale_games
        return None
    complete = bool(complete and fetch_pages >= total_pages and pages_fetched >= fetch_pages)
    _store_game_cache(key, collected, total_games=total_games, pages_fetched=pages_fetched, complete=complete)
    if complete:
        return collected
    stale_games = _cached_complete_games(key, expected_total_games=total_games)
    if stale_games is not None:
        return stale_games
    if total_games == 0:
        return []
    return None


def _load_local_games(connection, player_id, variant_code):
    rows = connection.execute(
        """
        SELECT
            pr.id,
            pr.raw_score,
            pr.final_score,
            pr.ended_at,
            pr.raw_payload_json,
            pr.board_state_json
        FROM performance_records pr
        JOIN player_accounts pa ON pa.id = pr.player_account_id
        LEFT JOIN variants v ON v.id = pr.variant_id
        JOIN platforms p ON p.id = pr.platform_id
        WHERE pa.player_id = ?
          AND p.code = '2048verse'
          AND v.code = ?
          AND pr.is_completed = 1
          AND pr.is_valid_source = 1
        ORDER BY COALESCE(pr.ended_at, '') DESC, pr.id DESC
        """,
        (player_id, variant_code),
    ).fetchall()
    return [_normalize_game_row(row) for row in rows]


def _load_games(connection, player_id, username, variant_code, profile_snapshot=None, *, allow_local_fallback=True):
    live_rows = _load_live_games(username, variant_code, profile_snapshot=profile_snapshot)
    if live_rows is not None:
        return live_rows
    if not allow_local_fallback or player_id is None:
        return None
    return _load_local_games(connection, player_id, variant_code)


def _load_live_rating_snapshot(username, variant_code):
    key = _live_cache_key(username, "profile::{}".format(variant_code))
    now = datetime.now(LOCAL_TIMEZONE)
    cached = VERSE_QUERY_PROFILE_CACHE.get(key)
    if isinstance(cached, dict):
        expires_at = cached.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at > now:
            snapshot = cached.get("snapshot")
            return dict(snapshot) if isinstance(snapshot, dict) else snapshot
        stale_snapshot = cached.get("snapshot")
    else:
        stale_snapshot = None

    payload = fetch_json(build_user_url(username, variant_code, 1))
    if not isinstance(payload, dict):
        return dict(stale_snapshot) if isinstance(stale_snapshot, dict) else stale_snapshot

    rating_value = _safe_float(payload.get("rating"))
    rank_value = _safe_int(payload.get("rank"))
    total_games = _safe_int(payload.get("totalGames"))
    best_score = _safe_int(payload.get("hs"))
    if rating_value is None and rank_value is None and total_games is None and best_score is None:
        return dict(stale_snapshot) if isinstance(stale_snapshot, dict) else stale_snapshot

    snapshot = {
        "source": "verse_live",
        "rating_value": rating_value,
        "rank_value": rank_value,
        "total_games": total_games,
        "best_score": best_score,
    }
    VERSE_QUERY_PROFILE_CACHE[key] = {
        "expires_at": now + timedelta(seconds=VERSE_QUERY_CACHE_TTL_SECONDS),
        "snapshot": dict(snapshot),
    }
    return snapshot


def _load_live_leaderboard_rows(variant_code, *, time_key="all", page=1):
    key = "{}::{}::{}".format(str(variant_code or "").strip().lower(), str(time_key or "all").strip().lower(), int(page))
    now = datetime.now(LOCAL_TIMEZONE)
    cached = VERSE_QUERY_LEADERBOARD_CACHE.get(key)
    if isinstance(cached, dict):
        expires_at = cached.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at > now:
            return list(cached.get("rows") or [])
        stale_rows = list(cached.get("rows") or [])
    else:
        stale_rows = None

    payload = fetch_json(build_leaderboard_url(variant_code, time_key, page))
    if not isinstance(payload, dict):
        return stale_rows
    rows = payload.get("leaderboard")
    if not isinstance(rows, list):
        return stale_rows
    normalized = [dict(item) for item in rows if isinstance(item, dict)]
    VERSE_QUERY_LEADERBOARD_CACHE[key] = {
        "expires_at": now + timedelta(seconds=VERSE_QUERY_CACHE_TTL_SECONDS),
        "rows": list(normalized),
    }
    return normalized


def _leaderboard_row_rating(row):
    if not isinstance(row, dict):
        return None
    rating_value = _safe_float(row.get("rating"))
    if rating_value is not None:
        return rating_value
    rating_snapshot = row.get("_rating_snapshot")
    if isinstance(rating_snapshot, dict):
        return _safe_float(rating_snapshot.get("rating_value"))
    return None


def _pick_bucket_code(variant_code):
    return RATING_BUCKET_BY_VARIANT.get(variant_code)


def _load_rating_snapshot(connection, player_id, username, variant_code, *, allow_local_fallback=True):
    live_snapshot = _load_live_rating_snapshot(username, variant_code)
    if live_snapshot is not None:
        return live_snapshot
    if not allow_local_fallback or player_id is None:
        return None

    bucket_code = _pick_bucket_code(variant_code)
    if not bucket_code:
        return None
    row = connection.execute(
        """
        SELECT
            pr.rating_value,
            pr.rating_deviation,
            pr.event_count,
            pr.best_rating,
            pr.last_updated_at,
            rb.code AS bucket_code,
            (
                SELECT COUNT(*) + 1
                FROM player_ratings better
                WHERE better.rating_bucket_id = pr.rating_bucket_id
                  AND (
                      better.rating_value > pr.rating_value
                      OR (
                          better.rating_value = pr.rating_value
                          AND better.rating_deviation < pr.rating_deviation
                      )
                  )
            ) AS rank_value
        FROM player_ratings pr
        JOIN rating_buckets rb ON rb.id = pr.rating_bucket_id
        WHERE pr.player_id = ? AND rb.code = ?
        """,
        (player_id, bucket_code),
    ).fetchone()
    if row is None:
        return None
    snapshot = dict(row)
    snapshot["source"] = "local"
    return snapshot


def _load_player_from_binding(connection, bot_platform, bot_user_id):
    row = connection.execute(
        """
        SELECT
            p.id AS player_id,
            p.display_name,
            pa.account_key AS username
        FROM bot_account_bindings bb
        JOIN players p ON p.id = bb.player_id
        JOIN player_accounts pa
          ON pa.player_id = p.id
        JOIN platforms pl
          ON pl.id = pa.platform_id
        WHERE bb.bot_platform = ?
          AND bb.bot_user_id = ?
          AND bb.is_active = 1
          AND pl.code = '2048verse'
        ORDER BY pa.is_primary DESC, pa.id ASC
        LIMIT 1
        """,
        (bot_platform, bot_user_id),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _load_player_by_username(connection, username):
    keyword = str(username or "").strip()
    if not keyword:
        return None
    rows = connection.execute(
        """
        SELECT
            p.id AS player_id,
            p.display_name,
            pa.account_key AS username
        FROM player_accounts pa
        JOIN players p ON p.id = pa.player_id
        JOIN platforms pl ON pl.id = pa.platform_id
        WHERE pl.code = '2048verse'
          AND (
              LOWER(pa.account_key) = LOWER(?)
              OR LOWER(pa.account_name) = LOWER(?)
              OR LOWER(p.display_name) = LOWER(?)
          )
        ORDER BY pa.is_primary DESC, pa.id ASC
        LIMIT 2
        """,
        (keyword, keyword, keyword),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    if len(rows) > 1:
        samples = ["{} ({})".format(row["display_name"], row["username"]) for row in rows[:2]]
        raise ValueError("匹配到多个用户：{}。请提供更精确的用户名。".format("、".join(samples)))

    # Soft fallback: fuzzy matching, stable-first ordering.
    like = "%{}%".format(keyword)
    rows = connection.execute(
        """
        SELECT
            p.id AS player_id,
            p.display_name,
            pa.account_key AS username
        FROM player_accounts pa
        JOIN players p ON p.id = pa.player_id
        JOIN platforms pl ON pl.id = pa.platform_id
        WHERE pl.code = '2048verse'
          AND (
              LOWER(pa.account_key) LIKE LOWER(?)
              OR LOWER(pa.account_name) LIKE LOWER(?)
              OR LOWER(p.display_name) LIKE LOWER(?)
          )
        ORDER BY pa.is_primary DESC, p.id ASC, pa.id ASC
        LIMIT 2
        """,
        (like, like, like),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    if len(rows) > 1:
        samples = ["{} ({})".format(row["display_name"], row["username"]) for row in rows[:2]]
        raise ValueError("匹配到多个用户：{}。请提供更精确的用户名。".format("、".join(samples)))
    return None


def _load_live_player_by_username(username, variant_code):
    candidate = str(username or "").strip()
    if not candidate:
        return None
    probe_variant = variant_code or "4x4"
    payload = _fetch_user_page(candidate, probe_variant, 1)
    if not isinstance(payload, dict):
        return None
    resolved_username = str(payload.get("username") or "").strip()
    if not resolved_username:
        return None
    return {
        "player_id": None,
        "display_name": resolved_username,
        "username": resolved_username,
        "source": "verse_live",
    }


def _resolve_target_player(connection, *, bot_platform, bot_user_id, explicit_username, variant_code=None):
    if explicit_username:
        player = _load_live_player_by_username(explicit_username, variant_code)
        if player is not None:
            return player
        player = _load_player_by_username(connection, explicit_username)
        if player is not None:
            return player
        raise ValueError("未找到该用户：{}".format(explicit_username))
    player = _load_player_from_binding(connection, bot_platform, bot_user_id)
    if player is None:
        raise ValueError("你还没有绑定账号。请发送：绑定 你的verse用户名")
    return player


def _format_float(value, ndigits=2):
    if value is None:
        return "-"
    return ("{:.%df}" % ndigits).format(float(value))


def _month_range_of_previous_month(now):
    month_start = datetime(year=now.year, month=now.month, day=1, tzinfo=now.tzinfo)
    prev_end = month_start
    prev_start_date = (month_start.date() - timedelta(days=1)).replace(day=1)
    prev_start = datetime(
        year=prev_start_date.year,
        month=prev_start_date.month,
        day=1,
        tzinfo=now.tzinfo,
    )
    return prev_start, prev_end


def _load_month_games(username, variant_code):
    cache_key = _heavy_query_cache_key("month_games", username, variant_code)
    cached = _heavy_query_cache_get(cache_key)
    if cached is not None:
        return cached
    now = datetime.now(LOCAL_TIMEZONE)
    start, end = _month_range_of_previous_month(now)
    collected = []
    seen_ids = set()
    started = perf_counter()
    max_pages = int(VERSE_QUERY_MAX_PAGES_LIGHT)
    for page in range(1, max_pages + 1):
        if perf_counter() - started > float(VERSE_QUERY_FETCH_BUDGET_SECONDS):
            break
        payload = _fetch_user_page(username, variant_code, page, sort="date", desc=True)
        if not isinstance(payload, dict):
            stale = _heavy_query_cache_get_stale(cache_key)
            return stale if stale is not None else []
        games = payload.get("games")
        if not isinstance(games, list) or not games:
            break
        page_has_month = False
        page_all_before_month = True
        for index, game in enumerate(games):
            normalized = _normalize_live_game(game, fallback_id="m{}-{}".format(page, index))
            game_id = str(normalized["id"])
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)
            ended_at = normalized.get("ended_at")
            if ended_at is not None and ended_at < start:
                if not page_has_month:
                    page_all_before_month = True
                else:
                    page_all_before_month = False
                break
            if ended_at is not None and start <= ended_at < end:
                collected.append(normalized)
                page_has_month = True
            if ended_at is None or ended_at >= start:
                page_all_before_month = False
        if page_has_month and len(games) < VERSE_QUERY_PAGE_SIZE:
            break
        if page_all_before_month:
            break
    collected.sort(
        key=lambda item: (
            item.get("ended_at") or datetime.min.replace(tzinfo=LOCAL_TIMEZONE),
            str(item.get("id")),
        ),
        reverse=True,
    )
    _heavy_query_cache_set(cache_key, collected)
    return collected


def _calc_month_rating(games, variant_code):
    if variant_code not in {"4x4", "3x4"}:
        return None
    now = datetime.now(LOCAL_TIMEZONE)
    start, end = _month_range_of_previous_month(now)
    board_sums = []
    for game in games:
        ended_at = game.get("ended_at")
        if ended_at is None:
            continue
        if not (start <= ended_at < end):
            continue
        if game.get("board_sum") is None:
            continue
        board_sums.append(int(game["board_sum"]))
    board_sums.sort(reverse=True)
    top = board_sums[:5]
    zero_fill = max(0, 5 - len(top))
    avg = (sum(top) + 0 * zero_fill) / 5.0
    if avg <= 0:
        return {
            "value": 0.0,
            "top_count": len(top),
            "zero_fill_count": zero_fill,
            "avg_board_sum": 0.0,
            "month_start": start,
            "month_end": end,
        }
    if variant_code == "4x4":
        value = 562.5 * math.log2(avg) - 6000.0
    else:
        value = 562.5 * math.log2(avg) - 4280.0
    return {
        "value": value,
        "top_count": len(top),
        "zero_fill_count": zero_fill,
        "avg_board_sum": avg,
        "month_start": start,
        "month_end": end,
    }


def _month_game_count_label(month_rating):
    if month_rating is None:
        return "0/5"
    return "{}/5".format(month_rating.get("top_count", 0))


def _month_rating_label(month_rating):
    if month_rating is None:
        return "上个月月rating"
    month_start = month_rating.get("month_start")
    if isinstance(month_start, datetime):
        return month_start.strftime("%Y-%m月rating")
    return "上个月月rating"


def _pb_score(games):
    values = [int(game["score"]) for game in games if game.get("score") is not None]
    if not values:
        return None
    return max(values)


def _load_live_pb(username, variant_code):
    payload = _fetch_user_page(username, variant_code, 1, sort="score", desc=True)
    if not isinstance(payload, dict):
        return None
    games = payload.get("games")
    if not isinstance(games, list) or not games:
        return _safe_int(payload.get("hs"))
    best = None
    for game in games:
        score = _safe_int((game or {}).get("score"))
        if score is None:
            continue
        if best is None or score > best:
            best = score
    return best if best is not None else _safe_int(payload.get("hs"))


def _avg_recent_pb20_games(games, pb_value=None):
    pb = pb_value if pb_value not in (None, "", 0) else _pb_score(games)
    if pb is None or pb <= 0:
        return None
    threshold = pb * 0.2
    if pb_value not in (None, "", 0) and len(games) <= 5:
        top = [item for item in games if item.get("score") is not None and float(item["score"]) >= threshold]
    else:
        filtered = [item for item in games if item.get("score") is not None and float(item["score"]) >= threshold]
        filtered.sort(key=lambda item: ((item.get("ended_at") or datetime.min.replace(tzinfo=LOCAL_TIMEZONE)), item["id"]), reverse=True)
        top = filtered[:5]
    if not top:
        return None
    avg_score = sum(int(item["score"]) for item in top) / float(len(top))
    return {"pb": pb, "threshold": threshold, "count": len(top), "avg_score": avg_score}


def _load_recent_pb20_games(username, variant_code, pb_value):
    cache_key = _heavy_query_cache_key("recent_pb20", username, variant_code, pb_value)
    cached = _heavy_query_cache_get(cache_key)
    if cached is not None:
        return cached
    if pb_value is None or pb_value <= 0:
        return []
    threshold = pb_value * 0.2
    collected = []
    seen_ids = set()
    started = perf_counter()
    max_pages = int(VERSE_QUERY_MAX_PAGES_LIGHT)
    for page in range(1, max_pages + 1):
        if perf_counter() - started > float(VERSE_QUERY_FETCH_BUDGET_SECONDS):
            break
        payload = _fetch_user_page(username, variant_code, page, sort="date", desc=True)
        if not isinstance(payload, dict):
            stale = _heavy_query_cache_get_stale(cache_key)
            return stale if stale is not None else []
        games = payload.get("games")
        if not isinstance(games, list) or not games:
            break
        for index, game in enumerate(games):
            normalized = _normalize_live_game(game, fallback_id="r{}-{}".format(page, index))
            game_id = str(normalized["id"])
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)
            score = normalized.get("score")
            if score is None or float(score) < threshold:
                continue
            collected.append(normalized)
            if len(collected) >= 5:
                break
        if len(collected) >= 5 or len(games) < VERSE_QUERY_PAGE_SIZE:
            break
    collected.sort(
        key=lambda item: (
            item.get("ended_at") or datetime.min.replace(tzinfo=LOCAL_TIMEZONE),
            str(item.get("id")),
        ),
        reverse=True,
    )
    result = collected[:5]
    _heavy_query_cache_set(cache_key, result)
    return result


def _count_games_with_predicate(games, predicate):
    count = 0
    for game in games:
        try:
            if predicate(game):
                count += 1
        except Exception:
            continue
    return count


def _count_games_with_max_tile_at_least(games, threshold):
    wanted = int(threshold)
    return _count_games_with_predicate(
        games,
        lambda game: game.get("max_tile") is not None and int(game["max_tile"]) >= wanted,
    )


def _inferential_stat(label, value_text):
    return "{} {}".format(label, value_text)


def _has_tile(game, tile):
    values = game.get("board_values") or []
    return int(tile) in values


def _has_tiles(game, tiles):
    values = set(int(value) for value in (game.get("board_values") or []))
    return all(int(tile) in values for tile in tiles)


def _tile_exponent(tile):
    value = int(tile)
    if value <= 0 or (value & (value - 1)) != 0:
        return None
    return value.bit_length() - 1


def _game_covers_tiles(game, tiles):
    counts = {}
    for value in (game.get("board_values") or []):
        exp = _tile_exponent(value)
        if exp is None:
            continue
        counts[exp] = counts.get(exp, 0) + 1

    targets = sorted((_tile_exponent(tile) for tile in tiles), reverse=True)
    if any(exp is None for exp in targets):
        return False

    for target_exp in targets:
        source_exp = None
        for exp in sorted(counts.keys(), reverse=True):
            if exp >= target_exp and counts.get(exp, 0) > 0:
                source_exp = exp
                break
        if source_exp is None:
            return False
        counts[source_exp] -= 1
        if counts[source_exp] <= 0:
            counts.pop(source_exp, None)
        for exp in range(target_exp, source_exp):
            counts[exp] = counts.get(exp, 0) + 1
    return True


def _is_32k_plus_game(game):
    max_tile = game.get("max_tile")
    if max_tile is not None and int(max_tile) >= 32768:
        return True
    return _has_tile(game, 32768)


def _calc_32k_ratio_metrics(games):
    count_32k_plus = 0
    numerator = 0
    denominator = 0
    required_chain = (32768, 16384, 8192, 4096, 2048, 1024)
    for game in games:
        counts = {}
        for value in (game.get("board_values") or []):
            exp = _tile_exponent(value)
            if exp is None:
                continue
            counts[exp] = counts.get(exp, 0) + 1
        chain_length = 0
        for tile in required_chain:
            exp = _tile_exponent(tile)
            if exp is None or counts.get(exp, 0) <= 0:
                break
            chain_length += 1
        if chain_length >= 1:
            count_32k_plus += 1
            denominator += 1
        if chain_length >= 2:
            numerator += 1
    ratio = None if denominator <= 0 else (float(numerator) / float(denominator))
    eligible = count_32k_plus >= 10 and ratio is not None
    return {
        "count_32k_plus": count_32k_plus,
        "numerator": numerator,
        "denominator": denominator,
        "ratio": ratio,
        "eligible": eligible,
    }


def _count_full_board_level(game, variant_code):
    thresholds = {
        "2x4": [510, 766, 894, 958],
        "3x3": [1022, 1534, 1790, 1918, 1982],
    }.get((variant_code or "").lower())
    if not thresholds:
        return 0
    total_sum = _safe_int(game.get("board_sum"))
    if total_sum is None:
        values = [int(value) for value in (game.get("board_values") or []) if value]
        if not values:
            return 0
        total_sum = sum(values)
    level = 0
    for threshold in thresholds:
        if total_sum >= int(threshold):
            level += 1
    return level


FULL_BOARD_SCORE_BASELINES = {
    ("2x4", 1): 3076,
    ("2x4", 2): 5380,
    ("2x4", 3): 6404,
    ("2x4", 4): 6852,
    ("3x3", 1): 7172,
    ("3x3", 2): 12292,
    ("3x3", 3): 14596,
    ("3x3", 4): 15620,
    ("3x3", 5): 16068,
}

FULL_BOARD_SCORE_MOVE_COUNTS = {
    ("2x4", 1): 255,
    ("2x4", 2): 383,
    ("2x4", 3): 447,
    ("2x4", 4): 479,
    ("3x3", 1): 511,
    ("3x3", 2): 767,
    ("3x3", 3): 895,
    ("3x3", 4): 959,
    ("3x3", 5): 991,
}

# 满盘概率窗口采用双侧正态近似：
# 24/33 各阶对应的每侧尾部概率与窗口覆盖率如下：
# - 24 一阶：1e-6 -> 99.9998%
# - 24 二阶：1e-5 -> 99.998%
# - 24 三阶：1e-4 -> 99.98%
# - 24 四阶：1e-3 -> 99.8%
# - 33 一阶：1e-6 -> 99.9998%
# - 33 二阶：1e-5 -> 99.998%
# - 33 三阶：1e-4 -> 99.98%
# - 33 四阶：1e-3 -> 99.8%
# - 33 五阶：1e-2 -> 98%
FULL_BOARD_SCORE_TAILS = {
    ("2x4", 1): 1e-6,
    ("2x4", 2): 1e-5,
    ("2x4", 3): 1e-4,
    ("2x4", 4): 1e-3,
    ("3x3", 1): 1e-6,
    ("3x3", 2): 1e-5,
    ("3x3", 3): 1e-4,
    ("3x3", 4): 1e-3,
    ("3x3", 5): 1e-2,
}


def _full_board_score_window(variant_code, level):
    key = ((variant_code or "").lower(), int(level))
    baseline = FULL_BOARD_SCORE_BASELINES.get(key)
    move_count = FULL_BOARD_SCORE_MOVE_COUNTS.get(key)
    tail = FULL_BOARD_SCORE_TAILS.get(key)
    if baseline is None or move_count is None or tail is None:
        return None
    mean_score = float(baseline) - (0.4 * float(move_count))
    sigma_score = 1.2 * math.sqrt(float(move_count))
    z_score = NormalDist().inv_cdf(1.0 - float(tail))
    floor_score = int(math.floor(mean_score - (z_score * sigma_score)))
    ceiling_score = int(math.ceil(mean_score + (z_score * sigma_score)))
    return floor_score, ceiling_score


def _load_score_focused_games(
    username,
    variant_code,
    *,
    predicate,
    min_required_tile,
    cache_key_hint=None,
    full_scan=False,
    score_floor=None,
    score_ceiling=None,
    allow_stale_cache=True,
):
    cache_key = _heavy_query_cache_key(
        "score_focused",
        username,
        variant_code,
        cache_key_hint or min_required_tile,
        "full" if full_scan else "heuristic",
        "score{}".format(score_floor) if score_floor is not None else "-",
        "score{}".format(score_ceiling) if score_ceiling is not None else "-",
    )
    cached = _heavy_query_cache_get(cache_key)
    if cached is not None:
        return cached
    first_payload = _fetch_user_page(username, variant_code, 1, sort="score", desc=True)
    if not isinstance(first_payload, dict):
        return _heavy_query_cache_get_stale(cache_key)
    total_games = _safe_int(first_payload.get("totalGames"))
    total_pages = 1 if not total_games or total_games <= 0 else max(1, int(math.ceil(float(total_games) / float(VERSE_QUERY_PAGE_SIZE))))
    fetch_pages = min(total_pages, int(VERSE_QUERY_MAX_PAGES))
    collected = []
    seen_ids = set()
    empty_pages = 0
    started = perf_counter()
    complete = True

    for page in range(1, fetch_pages + 1):
        if perf_counter() - started > float(VERSE_QUERY_HEAVY_FETCH_BUDGET_SECONDS):
            complete = False
            break
        payload = first_payload if page == 1 else _fetch_user_page(username, variant_code, page, sort="score", desc=True)
        if not isinstance(payload, dict):
            if full_scan:
                complete = False
            break
        games = payload.get("games")
        if not isinstance(games, list):
            if full_scan:
                complete = False
            break
        if not games:
            if page == 1 and int(total_games or 0) <= 0:
                break
            if full_scan:
                complete = False
            break
        page_best_score = None
        page_worst_score = None
        if score_floor is not None or score_ceiling is not None:
            for game in games:
                if not isinstance(game, dict):
                    continue
                score_value = _safe_int(game.get("score"))
                if score_value is None:
                    continue
                page_best_score = max(int(score_value), page_best_score or int(score_value))
                page_worst_score = min(int(score_value), page_worst_score or int(score_value))
        if score_floor is not None and page_best_score is not None and int(page_best_score) < int(score_floor):
            break
        if score_ceiling is not None and page_worst_score is not None and int(page_worst_score) > int(score_ceiling):
            continue
        page_hits = 0
        page_best_tile = None
        for index, game in enumerate(games):
            normalized = _normalize_live_game(game, fallback_id="s{}-{}".format(page, index))
            game_id = str(normalized["id"])
            if game_id in seen_ids:
                continue
            score_value = _safe_int(normalized.get("score"))
            if score_floor is not None and score_value is not None and int(score_value) < int(score_floor):
                continue
            if score_ceiling is not None and score_value is not None and int(score_value) > int(score_ceiling):
                continue
            seen_ids.add(game_id)
            max_tile = normalized.get("max_tile")
            if max_tile is not None:
                page_best_tile = max(int(max_tile), page_best_tile or 0)
            if predicate(normalized):
                collected.append(normalized)
                page_hits += 1
        if not full_scan and score_floor is None and score_ceiling is None:
            if page_hits <= 0:
                empty_pages += 1
            else:
                empty_pages = 0
            if page_hits <= 0 and page_best_tile is not None and int(page_best_tile) < int(min_required_tile):
                if full_scan:
                    complete = False
                break
            if empty_pages >= int(VERSE_QUERY_SCORE_SCAN_EMPTY_PAGES):
                if full_scan:
                    complete = False
                break
        if len(games) < VERSE_QUERY_PAGE_SIZE:
            break

    collected.sort(
        key=lambda item: (
            item.get("score") if item.get("score") is not None else -1,
            item.get("ended_at") or datetime.min.replace(tzinfo=LOCAL_TIMEZONE),
            str(item.get("id")),
        ),
        reverse=True,
    )
    if collected and complete:
        _heavy_query_cache_set(cache_key, collected)
        return collected
    if collected:
        if allow_stale_cache:
            stale = _heavy_query_cache_get_stale(cache_key)
            if stale is not None:
                return stale
        return None
    if complete:
        _heavy_query_cache_set(cache_key, [])
        return []
    if not allow_stale_cache:
        return None
    return _heavy_query_cache_get_stale(cache_key)


def _summary_rating_line(rating_snapshot):
    if rating_snapshot is None:
        return "rating - | 排名 -"
    if rating_snapshot.get("source") == "verse_live":
        return "rating {} | 排名 #{}".format(
            _format_float(rating_snapshot.get("rating_value"), 1),
            rating_snapshot.get("rank_value"),
        )
    return "本地rating {} | 本地排名 #{}".format(
        _format_float(rating_snapshot.get("rating_value"), 1),
        rating_snapshot.get("rank_value"),
    )


def _mode_variant_from_token(token):
    lowered = token.lower()
    if lowered in {"4x4", "3x4", "3x3", "2x4"}:
        return lowered
    mode = lowered[:2]
    return MODE_TOKEN_TO_VARIANT.get(mode)


def _forced_variant_from_token(token):
    lowered = str(token or "").strip().lower()
    if lowered in {
        "8ks",
        "16ks",
        "32ks",
        "65ks",
        "8/16",
        "16/32",
        "24/32",
        "28/32",
        "30/32",
        "31/32",
        "f512",
        "f256",
        "f128",
        "32k综率",
    }:
        return "4x4"
    if lowered in {"4ks", "2/4", "3/4"}:
        return "3x4"
    if lowered in {"24满盘", "512s", "768s"} or re.match(r"^24满盘\d+$", lowered):
        return "2x4"
    if lowered in {"33满盘", "1024s", "1536s"} or re.match(r"^33满盘\d+$", lowered):
        return "3x3"
    return None


def _score_focused_spec(token):
    lowered = str(token or "").strip().lower()
    four_x_four_score_floor = {
        "8ks": 87000,
        "16ks": 195000,
        "32ks": VERSE_QUERY_4X4_32K_RATIO_SCORE_FLOOR,
        "65ks": 831000,
        "8/16": 195000,
        "16/32": 650000,
        "24/32": 745000,
        "28/32": 797000,
        "30/32": 821000,
        "31/32": 831000,
        "f512": 831000,
        "f256": 831000,
        "f128": 831000,
    }
    if lowered == "8ks":
        return {
            "min_required_tile": 8192,
            "score_floor": four_x_four_score_floor["8ks"],
            "predicate": lambda game: (game.get("max_tile") or 0) >= 8192,
            "allow_stale_cache": False,
        }
    if lowered == "16ks":
        return {
            "min_required_tile": 16384,
            "score_floor": four_x_four_score_floor["16ks"],
            "predicate": lambda game: (game.get("max_tile") or 0) >= 16384,
            "allow_stale_cache": False,
        }
    if lowered == "32ks":
        return {
            "min_required_tile": 32768,
            "score_floor": four_x_four_score_floor["32ks"],
            "predicate": lambda game: (game.get("max_tile") or 0) >= 32768,
            "allow_stale_cache": False,
        }
    if lowered == "65ks":
        return {
            "min_required_tile": 65536,
            "score_floor": four_x_four_score_floor["65ks"],
            "predicate": lambda game: (game.get("max_tile") or 0) >= 65536,
            "allow_stale_cache": False,
        }
    if lowered == "4ks":
        return {
            "min_required_tile": 4096,
            "score_floor": 37000,
            "predicate": lambda game: (game.get("max_tile") or 0) >= 4096,
            "allow_stale_cache": False,
        }
    if lowered == "8/16":
        return {
            "min_required_tile": 16384,
            "score_floor": four_x_four_score_floor["8/16"],
            "predicate": lambda game: _game_covers_tiles(game, (16384, 8192)),
            "allow_stale_cache": False,
        }
    if lowered == "16/32":
        return {
            "min_required_tile": 32768,
            "score_floor": four_x_four_score_floor["16/32"],
            "predicate": lambda game: _game_covers_tiles(game, (32768, 16384)),
            "allow_stale_cache": False,
        }
    if lowered == "24/32":
        return {
            "min_required_tile": 32768,
            "score_floor": four_x_four_score_floor["24/32"],
            "predicate": lambda game: _game_covers_tiles(game, (32768, 16384, 8192)),
            "allow_stale_cache": False,
        }
    if lowered == "28/32":
        return {
            "min_required_tile": 32768,
            "score_floor": four_x_four_score_floor["28/32"],
            "predicate": lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096)),
            "allow_stale_cache": False,
        }
    if lowered == "30/32":
        return {
            "min_required_tile": 32768,
            "score_floor": four_x_four_score_floor["30/32"],
            "predicate": lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048)),
            "allow_stale_cache": False,
        }
    if lowered == "31/32":
        return {
            "min_required_tile": 32768,
            "score_floor": four_x_four_score_floor["31/32"],
            "predicate": lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048, 1024)),
            "allow_stale_cache": False,
        }
    if lowered == "f512":
        return {
            "min_required_tile": 32768,
            "score_floor": four_x_four_score_floor["f512"],
            "predicate": lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048, 1024, 512)),
            "allow_stale_cache": False,
        }
    if lowered == "f256":
        return {
            "min_required_tile": 32768,
            "score_floor": four_x_four_score_floor["f256"],
            "predicate": lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048, 1024, 512, 256)),
            "allow_stale_cache": False,
        }
    if lowered == "f128":
        return {
            "min_required_tile": 32768,
            "score_floor": four_x_four_score_floor["f128"],
            "predicate": lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048, 1024, 512, 256, 128)),
            "allow_stale_cache": False,
        }
    if lowered == "2/4":
        return {
            "min_required_tile": 4096,
            "score_floor": 61000,
            "predicate": lambda game: _game_covers_tiles(game, (4096, 2048)),
            "allow_stale_cache": False,
        }
    if lowered == "3/4":
        return {
            "min_required_tile": 4096,
            "score_floor": 71500,
            "predicate": lambda game: _game_covers_tiles(game, (4096, 2048, 1024)),
            "allow_stale_cache": False,
        }
    if lowered == "512s":
        return {
            "min_required_tile": 512,
            "score_floor": 3200,
            "predicate": lambda game: _has_tile(game, 512),
            "allow_stale_cache": False,
        }
    if lowered == "768s":
        return {
            "min_required_tile": 512,
            "score_floor": 5300,
            "predicate": lambda game: _has_tiles(game, (512, 256)),
            "allow_stale_cache": False,
        }
    if lowered == "1024s":
        return {
            "min_required_tile": 1024,
            "score_floor": 7500,
            "predicate": lambda game: _has_tile(game, 1024),
            "allow_stale_cache": False,
        }
    if lowered == "1536s":
        return {
            "min_required_tile": 1024,
            "score_floor": 12200,
            "predicate": lambda game: _has_tiles(game, (1024, 512)),
            "allow_stale_cache": False,
        }
    if lowered == "24满盘":
        window = _full_board_score_window("2x4", 1)
        if window is None:
            return None
        score_floor, score_ceiling = window
        return {
            "min_required_tile": 512,
            "score_floor": score_floor,
            "score_ceiling": score_ceiling,
            "predicate": lambda game: _count_full_board_level(game, "2x4") >= 1,
            "allow_stale_cache": False,
        }
    if lowered == "33满盘":
        window = _full_board_score_window("3x3", 1)
        if window is None:
            return {"min_required_tile": 1024, "full_scan": True, "predicate": lambda game: _count_full_board_level(game, "3x3") >= 1}
        score_floor, score_ceiling = window
        return {
            "min_required_tile": 1024,
            "score_floor": score_floor,
            "score_ceiling": score_ceiling,
            "predicate": lambda game: _count_full_board_level(game, "3x3") >= 1,
            "allow_stale_cache": False,
        }
    matched = re.match(r"^(24|33)满盘(\d+)$", lowered)
    if matched:
        mode_token, level_text = matched.groups()
        variant = MODE_TOKEN_TO_VARIANT[mode_token]
        wanted_level = int(level_text)
        window = _full_board_score_window(variant, wanted_level)
        if window is None:
            min_required_tile = 512 if variant == "2x4" else 1024
            return {
                "min_required_tile": min_required_tile,
                "full_scan": True,
                "predicate": lambda game, variant=variant, wanted_level=wanted_level: _count_full_board_level(game, variant) >= wanted_level,
                "allow_stale_cache": False,
            }
        score_floor, score_ceiling = window
        return {
            "min_required_tile": 512 if variant == "2x4" else 1024,
            "score_floor": score_floor,
            "score_ceiling": score_ceiling,
            "predicate": lambda game, variant=variant, wanted_level=wanted_level: _count_full_board_level(game, variant) >= wanted_level,
            "allow_stale_cache": False,
        }
    if lowered == "32k综率":
        return {
            "min_required_tile": 32768,
            "score_floor": VERSE_QUERY_4X4_32K_RATIO_SCORE_FLOOR,
            "predicate": _is_32k_plus_game,
            "allow_stale_cache": False,
        }
    return None


def _score_focused_query_disallows_local_fallback(token):
    lowered = str(token or "").strip().lower()
    return lowered.startswith(("24满盘", "33满盘", "24/32", "32k综率"))


def _token_page_budget(token):
    text = str(token or "").strip().lower()
    if text.endswith(("ra", "pb", "mra", "r5")):
        return VERSE_QUERY_MAX_PAGES_LIGHT
    if text in {"4x4", "3x4", "3x3", "2x4"}:
        return VERSE_QUERY_MAX_PAGES_MEDIUM
    return VERSE_QUERY_MAX_PAGES


def _render_mode_summary(player, variant_code, rating_snapshot, games):
    mode_label = VARIANT_TO_MODE_LABEL.get(variant_code, variant_code)
    pb = _pb_score(games)
    month_rating = _calc_month_rating(games, variant_code)
    lines = [
        "模式: {}".format(mode_label),
        _summary_rating_line(rating_snapshot),
        "PB {}".format(pb if pb is not None else "-"),
    ]
    if variant_code == "4x4":
        ratio_metrics = _calc_32k_ratio_metrics(games)
        count_32k = ratio_metrics["count_32k_plus"]
        if count_32k > 0:
            lines.append("32k数量 {}".format(count_32k))
        if ratio_metrics["eligible"]:
            lines.append("32k综率 {:.4%}".format(ratio_metrics["ratio"]))
        lines.append(
            "{} {}".format(
                _month_rating_label(month_rating),
                "-"
                if month_rating is None
                else "{}（月局数：{}）".format(
                    _format_float(month_rating["value"], 1),
                    _month_game_count_label(month_rating),
                )
            )
        )
    elif variant_code == "3x4":
        count_4k = _count_games_with_max_tile_at_least(games, 4096)
        lines.append("4k数量 {}".format(count_4k))
    elif variant_code == "3x3":
        lines.append("1024数量 {}".format(_count_games_with_predicate(games, lambda game: _has_tile(game, 1024))))
        full_board_count = _count_games_with_predicate(games, lambda game: _count_full_board_level(game, "3x3") >= 1)
        if full_board_count > 0:
            lines.append("满盘数量 {}".format(full_board_count))
    elif variant_code == "2x4":
        lines.append("512数量 {}".format(_count_games_with_predicate(games, lambda game: _has_tile(game, 512))))
        full_board_count = _count_games_with_predicate(games, lambda game: _count_full_board_level(game, "2x4") >= 1)
        if full_board_count > 0:
            lines.append("满盘数量 {}".format(full_board_count))
    return "\n".join(lines)


def _variant_not_supported_message():
    return "42/43 模式查询当前不支持。可用模式：44、34、33、24。"


def _wr_reply(variant_code):
    rows = _load_live_leaderboard_rows(variant_code, time_key="all", page=1)
    mode_label = VARIANT_TO_MODE_LABEL.get(variant_code, variant_code)
    if not rows:
        return "{} WR 暂无数据".format(mode_label)
    row = rows[0]
    username = str(row.get("username") or "-").strip() or "-"
    pb = _safe_int(row.get("score"))
    rating_value = _leaderboard_row_rating(row)
    if rating_value is None:
        rating_snapshot = _load_live_rating_snapshot(username, variant_code)
        rating_value = None if rating_snapshot is None else rating_snapshot.get("rating_value")
    return "{} WR | 用户名 {} | PB {} | rating {}".format(
        mode_label,
        username,
        pb if pb is not None else "-",
        _format_float(rating_value, 1) if rating_value is not None else "-",
    )


def _rawr_reply(variant_code):
    rows = _load_live_leaderboard_rows(variant_code, time_key="all", page=1)
    mode_label = VARIANT_TO_MODE_LABEL.get(variant_code, variant_code)
    if not rows:
        return "{} raWR 暂无数据".format(mode_label)

    candidates = []
    for row in rows[: int(VERSE_QUERY_RAWR_SCAN_LIMIT)]:
        username = str(row.get("username") or "").strip()
        if not username:
            continue
        rating_value = _leaderboard_row_rating(row)
        if rating_value is None:
            rating_snapshot = _load_live_rating_snapshot(username, variant_code)
            rating_value = None if rating_snapshot is None else rating_snapshot.get("rating_value")
        if rating_value is None:
            continue
        candidates.append(
            {
                "username": username,
                "pb": _safe_int(row.get("score")),
                "rating_value": float(rating_value),
            }
        )

    if not candidates:
        return "{} raWR 暂无数据".format(mode_label)

    best = max(candidates, key=lambda item: (item["rating_value"], item["pb"] if item["pb"] is not None else -1, item["username"]))
    return "{} raWR | 用户名 {} | PB {} | rating {}".format(
        mode_label,
        best["username"],
        best["pb"] if best["pb"] is not None else "-",
        _format_float(best["rating_value"], 1),
    )


def _render_token_query(player, token, variant_code, rating_snapshot, games):
    token = token.lower()
    mode_label = VARIANT_TO_MODE_LABEL.get(variant_code, variant_code)
    if token == "wr":
        if variant_code is None:
            return _variant_not_supported_message()
        return _wr_reply(variant_code)
    if token == "rawr":
        if variant_code is None:
            return _variant_not_supported_message()
        return _rawr_reply(variant_code)
    if token.endswith("mra"):
        if variant_code not in {"4x4", "3x4"}:
            return "{} 月rating仅支持 4x4/3x4。".format(mode_label)
        month_rating = _calc_month_rating(games, variant_code)
        if month_rating is None:
            return "{} {}暂无数据".format(mode_label, _month_rating_label(month_rating))
        return "{} {} {}（月局数：{}）".format(
            mode_label,
            _month_rating_label(month_rating),
            _format_float(month_rating["value"], 1),
            _month_game_count_label(month_rating),
        )
    if token.endswith("ra"):
        if variant_code is None:
            return _variant_not_supported_message()
        if rating_snapshot is None:
            return "{} rating 暂无数据".format(mode_label)
        if rating_snapshot.get("source") == "verse_live":
            return "{} rating {} | 排名 #{}".format(
                mode_label,
                _format_float(rating_snapshot.get("rating_value"), 1),
                rating_snapshot.get("rank_value") if rating_snapshot.get("rank_value") is not None else "-",
            )
        return "{} 本地rating {} | 本地排名 #{} | RD {} | 参赛 {}".format(
            mode_label,
            _format_float(rating_snapshot.get("rating_value"), 1),
            rating_snapshot.get("rank_value"),
            _format_float(rating_snapshot.get("rating_deviation"), 1),
            rating_snapshot.get("event_count"),
        )
    if token.endswith("pb"):
        pb = _pb_score(games)
        return "{} PB {}".format(mode_label, pb if pb is not None else "-")
    if token.endswith("r5"):
        if variant_code not in {"4x4", "3x4"}:
            return "{} r5 仅支持 4x4/3x4。".format(mode_label)
        payload = _avg_recent_pb20_games(
            games,
            None if rating_snapshot is None else _safe_int(rating_snapshot.get("best_score")),
        )
        if payload is None:
            return "{} r5 暂无可用数据".format(mode_label)
        return "{} r5 {:.1f} | 阈值 {:.1f} (PB={} 的20%) | 样本 {}".format(
            mode_label,
            payload["avg_score"],
            payload["threshold"],
            payload["pb"],
            payload["count"],
        )

    if token == "8ks":
        return "4x4 8k数量 {}".format(_count_games_with_max_tile_at_least(games, 8192))
    if token == "16ks":
        return "4x4 16k数量 {}".format(_count_games_with_max_tile_at_least(games, 16384))
    if token == "32ks":
        return "4x4 32k数量 {}".format(_count_games_with_max_tile_at_least(games, 32768))
    if token == "65ks":
        return "4x4 65k数量 {}".format(_count_games_with_max_tile_at_least(games, 65536))
    if token == "8/16":
        return _inferential_stat(
            "4x4 8/16数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (16384, 8192))),
        )
    if token == "16/32":
        return _inferential_stat(
            "4x4 16/32数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (32768, 16384))),
        )
    if token == "24/32":
        return _inferential_stat(
            "4x4 24/32数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (32768, 16384, 8192))),
        )
    if token == "28/32":
        return _inferential_stat(
            "4x4 28/32数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096))),
        )
    if token == "30/32":
        return _inferential_stat(
            "4x4 30/32数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048))),
        )
    if token == "31/32":
        return _inferential_stat(
            "4x4 31/32数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048, 1024))),
        )
    if token == "f512":
        return _inferential_stat(
            "4x4 65k final512数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048, 1024, 512))),
        )
    if token == "f256":
        return _inferential_stat(
            "4x4 65k final256数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048, 1024, 512, 256))),
        )
    if token == "f128":
        return _inferential_stat(
            "4x4 65k final128数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (32768, 16384, 8192, 4096, 2048, 1024, 512, 256, 128))),
        )

    if token == "4ks":
        return "3x4 4k数量 {}".format(_count_games_with_max_tile_at_least(games, 4096))
    if token == "2/4":
        return _inferential_stat(
            "3x4 2/4数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (4096, 2048))),
        )
    if token == "3/4":
        return _inferential_stat(
            "3x4 3/4数量",
            _count_games_with_predicate(games, lambda game: _game_covers_tiles(game, (4096, 2048, 1024))),
        )

    if token == "24满盘":
        return "2x4 一阶满盘数量 {}".format(_count_games_with_predicate(games, lambda game: _count_full_board_level(game, "2x4") >= 1))
    if token == "33满盘":
        return "3x3 一阶满盘数量 {}".format(_count_games_with_predicate(games, lambda game: _count_full_board_level(game, "3x3") >= 1))
    if token in {"24满盘x", "33满盘x"}:
        return "请把 x 替换成阶数数字，例如：24满盘2、33满盘3。"

    matched = re.match(r"^(24|33)满盘(\d+)$", token)
    if matched:
        mode_token, level_text = matched.groups()
        wanted_level = int(level_text)
        variant = MODE_TOKEN_TO_VARIANT[mode_token]
        return "{} {}阶满盘数量 {}".format(
            variant,
            wanted_level,
            _count_games_with_predicate(games, lambda game: _count_full_board_level(game, variant) >= wanted_level),
        )

    if token == "512s":
        return "2x4 512数量 {}".format(_count_games_with_predicate(games, lambda game: _has_tile(game, 512)))
    if token == "768s":
        return "2x4 768数量 {}".format(_count_games_with_predicate(games, lambda game: _has_tiles(game, (512, 256))))
    if token == "1024s":
        return "3x3 1024数量 {}".format(_count_games_with_predicate(games, lambda game: _has_tile(game, 1024)))
    if token == "1536s":
        return "3x3 1536数量 {}".format(_count_games_with_predicate(games, lambda game: _has_tiles(game, (1024, 512))))

    if token == "32k综率":
        metrics = _calc_32k_ratio_metrics(games)
        if metrics["count_32k_plus"] < 10:
            return "4x4 32k综率暂无（需至少10局32k及以上，当前{}局）".format(metrics["count_32k_plus"])
        if metrics["ratio"] is None:
            return "4x4 32k综率暂无（分母为0）"
        return "4x4 32k综率 {:.4%}".format(metrics["ratio"])

    if token in {"4x4", "3x4", "3x3", "2x4"}:
        return _render_mode_summary(player, token, rating_snapshot, games)

    return "未识别的查询指令：{}".format(token)


def handle_verse_query_message(connection, *, bot_platform, bot_user_id, text):
    parsed = parse_verse_query_message(text)
    if parsed is None:
        return None
    token = parsed["token"]
    if token.startswith(("42", "43")):
        return _variant_not_supported_message()
    variant_code = _forced_variant_from_token(token) or _mode_variant_from_token(token)
    cache_key = _verse_reply_cache_key(bot_platform, bot_user_id, parsed, variant_code)
    allow_local_fallback = parsed.get("username") is None

    def _build_reply():
        if token in {"wr", "rawr"}:
            resolved_variant_code = parsed.get("variant_override")
            if resolved_variant_code is None:
                return _variant_not_supported_message()
            return _render_token_query(None, token, resolved_variant_code, None, [])

        try:
            target_player = _resolve_target_player(
                connection,
                bot_platform=bot_platform,
                bot_user_id=bot_user_id,
                explicit_username=parsed["username"],
                variant_code=variant_code,
            )
        except ValueError as exc:
            return str(exc)

        lookup_variant = variant_code or "4x4"
        games = []
        rating_snapshot = None
        spec = _score_focused_spec(token)

        if token.endswith("ra"):
            rating_snapshot = _load_rating_snapshot(
                connection,
                target_player["player_id"],
                target_player["username"],
                lookup_variant,
                allow_local_fallback=allow_local_fallback,
            )
            body = _render_token_query(target_player, token, lookup_variant, rating_snapshot, [])
            player_label = target_player.get("username") or target_player.get("display_name") or "-"
            return "{}\n{}".format("玩家: {}".format(player_label), body)

        if token.endswith("pb"):
            pb = None
            if pb is None:
                pb = _load_live_pb(target_player["username"], lookup_variant)
            if pb is None and allow_local_fallback and target_player.get("player_id") is not None:
                pb = _pb_score(_load_local_games(connection, target_player["player_id"], lookup_variant))
            body = "{} PB {}".format(VARIANT_TO_MODE_LABEL.get(lookup_variant, lookup_variant), pb if pb is not None else "-")
            player_label = target_player.get("username") or target_player.get("display_name") or "-"
            return "{}\n{}".format("玩家: {}".format(player_label), body)

        if token.endswith("mra"):
            games = _load_month_games(target_player["username"], lookup_variant)
            if not games and allow_local_fallback and target_player.get("player_id") is not None:
                games = _load_local_games(connection, target_player["player_id"], lookup_variant)
        elif token.endswith("r5"):
            pb_value = None
            if pb_value is None:
                pb_value = _load_live_pb(target_player["username"], lookup_variant)
            if pb_value is None:
                rating_snapshot = _load_rating_snapshot(
                    connection,
                    target_player["player_id"],
                    target_player["username"],
                    lookup_variant,
                    allow_local_fallback=allow_local_fallback,
                )
                if rating_snapshot is not None:
                    pb_value = _safe_int(rating_snapshot.get("best_score"))
            games = _load_recent_pb20_games(target_player["username"], lookup_variant, pb_value)
            if not games and allow_local_fallback and target_player.get("player_id") is not None:
                games = _load_local_games(connection, target_player["player_id"], lookup_variant)
            if rating_snapshot is None and pb_value is not None:
                rating_snapshot = {"best_score": pb_value}
        elif spec is not None:
            games = _load_score_focused_games(
                target_player["username"],
                lookup_variant,
                predicate=spec["predicate"],
                min_required_tile=spec["min_required_tile"],
                cache_key_hint=token,
                full_scan=bool(spec.get("full_scan")),
                score_floor=spec.get("score_floor"),
                score_ceiling=spec.get("score_ceiling"),
                allow_stale_cache=bool(spec.get("allow_stale_cache", True)),
            )
            if (
                games is None
                and allow_local_fallback
                and target_player.get("player_id") is not None
                and not _score_focused_query_disallows_local_fallback(token)
            ):
                games = _load_local_games(connection, target_player["player_id"], lookup_variant)
            if games is None:
                mode_label = VARIANT_TO_MODE_LABEL.get(lookup_variant, lookup_variant)
                return "玩家: {}\n{} 数据暂不可用，请稍后再试。".format(
                    target_player.get("username") or target_player.get("display_name") or "-",
                    mode_label,
                )
        else:
            rating_snapshot = _load_rating_snapshot(
                connection,
                target_player["player_id"],
                target_player["username"],
                lookup_variant,
                allow_local_fallback=allow_local_fallback,
            )
            games = _load_games(
                connection,
                target_player["player_id"],
                target_player["username"],
                lookup_variant,
                profile_snapshot=rating_snapshot,
                allow_local_fallback=allow_local_fallback,
            )
            if games is None:
                mode_label = VARIANT_TO_MODE_LABEL.get(lookup_variant, lookup_variant)
                return "玩家: {}\n{} 历史数据较多，本次未完整获取，请稍后再试。".format(
                    target_player.get("username") or target_player.get("display_name") or "-",
                    mode_label,
                )

        body = _render_token_query(target_player, token, variant_code, rating_snapshot, games)
        player_label = target_player.get("username") or target_player.get("display_name") or "-"
        return "{}\n{}".format("玩家: {}".format(player_label), body)

    cached_reply = _verse_reply_cache_get(cache_key)
    if cached_reply is not None:
        return cached_reply
    return _verse_reply_run_once(cache_key, _build_reply)
