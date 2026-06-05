import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter

from services.attempt_service import start_attempt_session
from services.bot_admin_service import is_bot_admin, write_audit_log
from services.bot_attachment_service import (
    describe_segment_file,
    extract_file_segments,
    materialize_segment_file,
)
from services.bot_binding_service import bind_bot_account, deactivate_bot_binding, find_active_bot_binding, get_bot_binding
from services.bot_dashboard_service import (
    clear_player_dashboard,
    format_dashboard_reply,
    get_player_dashboard,
    resolve_dashboard_target,
    set_player_dashboard,
)
from services.competition_mode_service import event_uses_locking
from services.discord_lock_refresh_service import (
    refresh_locking_event_scores_from_discord,
    refresh_locking_player_scores_from_discord,
    sync_locking_scores_from_discord,
)
from services.pending_score_review_service import (
    approve_pending_score,
    list_pending_scores,
    reject_pending_score,
    submit_pending_score,
)
from services.player_profile_service import build_player_profile, format_player_profile
from services.raw_score_import_service import add_manual_score
from services.registration_service import cancel_registration, is_registered, list_player_registrations, register_player
from services.replay_lock_service import (
    attach_early_replay,
    attach_final_replay,
    get_replay_chain_status,
    list_replay_chain_issues,
    run_replay_prefix_check,
    set_replay_review_status,
)
from services.timed_reservation_service import (
    cancel_my_reservation,
    get_my_reservation,
    get_my_reservation_score,
    reserve_for_player,
    settle_due_reservations,
)
from services.verse_query_service import handle_verse_query_message, is_verse_query_message
from services.settlement_service import settle_event
from settings import LOCAL_TIMEZONE


PENDING_FLOWS = {}
FLOW_TIMEOUT_SECONDS = 600
COMPETITION_TYPE_LABELS = {
    "classic_raw_score": "原始分",
    "points_series_3x4": "积分赛",
    "timed_scoring": "限时赛",
    "stone_x": "Stone",
    "no_x": "No-X",
    "speedrun": "竞速",
    "fibonacci_raw_score": "斐波那契",
}
RECENT_EVENT_VISIBLE_DAYS = 7
HELP_IMAGE_DIR = Path(__file__).resolve().parents[1] / "data" / "bot_help_images"
MY_SCORE_PAGE_SIZE = 10
LOGGER = logging.getLogger(__name__)
GROUP_DEBUG_LOG_PATH = HELP_IMAGE_DIR.parent / "group_debug.log"


def _env_flag(name, default=False):
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_positive_int(name, default_value):
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default_value
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return value if value > 0 else default_value


def _append_group_debug(message):
    timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    try:
        GROUP_DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with GROUP_DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write("[{}] {}\n".format(timestamp, message))
    except Exception:
        pass


def _profile_sync_step(steps, name, fn):
    started = perf_counter()
    result = fn()
    elapsed_ms = int((perf_counter() - started) * 1000)
    steps.append((name, elapsed_ms))
    return result


# 默认不在“我的成绩”中触发 Discord 同步，避免网络请求导致回复长时间阻塞。
MY_SCORE_LOCK_REFRESH_ON_QUERY = _env_flag("MY_SCORE_LOCK_REFRESH_ON_QUERY", False)
MY_SCORE_PLAYER_LOCK_REFRESH_ON_QUERY = _env_flag("MY_SCORE_PLAYER_LOCK_REFRESH_ON_QUERY", True)
MY_SCORE_SETTLE_ON_QUERY = _env_flag("MY_SCORE_SETTLE_ON_QUERY", True)
MY_SCORE_QUERY_LIVE_ONLY = _env_flag("MY_SCORE_QUERY_LIVE_ONLY", True)
MY_SCORE_PLAYER_REFRESH_COOLDOWN_SECONDS = _env_positive_int("MY_SCORE_PLAYER_REFRESH_COOLDOWN_SECONDS", 90)
MY_SCORE_EVENT_SETTLE_COOLDOWN_SECONDS = _env_positive_int("MY_SCORE_EVENT_SETTLE_COOLDOWN_SECONDS", 30)
RESERVATION_SETTLE_COOLDOWN_SECONDS = _env_positive_int("RESERVATION_SETTLE_COOLDOWN_SECONDS", 20)
MY_SCORE_SLOW_LOG_MS = _env_positive_int("MY_SCORE_SLOW_LOG_MS", 1500)
BOT_PRIVATE_SLOW_LOG_MS = _env_positive_int("BOT_PRIVATE_SLOW_LOG_MS", 1200)
MY_SCORE_PLAYER_REFRESH_TIMESTAMPS = {}
MY_SCORE_EVENT_SETTLE_TIMESTAMPS = {}
RESERVATION_SETTLE_TIMESTAMPS = {}
GROUP_CHAT_ENABLED = _env_flag("GROUP_CHAT_ENABLED", False)
GROUP_CHAT_RATE_LIMIT_PER_MINUTE = _env_positive_int("GROUP_CHAT_RATE_LIMIT_PER_MINUTE", 1)
GROUP_CHAT_GLOBAL_RATE_LIMIT_PER_MINUTE = _env_positive_int("GROUP_CHAT_GLOBAL_RATE_LIMIT_PER_MINUTE", 12)
GROUP_CHAT_MAX_CONCURRENT_REPLY_JOBS_PER_GROUP = _env_positive_int("GROUP_CHAT_MAX_CONCURRENT_REPLY_JOBS_PER_GROUP", 2)
GROUP_CHAT_MAX_REPLY_CHARS = _env_positive_int("GROUP_CHAT_MAX_REPLY_CHARS", 180)
ONEBOT_REPLY_LOOKUP_ENABLED = _env_flag("ONEBOT_REPLY_LOOKUP_ENABLED", False)
BOT_HELP_IMAGE_ENABLED = _env_flag("BOT_HELP_IMAGE_ENABLED", True)
BOT_LOCK_UPLOAD_ENABLED = _env_flag("BOT_LOCK_UPLOAD_ENABLED", True)
BOT_SUBMIT_SCORE_ENABLED = _env_flag("BOT_SUBMIT_SCORE_ENABLED", True)
GROUP_CHAT_WHITELIST = {
    item.strip()
    for item in str(os.getenv("GROUP_CHAT_WHITELIST", "")).split(",")
    if item.strip()
}
GROUP_RATE_LIMIT_STATE = {}
GROUP_GLOBAL_RATE_LIMIT_STATE = {}
GROUP_REPLY_JOB_STATE = {}
GROUP_REPLY_JOB_LOCK = threading.Lock()
_ONEBOT_REPLY_LOOKUP_SKIP_CHECK = None


def _message_handler_error_reply(exc, *, is_group):
    text = str(exc or "")
    lowered = text.lower()
    if "database is locked" in lowered or "database table is locked" in lowered:
        return None if is_group else "系统繁忙，请稍后再试。"
    if is_group:
        return None
    return "处理失败：{}".format(text)


def is_transport_unstable_error(exc):
    text = str(exc or "").lower()
    if "timeout" in text and ("sendmsg" in text or "call api" in text or "websocket" in text):
        return True
    if "websocket" in text and "closed by peer" in text:
        return True
    return False


def patch_onebot_reply_lookup(onebot_bot_module):
    if ONEBOT_REPLY_LOOKUP_ENABLED:
        return False
    checker = getattr(onebot_bot_module, "_check_reply", None)
    if checker is None:
        return False
    if getattr(checker, "__name__", "") == "_skip_onebot_reply_lookup":
        return False
    global _ONEBOT_REPLY_LOOKUP_SKIP_CHECK

    def _skip_onebot_reply_lookup(*args, **kwargs):
        return None

    _ONEBOT_REPLY_LOOKUP_SKIP_CHECK = _skip_onebot_reply_lookup
    onebot_bot_module._check_reply = _ONEBOT_REPLY_LOOKUP_SKIP_CHECK
    return True


def render_send_timeout_fallback_message(reply):
    text = str(reply or "").strip()
    if not text:
        return "发送失败，请稍后再试。"
    fallback_text = re.sub(r"\[CQ:image,file=[^\]]+\]", "", text)
    fallback_text = re.sub(r"\s+", " ", fallback_text).strip()
    if fallback_text:
        return fallback_text
    return "发送失败，请稍后再试。"


def _flow_key(bot_platform, bot_user_id, flow_scope=None):
    if flow_scope:
        return "{}:{}:{}".format(bot_platform, bot_user_id, flow_scope)
    return "{}:{}".format(bot_platform, bot_user_id)


def _get_flow(bot_platform, bot_user_id, flow_scope=None):
    return PENDING_FLOWS.get(_flow_key(bot_platform, bot_user_id, flow_scope=flow_scope))


def _set_flow(bot_platform, bot_user_id, payload, flow_scope=None):
    payload = dict(payload)
    payload["_updated_at"] = datetime.now(LOCAL_TIMEZONE).isoformat()
    PENDING_FLOWS[_flow_key(bot_platform, bot_user_id, flow_scope=flow_scope)] = payload


def _clear_flow(bot_platform, bot_user_id, flow_scope=None):
    PENDING_FLOWS.pop(_flow_key(bot_platform, bot_user_id, flow_scope=flow_scope), None)


def _flow_expired(flow):
    updated_at = flow.get("_updated_at")
    if not updated_at:
        return False
    try:
        updated = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=LOCAL_TIMEZONE)
    return (datetime.now(LOCAL_TIMEZONE) - updated).total_seconds() > FLOW_TIMEOUT_SECONDS


def _parse_kv_text(text):
    items = []
    current_key = None
    current_value_parts = []
    for token in text.strip().split():
        if "=" in token:
            key, value = token.split("=", 1)
            if key:
                if current_key is not None:
                    items.append((current_key, " ".join(current_value_parts).strip()))
                current_key = key.strip()
                current_value_parts = [value.strip()]
                continue
        if current_key is None:
            raise ValueError("Invalid token: {}".format(token))
        current_value_parts.append(token)
    if current_key is not None:
        items.append((current_key, " ".join(current_value_parts).strip()))
    return {key: value for key, value in items}


def _parse_int_or_none(value, field_name):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError("{} must be an integer".format(field_name))


def _normalize_time(value, field_name):
    if not value:
        return None
    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(str(value).strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError("{} format is invalid".format(field_name))


def _parse_json(value):
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_event_type_label(competition_type):
    return COMPETITION_TYPE_LABELS.get(competition_type, competition_type or "赛事")


def _row_get(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _event_note(row):
    metadata = _parse_json(_row_get(row, "metadata_json"))
    note = metadata.get("remark") or metadata.get("note") or metadata.get("notes")
    return str(note).strip() if note else None


def _format_event_time_range(row):
    start_dt = _parse_local_time(_row_get(row, "start_time"))
    end_dt = _parse_local_time(_row_get(row, "end_time"))
    if start_dt is None or end_dt is None:
        return "{} -> {}".format(_row_get(row, "start_time") or "-", _row_get(row, "end_time") or "-")
    now = datetime.now(LOCAL_TIMEZONE)
    cross_year = start_dt.year != end_dt.year
    show_year = cross_year or start_dt.year != now.year or end_dt.year != now.year
    fmt = "%Y-%m-%d %H:%M" if show_year else "%m-%d %H:%M"
    return "{} -> {}".format(start_dt.strftime(fmt), end_dt.strftime(fmt))


def _parse_local_time(value):
    if not value:
        return None
    text = str(value).strip()
    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
        "%Y-%m-%d %H",
        "%Y-%m-%dT%H",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ]
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=LOCAL_TIMEZONE)
        return parsed.astimezone(LOCAL_TIMEZONE)
    except ValueError:
        pass
    for fmt in candidates:
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
            return parsed.astimezone(LOCAL_TIMEZONE)
        except ValueError:
            continue
    return None


def _reservation_status_label(status, reserved_start_time=None, reserved_end_time=None):
    status_text = (status or "unreserved").lower()
    if status_text in {"unreserved", "cancelled"}:
        return "未预约"
    if status_text == "settled":
        return "已结算"
    if status_text in {"reserved", "confirmed_late"}:
        now = datetime.now(LOCAL_TIMEZONE)
        start_dt = _parse_local_time(reserved_start_time)
        end_dt = _parse_local_time(reserved_end_time)
        if start_dt is not None and end_dt is not None and start_dt <= now < end_dt:
            return "进行中"
        return "已预约"
    return status or "未预约"


def _runtime_status_label(row):
    status = (_row_get(row, "status") or "").lower()
    if status == "archived":
        return "已归档"
    now = datetime.now(LOCAL_TIMEZONE)
    start_time = _parse_local_time(_row_get(row, "start_time"))
    end_time = _parse_local_time(_row_get(row, "end_time"))
    if start_time and now < start_time:
        return "待开始"
    if end_time and now > end_time:
        return "已结束"
    if start_time or end_time:
        return "进行中"
    if status == "ready":
        return "待开始"
    return "未设时间"


def _is_history_hidden(row):
    if _runtime_status_label(row) != "已结束":
        return False
    end_time = _parse_local_time(_row_get(row, "end_time"))
    if end_time is None:
        return False
    return (datetime.now(LOCAL_TIMEZONE) - end_time) > timedelta(days=RECENT_EVENT_VISIBLE_DAYS)


def _format_event_line(row, *, include_registration_status=False, include_lock_flag=False):
    return "{} | {} | {}".format(
        _row_get(row, "event_name", "-"),
        _format_event_time_range(row),
        _runtime_status_label(row),
    )


def _as_numbered_lines(rows):
    lines = []
    for index, row in enumerate(rows, start=1):
        lines.append("{}. {}".format(index, _format_event_line(row)))
    return "\n".join(lines)


def _resolve_choice_index(message, count):
    text = (message or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    if number < 1 or number > count:
        return None
    return number - 1


def _is_admin_command(message):
    return (
        message == "管理员帮助"
        or message.startswith("待审核列表")
        or message.startswith("通过成绩")
        or message.startswith("驳回成绩")
        or message.startswith("锁局状态")
        or message.startswith("异常锁局列表")
        or message.startswith("刷新锁局")
        or message.startswith("解绑用户")
    )


def _is_top_level_command(message):
    text = (message or "").strip()
    if not text:
        return False
    if is_verse_query_message(text):
        return True
    exact = {
        "help",
        "帮助",
        "菜单",
        "管理员帮助",
        "绑定",
        "解绑",
        "取消绑定",
        "赛事",
        "赛事列表",
        "查看赛事",
        "归档比赛",
        "查看归档比赛",
        "报名",
        "取消报名",
        "我的报名",
        "我的成绩",
        "我的限时赛成绩",
        "预约",
        "预约限时赛",
        "我的限时预约",
        "修改限时预约",
        "取消限时预约",
        "提交成绩",
        "提交",
        "我的提交",
        "我的档案",
        "看板",
        "查看看板",
        "设置看板",
        "清空看板",
        "floor",
        "finish",
    }
    if text in exact:
        return True
    prefixes = ("绑定 ", "报名 ", "取消报名 ", "提交成绩 ", "提交 ", "我的提交 ")
    return any(text.startswith(prefix) for prefix in prefixes)


def _group_flow_scope(group_id):
    return "group:{}".format(group_id)


def _group_rate_limit_key(group_id, user_id):
    return "{}:{}".format(group_id, user_id)


def _group_global_rate_limit_key(group_id):
    return str(group_id)


def _prune_rate_limit_state(state, key, timestamps):
    if timestamps:
        state[key] = timestamps
    else:
        state.pop(key, None)


def _consume_group_rate_limit(group_id, user_id):
    now = datetime.now(LOCAL_TIMEZONE)
    window_start = now - timedelta(seconds=60)
    key = _group_rate_limit_key(group_id, user_id)
    timestamps = [item for item in GROUP_RATE_LIMIT_STATE.get(key, []) if item > window_start]
    allowed = len(timestamps) < GROUP_CHAT_RATE_LIMIT_PER_MINUTE
    if allowed:
        timestamps.append(now)
    _prune_rate_limit_state(GROUP_RATE_LIMIT_STATE, key, timestamps)
    return allowed


def _consume_group_global_rate_limit(group_id):
    now = datetime.now(LOCAL_TIMEZONE)
    window_start = now - timedelta(seconds=60)
    key = _group_global_rate_limit_key(group_id)
    timestamps = [item for item in GROUP_GLOBAL_RATE_LIMIT_STATE.get(key, []) if item > window_start]
    allowed = len(timestamps) < GROUP_CHAT_GLOBAL_RATE_LIMIT_PER_MINUTE
    if allowed:
        timestamps.append(now)
    _prune_rate_limit_state(GROUP_GLOBAL_RATE_LIMIT_STATE, key, timestamps)
    return allowed


def _acquire_group_reply_slot(group_id):
    key = str(group_id)
    with GROUP_REPLY_JOB_LOCK:
        active = int(GROUP_REPLY_JOB_STATE.get(key, 0) or 0)
        if active >= GROUP_CHAT_MAX_CONCURRENT_REPLY_JOBS_PER_GROUP:
            return False
        GROUP_REPLY_JOB_STATE[key] = active + 1
        return True


def _release_group_reply_slot(group_id):
    key = str(group_id)
    with GROUP_REPLY_JOB_LOCK:
        active = int(GROUP_REPLY_JOB_STATE.get(key, 0) or 0) - 1
        if active > 0:
            GROUP_REPLY_JOB_STATE[key] = active
        else:
            GROUP_REPLY_JOB_STATE.pop(key, None)


def _group_help_summary():
    image = _help_image_cq(mode="group") if BOT_HELP_IMAGE_ENABLED else None
    if image:
        return image
    return (
        "群聊可用命令：赛事、报名、我的成绩、预约、提交成绩、floor、finish。\n"
        "查询语法：@bot [用户名] 指令（用户名可省略）。\n"
        "个人看板：@bot 用户名。\n"
        "绑定请私聊 bot 发送“绑定”，并设置4-5位绑定密码。\n"
        "群里同一时段请求太多时会触发总量限流。\n"
        "同一群同时处理的慢请求也会受并发限制。\n"
        "示例：@bot 44ra、@bot Oracle_F 32k综率、@bot Oracle_F。"
    )


def _group_long_reply_summary(message):
    normalized = (message or "").strip()
    if normalized.lower() in {"help", "帮助", "菜单"}:
        return "帮助内容较长，请私聊 bot 发送 help 查看完整帮助。"
    if normalized in {"我的成绩", "我的限时赛成绩"}:
        return "成绩内容较长，请私聊 bot 发送 我的成绩 查看完整结果。"
    if normalized.startswith("报名"):
        return "报名列表较长，请私聊 bot 发送 报名 查看完整列表。"
    if normalized.startswith("预约") or normalized in {"我的限时预约", "预约限时赛", "修改限时预约", "取消限时预约"}:
        return "预约内容较长，请私聊 bot 发送 预约 查看完整信息。"
    if normalized in {"赛事", "赛事列表", "查看赛事", "归档比赛", "查看归档比赛"}:
        return "赛事内容较长，请私聊 bot 发送 查看赛事 查看完整列表。"
    return "内容较长，请私聊 bot 查看完整结果。"


def _group_should_redirect_reply(reply_text):
    text = str(reply_text or "").strip()
    if not text:
        return False
    if "[CQ:image," in text:
        return True
    return len(text) > GROUP_CHAT_MAX_REPLY_CHARS


def _group_should_keep_direct_reply(message, existing_flow):
    normalized = (message or "").strip().lower()
    if normalized in {"提交成绩", "提交", "floor", "finish"}:
        return True
    if normalized.startswith(("提交成绩 ", "提交 ")):
        return True
    action = None if not isinstance(existing_flow, dict) else str(existing_flow.get("action") or "")
    return action in {
        "submit_score",
        "floor_select_event",
        "await_early_replay",
        "finish_select_session",
        "await_final_replay",
    }


def _group_is_allowed_command(message, *, has_flow):
    if has_flow and not _is_top_level_command(message):
        return True
    normalized = (message or "").strip()
    if not normalized:
        return False
    lower = normalized.lower()
    allowed_exact = {
        "help",
        "帮助",
        "菜单",
        "赛事",
        "赛事列表",
        "查看赛事",
        "报名",
        "我的成绩",
        "我的限时赛成绩",
        "预约",
        "预约限时赛",
        "修改限时预约",
        "取消限时预约",
        "我的限时预约",
        "提交成绩",
        "提交",
        "floor",
        "finish",
        "取消",
        "cancel",
        "返回",
        "更多",
        "确认",
        "confirm",
    }
    if normalized in allowed_exact or lower in allowed_exact:
        return True
    if is_verse_query_message(normalized):
        return True
    if _is_dashboard_query_message(normalized):
        return True
    return normalized.startswith(("绑定 ", "提交成绩 ", "提交 "))


def _group_should_consume_rate_limit(message, *, has_flow):
    # Count first-step commands only; flow follow-up inputs do not consume quota.
    if has_flow and not _is_top_level_command(message):
        return False
    return True


def _group_is_forbidden_command(message):
    normalized = (message or "").strip()
    if _is_admin_command(normalized):
        return True
    return normalized.startswith("管理员帮助")


def _handle_group_dashboard_query(connection, *, message):
    normalized = (message or "").strip()
    if not _is_dashboard_query_message(normalized):
        return None
    target = resolve_dashboard_target(connection, game_platform="2048verse", username=normalized)
    if target is None:
        return "未找到该用户的绑定信息，无法查看个人看板。"
    dashboard = get_player_dashboard(
        connection,
        game_platform="2048verse",
        player_id=target["player_id"],
    )
    return format_dashboard_reply(target.get("account_key") or target.get("display_name") or normalized, dashboard)


def _is_dashboard_query_message(message):
    normalized = (message or "").strip()
    if not normalized:
        return False
    if " " in normalized:
        return False
    if normalized.lower() in {
        "help",
        "帮助",
        "菜单",
        "绑定",
        "赛事",
        "赛事列表",
        "查看赛事",
        "归档比赛",
        "查看归档比赛",
        "报名",
        "取消报名",
        "我的报名",
        "我的成绩",
        "我的限时赛成绩",
        "预约",
        "预约限时赛",
        "我的限时预约",
        "修改限时预约",
        "取消限时预约",
        "提交成绩",
        "提交",
        "我的提交",
        "我的档案",
        "看板",
        "查看看板",
        "设置看板",
        "清空看板",
        "floor",
        "finish",
        "取消",
        "返回",
        "更多",
        "确认",
        "confirm",
        "cancel",
    }:
        return False
    if is_verse_query_message(normalized):
        return False
    return True


def _admin_actor_id(bot_platform, bot_user_id):
    return "{}:{}".format(bot_platform, bot_user_id)


def _user_actor_id(bot_platform, bot_user_id):
    return "{}:{}".format(bot_platform, bot_user_id)


def _load_event_rows(
    connection,
    *,
    platform_code=None,
    player_id=None,
    include_finished=True,
    include_archived=False,
    include_history_hidden=False,
):
    query = """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            e.status,
            e.start_time,
            e.end_time,
            e.registration_close_time,
            e.competition_type,
            e.metadata_json,
            p.code AS platform_code,
            v.code AS variant_code,
            r.status AS registration_status
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        LEFT JOIN registrations r
            ON r.event_id = e.id
           AND r.player_id = ?
        WHERE 1 = 1
    """
    params = [player_id]
    if platform_code:
        query += " AND p.code = ?"
        params.append(platform_code)
    if include_archived:
        query += " AND e.status IN ('ready', 'active', 'finished', 'archived')"
    elif include_finished:
        query += " AND e.status IN ('ready', 'active', 'finished')"
    else:
        query += " AND e.status IN ('ready', 'active')"
    query += " ORDER BY COALESCE(e.start_time, '') DESC, e.id DESC"
    rows = [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]
    if include_archived or include_history_hidden:
        return rows
    return [row for row in rows if not _is_history_hidden(row)]


def _resolve_event(connection, game_platform, explicit_event_code=None):
    if explicit_event_code:
        row = connection.execute(
            """
            SELECT e.event_code
            FROM events e
            JOIN platforms p ON p.id = e.platform_id
            WHERE e.event_code = ? AND p.code = ?
            """,
            (explicit_event_code, game_platform),
        ).fetchone()
        if row is None:
            raise ValueError("Event not found for platform: {}".format(explicit_event_code))
        return explicit_event_code

    rows = connection.execute(
        """
        SELECT e.event_code
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        WHERE p.code = ?
          AND e.status IN ('ready', 'active')
        ORDER BY COALESCE(e.start_time, '') DESC, e.id DESC
        LIMIT 3
        """,
        (game_platform,),
    ).fetchall()
    if not rows:
        raise ValueError("No active event available; please specify event=赛事ID")
    if len(rows) > 1:
        raise ValueError("Multiple active events found; please specify event=赛事ID")
    return rows[0]["event_code"]


def _list_recent_events(connection, game_platform, *, player_id=None, limit=10):
    rows = _load_event_rows(
        connection,
        platform_code=game_platform,
        player_id=player_id,
        include_finished=True,
        include_archived=False,
        include_history_hidden=False,
    )
    return rows[:limit]


def _list_open_events(connection, game_platform, *, player_id=None, limit=10):
    rows = _load_event_rows(
        connection,
        platform_code=game_platform,
        player_id=player_id,
        include_finished=False,
        include_archived=False,
        include_history_hidden=False,
    )
    now = datetime.now(LOCAL_TIMEZONE)
    def _is_open(row):
        deadline = _parse_local_time(row.get("registration_close_time")) or _parse_local_time(row.get("start_time"))
        return deadline is None or now < deadline
    return [row for row in rows if _is_open(row)][:limit]


def _list_reservable_events(connection, game_platform, *, player_id=None, limit=20):
    rows = _load_event_rows(
        connection,
        platform_code=game_platform,
        player_id=player_id,
        include_finished=False,
        include_archived=False,
        include_history_hidden=False,
    )
    now = datetime.now(LOCAL_TIMEZONE)
    result = []
    for row in rows:
        if row.get("registration_status") != "active":
            continue
        if not _is_reservation_timed_event(connection, row["event_code"]):
            continue
        end_dt = _parse_local_time(row.get("end_time"))
        if end_dt is not None and now >= end_dt:
            continue
        result.append(row)
    return result[:limit]


def _list_archived_events(connection, game_platform, *, player_id=None, limit=10):
    rows = _load_event_rows(
        connection,
        platform_code=game_platform,
        player_id=player_id,
        include_finished=True,
        include_archived=True,
        include_history_hidden=True,
    )
    archived = [row for row in rows if (row.get("status") or "").lower() == "archived"]
    return archived[:limit]


def _parse_year_month(text):
    value = (text or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", value):
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m")
    except ValueError:
        return None
    return dt.strftime("%Y-%m")


def _event_month_key(row):
    start_dt = _parse_local_time(_row_get(row, "start_time"))
    if start_dt is None:
        end_dt = _parse_local_time(_row_get(row, "end_time"))
        if end_dt is None:
            return None
        return end_dt.strftime("%Y-%m")
    return start_dt.strftime("%Y-%m")


def _event_started(row):
    start_dt = _parse_local_time(_row_get(row, "start_time"))
    if start_dt is None:
        return True
    return datetime.now(LOCAL_TIMEZONE) >= start_dt


def _event_should_refresh_on_query(row):
    status = str(row.get("status") or "").lower()
    if status == "archived":
        return False
    if MY_SCORE_QUERY_LIVE_ONLY and _runtime_status_label(row) != "进行中":
        return False
    return MY_SCORE_LOCK_REFRESH_ON_QUERY or MY_SCORE_PLAYER_LOCK_REFRESH_ON_QUERY or MY_SCORE_SETTLE_ON_QUERY


def _my_score_player_refresh_key(row, binding):
    return "{}:{}".format(row["event_code"], binding["player_id"])


def _claim_my_score_player_refresh(row, binding):
    key = _my_score_player_refresh_key(row, binding)
    now = datetime.now(LOCAL_TIMEZONE)
    last = MY_SCORE_PLAYER_REFRESH_TIMESTAMPS.get(key)
    if last is not None:
        elapsed = (now - last).total_seconds()
        if elapsed < MY_SCORE_PLAYER_REFRESH_COOLDOWN_SECONDS:
            remaining = max(1, int(MY_SCORE_PLAYER_REFRESH_COOLDOWN_SECONDS - elapsed))
            return False, remaining
    MY_SCORE_PLAYER_REFRESH_TIMESTAMPS[key] = now
    return True, 0


def _claim_cooldown(cache, key, cooldown_seconds):
    if cooldown_seconds <= 0:
        return True, 0
    now = datetime.now(LOCAL_TIMEZONE)
    last = cache.get(key)
    if last is not None:
        elapsed = (now - last).total_seconds()
        if elapsed < cooldown_seconds:
            remaining = max(1, int(cooldown_seconds - elapsed))
            return False, remaining
    cache[key] = now
    return True, 0


def _maybe_settle_due_reservations(connection, *, event_code=None, force=False):
    if force:
        return settle_due_reservations(connection, event_code=event_code)
    key = str(event_code or "__all__")
    allowed, remaining = _claim_cooldown(
        RESERVATION_SETTLE_TIMESTAMPS,
        key,
        RESERVATION_SETTLE_COOLDOWN_SECONDS,
    )
    if not allowed:
        return {"skipped": True, "reason": "cooldown", "remaining_seconds": remaining}
    return settle_due_reservations(connection, event_code=event_code)


def _maybe_settle_event(connection, event_code):
    allowed, remaining = _claim_cooldown(
        MY_SCORE_EVENT_SETTLE_TIMESTAMPS,
        str(event_code),
        MY_SCORE_EVENT_SETTLE_COOLDOWN_SECONDS,
    )
    if not allowed:
        return {"skipped": True, "reason": "cooldown", "remaining_seconds": remaining}
    settle_event(connection, event_code)
    return {"skipped": False}


def _ensure_my_score_discord_sync(connection, refresh_context):
    cached = refresh_context.get("discord_sync")
    if cached is not None:
        return cached
    started = perf_counter()
    try:
        summary = sync_locking_scores_from_discord(connection)
    except Exception as exc:
        LOGGER.warning(f"my_score_discord_sync_failed error={exc}")
        summary = {"enabled": False, "reason": "exception:{}".format(type(exc).__name__)}
    elapsed_ms = int((perf_counter() - started) * 1000)
    refresh_context["discord_sync"] = summary
    if elapsed_ms >= MY_SCORE_SLOW_LOG_MS:
        LOGGER.warning(
            f"my_score_discord_sync elapsed_ms={elapsed_ms} enabled={summary.get('enabled')} reason={summary.get('reason')}"
        )
    else:
        LOGGER.info(
            "my_score_discord_sync elapsed_ms=%s enabled=%s reason=%s",
            elapsed_ms,
            summary.get("enabled"),
            summary.get("reason"),
        )
    return summary


def _refresh_event_on_query(connection, row, binding, refresh_context):
    started = perf_counter()
    event_code = row["event_code"]
    actions = []
    if _is_reservation_timed_event(connection, event_code):
        reservation_outcome = _maybe_settle_due_reservations(connection, event_code=event_code)
        if reservation_outcome.get("skipped"):
            actions.append("reservation_settle=cooldown:{}s".format(reservation_outcome.get("remaining_seconds")))
        else:
            actions.append("reservation_settle")
    if (
        MY_SCORE_PLAYER_LOCK_REFRESH_ON_QUERY
        and event_uses_locking(row["competition_type"], row["platform_code"], row["variant_code"])
    ):
        session = _read_latest_open_lock_session(connection, row["id"], binding["player_id"])
        if session is not None:
            allowed, remaining = _claim_my_score_player_refresh(row, binding)
            if allowed:
                sync_summary = _ensure_my_score_discord_sync(connection, refresh_context)
                outcome = refresh_locking_player_scores_from_discord(
                    connection,
                    event_code,
                    player_id=binding["player_id"],
                    sync_summary=sync_summary,
                )
                actions.append("player_refresh={}".format(outcome.get("reason") or outcome.get("status") or "ok"))
            else:
                actions.append("player_refresh=cooldown:{}s".format(remaining))
    elif MY_SCORE_LOCK_REFRESH_ON_QUERY and event_uses_locking(row["competition_type"], row["platform_code"], row["variant_code"]):
        try:
            refresh_locking_event_scores_from_discord(connection, event_code)
            actions.append("event_refresh=ok")
        except Exception:
            actions.append("event_refresh=error")
    if MY_SCORE_SETTLE_ON_QUERY:
        settle_outcome = _maybe_settle_event(connection, event_code)
        if settle_outcome.get("skipped"):
            actions.append("event_settle=cooldown:{}s".format(settle_outcome.get("remaining_seconds")))
        else:
            actions.append("event_settle")
    elapsed_ms = int((perf_counter() - started) * 1000)
    noteworthy_actions = [action for action in actions if action != "event_settle"]
    if noteworthy_actions or elapsed_ms >= MY_SCORE_SLOW_LOG_MS:
        if elapsed_ms >= MY_SCORE_SLOW_LOG_MS:
            LOGGER.warning(
                "my_score_refresh event=%s player_id=%s elapsed_ms=%s actions=%s",
                event_code,
                binding["player_id"],
                elapsed_ms,
                ",".join(noteworthy_actions or actions) or "-",
            )
        else:
            LOGGER.info(
                "my_score_refresh event=%s player_id=%s elapsed_ms=%s actions=%s",
                event_code,
                binding["player_id"],
                elapsed_ms,
                ",".join(noteworthy_actions) or "-",
            )


def _load_my_score_event_rows(connection, binding, *, archived_only=False, month_key=None):
    rows = _load_event_rows(
        connection,
        platform_code=binding["game_platform"],
        player_id=binding["player_id"],
        include_finished=True,
        include_archived=True,
        include_history_hidden=True,
    )
    filtered = []
    for row in rows:
        status = (row.get("status") or "").lower()
        if row.get("registration_status") != "active":
            continue
        if not _event_started(row):
            continue
        if archived_only:
            if status != "archived":
                continue
        elif status == "archived":
            continue
        if month_key and _event_month_key(row) != month_key:
            continue
        filtered.append(row)
    return filtered


def _read_event_result_for_player(connection, event_id, player_id):
    return connection.execute(
        """
        SELECT rank_value, primary_metric_value, best_single_score, result_payload_json
        FROM event_results
        WHERE event_id = ? AND player_id = ?
        LIMIT 1
        """,
        (event_id, player_id),
    ).fetchone()


def _read_latest_open_lock_session(connection, event_id, player_id):
    return connection.execute(
        """
        SELECT id, status, metadata_json
        FROM attempt_sessions
        WHERE event_id = ?
          AND player_id = ?
          AND status IN ('pending_lock', 'locked_in_progress')
        ORDER BY id DESC
        LIMIT 1
        """,
        (event_id, player_id),
    ).fetchone()


def _format_lock_pending_hint(session_row):
    if session_row is None:
        return None
    metadata = _parse_json(session_row["metadata_json"])
    early = metadata.get("early_replay") if isinstance(metadata.get("early_replay"), dict) else None
    final = metadata.get("final_replay") if isinstance(metadata.get("final_replay"), dict) else None
    auto_match = metadata.get("discord_auto_match") if isinstance(metadata.get("discord_auto_match"), dict) else None
    if auto_match and auto_match.get("status") == "manual_required":
        reason = str(auto_match.get("reason") or "")
        if reason == "multiple_prefix_matches":
            return "锁局待人工确认（匹配到多条终局记录）"
        if reason == "post_attach_prefix_failed":
            return "锁局待人工确认（终局前缀复核未通过）"
        return "锁局待人工确认"
    if early and not final:
        return "已 floor，待同步终局记录"
    if early and final:
        return "终局已提交，待审核"
    return "锁局流程进行中"


def _format_score_value(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
    return str(value)


def _build_my_score_line(connection, row, binding, refresh_context):
    if _event_should_refresh_on_query(row):
        try:
            _refresh_event_on_query(connection, row, binding, refresh_context)
        except Exception:
            pass
    result = _read_event_result_for_player(connection, row["id"], binding["player_id"])
    runtime = _runtime_status_label(row)
    if result is None:
        if event_uses_locking(row["competition_type"], row["platform_code"], row["variant_code"]):
            session = _read_latest_open_lock_session(connection, row["id"], binding["player_id"])
            hint = _format_lock_pending_hint(session)
            if hint:
                return "{} | {} | 暂无成绩（{}）".format(row["event_name"], runtime, hint)
        return "{} | {} | 暂无成绩".format(row["event_name"], runtime)
    payload = _parse_json(result["result_payload_json"])
    rank_text = _format_score_value(result["rank_value"])
    score_text = _format_score_value(result["primary_metric_value"])
    line = "{} | {} | 排名 {} | 分数 {}".format(row["event_name"], runtime, rank_text, score_text)
    if row["competition_type"] == "timed_scoring":
        full_boards = payload.get("total_full_boards")
        single_best = result["best_single_score"]
        line += " | 满盘数 {}".format(_format_score_value(full_boards))
        line += " | 单局最高分 {}".format(_format_score_value(single_best))
    elif result["best_single_score"] is not None:
        line += " | 单局最高分 {}".format(_format_score_value(result["best_single_score"]))
    return line


def _build_my_score_page_reply(connection, binding, flow):
    archived_only = bool(flow.get("archived_only"))
    month_key = flow.get("month_key")
    offset = int(flow.get("offset") or 0)
    rows = _load_my_score_event_rows(connection, binding, archived_only=archived_only, month_key=month_key)
    total = len(rows)
    if total == 0:
        if archived_only:
            return "当前没有可查询的已归档赛事成绩。"
        return "你当前没有可查询成绩的已报名赛事。请先报名比赛。"
    page_rows = rows[offset : offset + MY_SCORE_PAGE_SIZE]
    in_progress = [row for row in page_rows if _runtime_status_label(row) == "进行中"]
    finished = [row for row in page_rows if _runtime_status_label(row) != "进行中"]
    refresh_context = {}

    title = "我的成绩（仅显示已报名且已开赛）" if not archived_only else "归档成绩（仅显示已报名且已开赛）"
    lines = [title]
    if in_progress:
        lines.append("")
        lines.append("进行中")
        for row in in_progress:
            lines.append("- " + _build_my_score_line(connection, row, binding, refresh_context))
    if finished:
        lines.append("")
        lines.append("已完赛")
        for row in finished:
            lines.append("- " + _build_my_score_line(connection, row, binding, refresh_context))

    shown_to = min(offset + MY_SCORE_PAGE_SIZE, total)
    lines.append("")
    lines.append("已显示 {}/{} 条。".format(shown_to, total))
    if shown_to < total:
        lines.append("回复“更多”查看下一页。")
    else:
        lines.append("已显示{}全部赛事。".format("该月" if month_key else ("已归档" if archived_only else "全部未归档")))
    lines.append("可回复 YYYY-MM 按月查询，或回复“返回”重置。")
    if not archived_only:
        lines.append("其余赛事可能为未开始或已归档。")
    return "\n".join(lines)


def _list_lockable_registered_events(connection, binding):
    rows = _load_event_rows(
        connection,
        platform_code=binding["game_platform"],
        player_id=binding["player_id"],
        include_finished=False,
        include_archived=False,
        include_history_hidden=False,
    )
    result = []
    now = datetime.now(LOCAL_TIMEZONE)
    for row in rows:
        if row.get("registration_status") != "active":
            continue
        if not event_uses_locking(row["competition_type"], row["platform_code"], row["variant_code"]):
            continue
        if row["start_time"]:
            start_dt = _parse_local_time(row["start_time"])
            if now < start_dt:
                continue
        result.append(row)
    return result


def _list_open_finish_sessions(connection, binding):
    rows = connection.execute(
        """
        SELECT
            s.id,
            s.status,
            s.start_command_time,
            s.lock_deadline_time,
            s.metadata_json,
            e.event_code,
            e.event_name
        FROM attempt_sessions s
        JOIN events e ON e.id = s.event_id
        WHERE s.player_id = ?
          AND s.status IN ('pending_lock', 'locked_in_progress')
        ORDER BY s.id DESC
        """,
        (binding["player_id"],),
    ).fetchall()
    result = []
    for row in rows:
        metadata = _parse_json(row["metadata_json"])
        early = metadata.get("early_replay") if isinstance(metadata.get("early_replay"), dict) else None
        final = metadata.get("final_replay") if isinstance(metadata.get("final_replay"), dict) else None
        if not early or final:
            continue
        result.append({**dict(row), "metadata": metadata})
    return result


def _load_session_for_player(connection, session_id, player_id):
    row = connection.execute(
        """
        SELECT
            s.id,
            s.status,
            s.player_id,
            s.metadata_json,
            e.event_code,
            e.event_name
        FROM attempt_sessions s
        JOIN events e ON e.id = s.event_id
        WHERE s.id = ? AND s.player_id = ?
        """,
        (session_id, player_id),
    ).fetchone()
    if row is None:
        raise ValueError("Attempt session not found")
    return row


def _find_existing_open_session(connection, event_code, player_id):
    return connection.execute(
        """
        SELECT
            s.id,
            s.status,
            s.metadata_json
        FROM attempt_sessions s
        JOIN events e ON e.id = s.event_id
        WHERE e.event_code = ?
          AND s.player_id = ?
          AND s.status IN ('pending_lock', 'locked_in_progress')
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (event_code, player_id),
    ).fetchone()


def _load_event_competition_type(connection, event_code):
    row = connection.execute(
        """
        SELECT competition_type
        FROM events
        WHERE event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if row is None:
        raise ValueError("Event not found: {}".format(event_code))
    return row["competition_type"]


def _submit_score_by_event_mode(
    connection,
    *,
    event_code,
    username,
    display_name,
    submitter_platform,
    submitter_account,
    source_record_id,
    started_at,
    ended_at,
    raw_score,
    final_score,
    competition_score,
    primary_time_ms,
    target_tile_value,
    score_before_target,
    evidence,
    payload,
):
    competition_type = _load_event_competition_type(connection, event_code)
    if competition_type == "points_series_3x4":
        direct = add_manual_score(
            connection,
            event_code,
            username=username,
            display_name=display_name,
            source_record_id=source_record_id,
            started_at=started_at,
            ended_at=ended_at,
            raw_score=raw_score,
            final_score=final_score,
            competition_score=competition_score,
            primary_time_ms=primary_time_ms,
            target_tile_value=target_tile_value,
            score_before_target=score_before_target,
            evidence_note=(evidence or {}).get("note"),
        )
        return {"mode": "direct", "result": direct}

    pending = submit_pending_score(
        connection,
        event_code,
        username=username,
        display_name=display_name,
        submitter_platform=submitter_platform,
        submitter_account=submitter_account,
        source_record_id=source_record_id,
        started_at=started_at,
        ended_at=ended_at,
        raw_score=raw_score,
        final_score=final_score,
        competition_score=competition_score,
        primary_time_ms=primary_time_ms,
        target_tile_value=target_tile_value,
        score_before_target=score_before_target,
        evidence=evidence,
        payload=payload,
    )
    return {"mode": "pending", "result": pending}


def _build_help_text():
    return (
        "版本: bot-flow-2026-06-04\n"
        "选手命令\n"
        "1. 绑定（私聊，需设置4-5位绑定密码）\n"
        "2. 赛事\n"
        "3. 归档比赛\n"
        "4. 报名\n"
        "5. 取消报名\n"
        "6. 我的报名\n"
        "7. 预约限时赛\n"
        "8. 我的限时预约\n"
        "9. 我的成绩\n"
        "10. 修改限时预约\n"
        "11. 取消限时预约\n"
        "12. 提交\n"
        "13. 我的提交\n"
        "14. 我的档案\n"
        "15. 看板\n"
        "16. floor\n"
        "17. finish\n"
        "18. [用户名] 指令查询（如 44ra、Oracle_F 32k综率）\n"
        "19. 群里可用：@bot 用户名 查看个人看板\n"
        "提示：进行中的流程里，随时发送“取消”退出。\n"
        "时间输入固定格式：YYYY-MM-DD HH:MM（例如 2026-05-22 20:30）。"
    )


def _build_admin_help_text():
    return (
        "管理员命令\n"
        "1. 待审核列表 event=赛事ID\n"
        "2. 通过成绩 submission=编号\n"
        "3. 驳回成绩 submission=编号 reason=原因\n"
        "4. 锁局状态 event=赛事ID user=用户名\n"
        "5. 异常锁局列表\n"
        "6. 刷新锁局 event=赛事ID\n"
        "7. 解绑用户 bot_user_id=QQ号 / user=Verse用户名 reason=原因\n"
        "说明：补录回放、手动绑 Verse 记录仍在本机 CLI 中处理。"
    )


def _wrap_text(text, max_chars):
    text = str(text or "")
    if not text:
        return []
    lines = []
    current = ""
    for ch in text:
        current += ch
        if len(current) >= max_chars:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines


def _build_help_image(path, title, sections):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Portrait layout for mobile reading.
    width = 900
    margin = 28
    section_gap = 26
    title_height = 78
    tip_h = 74
    footer_height = 26
    column_count = 2
    col_gap = 14
    card_h = 130
    section_title_h = 52
    section_padding = 14
    section_card_gap = 10
    card_w = int((width - margin * 2 - col_gap * (column_count - 1)) / column_count)

    # pre-calc dynamic height
    content_h = title_height + tip_h
    for section in sections:
        count = len(section.get("items") or [])
        rows = (count + column_count - 1) // column_count
        content_h += section_title_h + section_padding * 2 + rows * card_h + max(0, rows - 1) * section_card_gap + section_gap
    height = content_h + footer_height + margin

    image = Image.new("RGB", (width, height), "#1c1f33")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 46)
        section_font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 30)
        cmd_font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 28)
        note_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
        foot_font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 22)
    except Exception:
        title_font = ImageFont.load_default()
        section_font = ImageFont.load_default()
        cmd_font = ImageFont.load_default()
        note_font = ImageFont.load_default()
        foot_font = ImageFont.load_default()

    # page card (dark glass)
    draw.rounded_rectangle((14, 14, width - 14, height - 14), radius=26, fill="#222842", outline="#5f6e98", width=2)
    draw.text((margin, margin - 2), title, fill="#f2f5ff", font=title_font)

    # prominent tip near top
    tip_top = margin + title_height - 8
    draw.rounded_rectangle((margin, tip_top, width - margin, tip_top + tip_h - 8), radius=12, fill="#384b7e", outline="#8aa0d8", width=1)
    draw.text((margin + 14, tip_top + 12), "提示：流程中随时发送“取消”退出；需要选择时请回复序号。", fill="#f3f6ff", font=foot_font)

    y = margin + title_height + tip_h
    section_bg = ["#2a314f", "#2c3454", "#27314d", "#2b3658"]
    card_bg = ["#3b4e7a", "#355f74", "#4d4a7f", "#4c3f73"]
    for s_index, section in enumerate(sections):
        items = section.get("items") or []
        rows = (len(items) + column_count - 1) // column_count
        sec_h = section_title_h + section_padding * 2 + rows * card_h + max(0, rows - 1) * section_card_gap
        sec_rect = (margin, y, width - margin, y + sec_h)
        draw.rounded_rectangle(sec_rect, radius=16, fill=section_bg[s_index % len(section_bg)], outline="#6274a8", width=1)
        draw.text((margin + 16, y + 10), section.get("title") or "功能", fill="#eaf0ff", font=section_font)
        base_y = y + section_title_h + section_padding
        for idx, item in enumerate(items):
            r = idx // column_count
            c = idx % column_count
            x0 = margin + c * (card_w + col_gap)
            y0 = base_y + r * (card_h + section_card_gap)
            x1 = x0 + card_w
            y1 = y0 + card_h
            draw.rounded_rectangle((x0, y0, x1, y1), radius=12, fill=card_bg[(c + s_index) % len(card_bg)], outline="#8aa0d8", width=1)
            draw.text((x0 + 12, y0 + 10), item.get("cmd") or "-", fill="#f5f7ff", font=cmd_font)
            note = item.get("note") or ""
            use = item.get("use") or ""
            if note:
                note_lines = _wrap_text(note, 18)
                for i, line in enumerate(note_lines[:2]):
                    draw.text((x0 + 12, y0 + 50 + i * 24), line, fill="#d5ddf7", font=note_font)
            if use:
                use_lines = _wrap_text("示例: {}".format(use), 20)
                for i, line in enumerate(use_lines[:2]):
                    draw.text((x0 + 12, y0 + 96 + i * 20), line, fill="#b8c7ef", font=note_font)
        y += sec_h + section_gap

    draw.text((margin, height - 34), "QQ Bot 帮助", fill="#b9c7ef", font=note_font)
    image.save(path)
    return True


def _help_image_cq(mode="player"):
    if not BOT_HELP_IMAGE_ENABLED:
        return None
    if mode == "player":
        path = HELP_IMAGE_DIR / "player_help.png"
        sections = [
            {
                "title": "账号与赛事",
                "items": [
                    {"cmd": "绑定 / 解绑", "note": "私聊绑定 Verse 账号并设置密码"},
                    {"cmd": "赛事", "note": "查看赛事列表"},
                    {"cmd": "归档比赛", "note": "查看归档赛事"},
                    {"cmd": "我的报名", "note": "查看我的报名"},
                ],
            },
            {
                "title": "报名与预约",
                "items": [
                    {"cmd": "报名", "note": "按序号报名赛事"},
                    {"cmd": "取消报名", "note": "取消未开赛报名"},
                    {"cmd": "预约限时赛", "note": "预约限时窗口"},
                    {"cmd": "修改限时预约", "note": "修改预约时间"},
                    {"cmd": "取消限时预约", "note": "取消预约"},
                    {"cmd": "我的限时预约", "note": "查看预约状态"},
                    {"cmd": "我的成绩", "note": "查看我的赛事成绩"},
                ],
            },
            {
                "title": "成绩与锁局",
                "items": [
                    {"cmd": "提交", "note": "手动提交成绩"},
                    {"cmd": "我的提交", "note": "查看提交记录"},
                    {"cmd": "floor", "note": "开始锁局上传 early"},
                    {"cmd": "finish", "note": "上传终局回放"},
                    {"cmd": "我的档案", "note": "查看个人档案"},
                ],
            },
        ]
        title = "QQ Bot 选手帮助"
    elif mode == "group":
        path = HELP_IMAGE_DIR / "group_help.png"
        if path.exists():
            return "[CQ:image,file={}]".format(path.as_uri())
        sections = [
            {
                "title": "比赛功能（需@bot）",
                "items": [
                    {"cmd": "@bot help", "note": "查看群聊命令图"},
                    {"cmd": "@bot 绑定", "note": "按提示绑定verse账号"},
                    {"cmd": "@bot 查看赛事", "note": "查看赛事列表"},
                    {"cmd": "@bot 报名", "note": "按提示报名"},
                    {"cmd": "@bot 我的成绩", "note": "查看自己的成绩"},
                    {"cmd": "@bot 预约", "note": "预约/修改/取消预约"},
                    {"cmd": "@bot 提交成绩", "note": "成绩提交流程"},
                    {"cmd": "@bot floor", "note": "上传 early 回放"},
                    {"cmd": "@bot finish", "note": "上传终局回放"},
                ],
            },
            {
                "title": "玩家查询（用户名可省略）",
                "items": [
                    {"cmd": "@bot 44ra", "note": "查自己4x4 rating"},
                    {"cmd": "@bot 用户名 44pb", "note": "查他人4x4 PB"},
                    {"cmd": "@bot 用户名 8ks", "note": "查他人4x4 8k数量"},
                    {"cmd": "@bot 用户名 32k综率", "note": "查他人32k综率"},
                    {"cmd": "@bot 用户名 4x4", "note": "查4x4总览"},
                    {"cmd": "@bot 用户名 3x4", "note": "查3x4总览"},
                    {"cmd": "@bot 用户名 2x4", "note": "查2x4总览"},
                    {"cmd": "@bot 4x4 wr", "note": "查看4x4榜首"},
                ],
            },
            {
                "title": "限制与边界",
                "items": [
                    {"cmd": "绑定 / 解绑", "note": "仅私聊可用"},
                    {"cmd": "限流", "note": "每人每分钟2条首条命令"},
                    {"cmd": "流程内回复", "note": "序号/确认不计入限流"},
                    {"cmd": "不支持", "note": "42/43模式查询"},
                ],
            },
        ]
        title = "QQ Bot 群聊帮助"
    else:
        path = HELP_IMAGE_DIR / "admin_help.png"
        sections = [
            {
                "title": "选手命令（与普通用户一致）",
                "items": [
                    {"cmd": "绑定", "note": "绑定 Verse 账号"},
                    {"cmd": "赛事", "note": "查看赛事列表"},
                    {"cmd": "归档比赛", "note": "查看归档赛事"},
                    {"cmd": "报名", "note": "按序号报名赛事"},
                    {"cmd": "取消报名", "note": "取消未开赛报名"},
                    {"cmd": "我的报名", "note": "查看我的报名"},
                    {"cmd": "预约限时赛", "note": "预约限时窗口"},
                    {"cmd": "我的限时预约", "note": "查看预约状态"},
                    {"cmd": "我的成绩", "note": "查看我的赛事成绩"},
                    {"cmd": "修改限时预约", "note": "修改预约时间"},
                    {"cmd": "取消限时预约", "note": "取消预约"},
                    {"cmd": "提交", "note": "手动提交成绩"},
                    {"cmd": "我的提交", "note": "查看提交记录"},
                    {"cmd": "floor", "note": "开始锁局上传 early"},
                    {"cmd": "finish", "note": "上传终局回放"},
                    {"cmd": "我的档案", "note": "查看个人档案"},
                ],
            },
            {
                "title": "管理员审核",
                "items": [
                    {"cmd": "待审核列表", "note": "查看待审核提交", "use": "待审核列表 event=20260428"},
                    {"cmd": "通过成绩", "note": "通过指定提交", "use": "通过成绩 submission=123"},
                    {"cmd": "驳回成绩", "note": "驳回并写原因", "use": "驳回成绩 submission=123 reason=格式错误"},
                ],
            },
            {
                "title": "锁局管理",
                "items": [
                    {"cmd": "锁局状态", "note": "查看单用户锁局链状态", "use": "锁局状态 event=20260428 user=abc"},
                    {"cmd": "异常锁局列表", "note": "快速巡检当前异常", "use": "异常锁局列表"},
                    {"cmd": "刷新锁局", "note": "触发Discord回放匹配", "use": "刷新锁局 event=20260430"},
                ],
            },
        ]
        title = "QQ Bot 管理员帮助"
    ok = _build_help_image(path, title, sections)
    if not ok:
        return None
    return "[CQ:image,file={}]".format(path.as_uri())


def _format_events_for_dm(rows):
    if not rows:
        return "当前没有可用赛事。"
    return "可用赛事\n" + _as_numbered_lines(rows)


def _format_my_registrations(rows):
    if not rows:
        return "你当前没有报名中的赛事。"
    lines = []
    for row in rows:
        note = _event_note(row)
        line = "{} | {}".format(row["event_name"], _format_event_time_range(row))
        if note:
            line += " | 备注: {}".format(note)
        lines.append("- " + line)
    return "我的报名\n" + "\n".join(lines)


def _is_registration_cancellable(row):
    start_time = _parse_local_time(_row_get(row, "start_time"))
    if start_time is None:
        return True
    return datetime.now(LOCAL_TIMEZONE) < start_time


def _is_reservation_timed_event(connection, event_code):
    row = connection.execute(
        """
        SELECT competition_type, metadata_json
        FROM events
        WHERE event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if row is None:
        return False
    if row["competition_type"] != "timed_scoring":
        return False
    metadata = _parse_json(row["metadata_json"])
    return metadata.get("timed_mode") == "reservation"


def _parse_score_input(text):
    value = (text or "").strip()
    if not value:
        raise ValueError("Please enter a score")
    lower = value.lower()
    if lower.startswith("t=") or lower.startswith("time=") or lower.startswith("primary_time_ms="):
        if "=" not in value:
            raise ValueError("Please use time=毫秒")
        return {
            "raw_score": None,
            "final_score": None,
            "competition_score": None,
            "primary_time_ms": _parse_int_or_none(value.split("=", 1)[1].strip(), "primary_time_ms"),
        }
    score = _parse_int_or_none(value, "score")
    return {
        "raw_score": None,
        "final_score": score,
        "competition_score": None,
        "primary_time_ms": None,
    }


def _handle_bind_flow(connection, *, bot_platform, bot_user_id, text, flow_scope=None):
    flow = _get_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    if flow is None or flow.get("action") != "bind_account":
        return None
    message = (text or "").strip()
    if message in {"取消", "cancel", "CANCEL"}:
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "已取消绑定流程。"
    if flow.get("step") == "username":
        username = message.strip()
        if not username:
            return "请输入verse用户名。"
        _set_flow(
            bot_platform,
            bot_user_id,
            {
                "action": "bind_account",
                "step": "pin",
                "username": username,
            },
            flow_scope=flow_scope,
        )
        return "请输入4-5位绑定密码。"
    if flow.get("step") == "pin":
        username = (flow.get("username") or "").strip()
        if not username:
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "绑定流程状态异常，已重置。请重新发送“绑定”。"
        bind_pin = message.strip()
        try:
            result = bind_bot_account(
                connection,
                bot_platform=bot_platform,
                bot_user_id=bot_user_id,
                game_platform="2048verse",
                account_key=username,
                bind_pin=bind_pin,
                metadata={"source": "private_bot_flow"},
            )
        except ValueError as exc:
            return str(exc)
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "已绑定 {} 账号：{}\n发送 help 查看可用命令。".format(result["game_platform"], result["account_key"])
    _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    return "绑定流程状态异常，已重置。请重新发送“绑定”。"


def _handle_dashboard_flow(connection, *, binding, bot_platform, bot_user_id, text, flow_scope=None):
    flow = _get_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    if flow is None or flow.get("action") != "dashboard_edit":
        return None

    message = (text or "").strip()
    if message in {"取消", "cancel", "CANCEL"}:
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "已取消个人看板编辑。"

    if message in {"清空", "删除"}:
        clear_player_dashboard(
            connection,
            game_platform=binding["game_platform"],
            player_id=binding["player_id"],
        )
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "个人看板已清空。"

    if message in {"查看", "预览"}:
        current = get_player_dashboard(
            connection,
            game_platform=binding["game_platform"],
            player_id=binding["player_id"],
        )
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        if current is None:
            return "当前还没有设置个人看板。发送“看板”开始设置。"
        return "当前个人看板\n{}".format(current["dashboard_text"])

    try:
        payload = set_player_dashboard(
            connection,
            game_platform=binding["game_platform"],
            player_id=binding["player_id"],
            dashboard_text=message,
        )
    except ValueError as exc:
        return "个人看板设置失败：{}".format(exc)

    _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    return (
        "个人看板已更新。\n"
        "行数：{line_count}/{max_lines}\n"
        "非空格字符：{nonspace}/{max_chars}\n"
        "群里可发送“@bot 你的用户名”查看。"
    ).format(
        line_count=payload["line_count"],
        max_lines=10,
        nonspace=payload["nonspace_char_count"],
        max_chars=200,
    )


def _handle_registration_flow(connection, *, binding, bot_platform, bot_user_id, text, flow_scope=None):
    flow = _get_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    if flow is None:
        return None
    message = (text or "").strip()
    if message in {"取消", "cancel", "CANCEL"}:
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "已取消当前流程。"

    if flow.get("action") == "my_scores_browse":
        raw = (message or "").strip()
        if raw == "返回":
            flow["month_key"] = None
            flow["offset"] = 0
            _set_flow(bot_platform, bot_user_id, flow, flow_scope=flow_scope)
            return _build_my_score_page_reply(connection, binding, flow)
        if raw == "更多":
            total = len(
                _load_my_score_event_rows(
                    connection,
                    binding,
                    archived_only=bool(flow.get("archived_only")),
                    month_key=flow.get("month_key"),
                )
            )
            offset = int(flow.get("offset") or 0)
            if offset + MY_SCORE_PAGE_SIZE >= total:
                return "已经显示完了。可回复 YYYY-MM 查询其他月份，或回复“返回”重置。"
            flow["offset"] = offset + MY_SCORE_PAGE_SIZE
            _set_flow(bot_platform, bot_user_id, flow, flow_scope=flow_scope)
            return _build_my_score_page_reply(connection, binding, flow)
        month_key = _parse_year_month(raw)
        if month_key:
            flow["month_key"] = month_key
            flow["offset"] = 0
            _set_flow(bot_platform, bot_user_id, flow, flow_scope=flow_scope)
            return _build_my_score_page_reply(connection, binding, flow)
        return "请输入“更多”、YYYY-MM（例如 2026-05）或“返回”。"

    if flow.get("action") == "register_event":
        candidate_codes = list(flow.get("candidate_event_codes") or [])
        selected_index = _resolve_choice_index(message, len(candidate_codes))
        if selected_index is None:
            return "请输入序号（可发送“取消”退出）。"
        event_code = candidate_codes[selected_index]
        if is_registered(connection, event_code, binding["account_key"]):
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "你已经报名了这场赛事。"
        try:
            result = register_player(
                connection,
                event_code,
                binding["account_key"],
                display_name=binding["display_name"],
                registered_via="qq_private_bot",
                metadata={"bot_platform": bot_platform, "bot_user_id": bot_user_id},
            )
        except ValueError as exc:
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "报名失败：{}".format(exc)
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        extra = ""
        if _is_reservation_timed_event(connection, event_code):
            extra = "\n你已报名，可发送“预约限时赛”进行预约。"
        return "报名成功。\n{} ({}){}".format(result["display_name"], result["username"], extra)

    if flow.get("action") == "cancel_registration":
        candidate_codes = list(flow.get("candidate_event_codes") or [])
        selected_index = _resolve_choice_index(message, len(candidate_codes))
        if selected_index is None:
            return "请输入序号（可发送“取消”退出）。"
        event_code = candidate_codes[selected_index]
        try:
            cancel_registration(connection, event_code, binding["account_key"])
        except ValueError as exc:
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "取消报名失败：{}".format(exc)
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "已取消报名。"

    if flow.get("action") == "reserve_timed_event":
        step = flow.get("step") or "event"
        if step == "event":
            candidate_codes = list(flow.get("candidate_event_codes") or [])
            selected_index = _resolve_choice_index(message, len(candidate_codes))
            if selected_index is None:
                return "请输入序号。"
            event_code = candidate_codes[selected_index]
            flow["event_code"] = event_code
            flow["step"] = "start_time"
            _set_flow(bot_platform, bot_user_id, flow, flow_scope=flow_scope)
            return "请输入预约开始时间，格式 YYYY-MM-DD HH:MM（年-月-日 时:分，例如 2026-05-22 20:30）。"
        if step == "start_time":
            event_code = flow.get("event_code")
            try:
                parsed_text = _normalize_time(message, "reserved_start")
            except ValueError:
                return "时间格式不对。请按 YYYY-MM-DD HH:MM 输入，例如 2026-05-22 20:30。"
            reserved_start_dt = _parse_local_time(parsed_text)
            try:
                result = reserve_for_player(
                    connection,
                    event_code=event_code,
                    bot_platform=bot_platform,
                    bot_user_id=bot_user_id,
                    reserved_start_dt=reserved_start_dt,
                    late_confirmed=False,
                )
            except ValueError as exc:
                _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
                return "预约失败：{}".format(exc)
            if result.get("requires_confirm"):
                flow["step"] = "late_confirm"
                flow["pending_start"] = parsed_text
                _set_flow(bot_platform, bot_user_id, flow, flow_scope=flow_scope)
                return "当前时间已晚于该预约开始时间。回复“确认”继续，或发送“取消”退出。"
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "预约成功：{} -> {}".format(result["reserved_start_time"], result["reserved_end_time"])
        if step == "late_confirm":
            if message not in {"确认", "confirm", "CONFIRM"}:
                return "请回复“确认”继续，或发送“取消”退出。"
            event_code = flow.get("event_code")
            reserved_start_dt = _parse_local_time(flow.get("pending_start"))
            try:
                result = reserve_for_player(
                    connection,
                    event_code=event_code,
                    bot_platform=bot_platform,
                    bot_user_id=bot_user_id,
                    reserved_start_dt=reserved_start_dt,
                    late_confirmed=True,
                )
            except ValueError as exc:
                _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
                return "预约失败：{}".format(exc)
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "已确认并预约成功（该预约不可再改）：{} -> {}".format(result["reserved_start_time"], result["reserved_end_time"])

    if flow.get("action") == "cancel_timed_reservation":
        candidate_codes = list(flow.get("candidate_event_codes") or [])
        selected_index = _resolve_choice_index(message, len(candidate_codes))
        if selected_index is None:
            return "请输入序号。"
        event_code = candidate_codes[selected_index]
        try:
            cancel_my_reservation(
                connection,
                event_code=event_code,
                bot_platform=bot_platform,
                bot_user_id=bot_user_id,
            )
        except ValueError as exc:
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "取消预约失败：{}".format(exc)
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "已取消限时预约。"

    if flow.get("action") == "query_timed_score":
        candidate_codes = list(flow.get("candidate_event_codes") or [])
        selected_index = _resolve_choice_index(message, len(candidate_codes))
        if selected_index is None:
            return "请输入序号。"
        event_code = candidate_codes[selected_index]
        _maybe_settle_due_reservations(connection, event_code=event_code, force=True)
        try:
            score = get_my_reservation_score(
                connection,
                event_code=event_code,
                bot_platform=bot_platform,
                bot_user_id=bot_user_id,
            )
        except ValueError as exc:
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "查询失败：{}".format(exc)
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        best_score = score["best_score"]
        return (
            "我的限时赛成绩\n"
            "预约窗口: {start} -> {end}\n"
            "预约状态: {status}\n"
            "结算分: {best}\n"
            "结算时间: {settled}"
        ).format(
            start=score["reserved_start_time"],
            end=score["reserved_end_time"],
            status=_reservation_status_label(
                score["status"],
                score["reserved_start_time"],
                score["reserved_end_time"],
            ),
            best=best_score if best_score is not None else "-",
            settled=score["settled_at"] or "未结算",
        )

    if flow.get("action") == "reservation_hub_menu":
        selected = _resolve_choice_index(message, 4)
        if selected is None:
            return "请输入序号：1预约限时赛 2查看我的预约 3修改限时预约 4取消限时预约"

        # 1/3 share reservation flow
        if selected in {0, 2}:
            rows = _list_reservable_events(connection, binding["game_platform"], player_id=binding["player_id"], limit=20)
            if not rows:
                _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
                return "你还没有报名任何可预约的限时赛。请先发送“报名”。"
            _set_flow(
                bot_platform,
                bot_user_id,
                {
                    "action": "reserve_timed_event",
                    "candidate_event_codes": [row["event_code"] for row in rows],
                    "step": "event",
                },
                flow_scope=flow_scope,
            )
            if selected == 0:
                return "请输入要预约的赛事序号。\n" + _format_events_for_dm(rows)
            return "请输入要修改预约的赛事序号。\n" + _format_events_for_dm(rows)

        # 2: view my reservation
        if selected == 1:
            _maybe_settle_due_reservations(connection)
            rows = _list_reservable_events(connection, binding["game_platform"], player_id=binding["player_id"], limit=20)
            if not rows:
                _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
                return "你还没有报名任何预约型限时赛。请先发送“报名”。"
            for row in rows:
                reservation = get_my_reservation(
                    connection,
                    event_code=row["event_code"],
                    bot_platform=bot_platform,
                    bot_user_id=bot_user_id,
                )
                if reservation is None:
                    continue
                if (reservation["status"] or "").lower() not in {"reserved", "confirmed_late"}:
                    continue
                _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
                return "我的限时预约\n{} | {} -> {} | 状态 {}".format(
                    row["event_name"],
                    reservation["reserved_start_time"],
                    reservation["reserved_end_time"],
                    _reservation_status_label(
                        reservation["status"],
                        reservation["reserved_start_time"],
                        reservation["reserved_end_time"],
                    ),
                )
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "你当前没有有效的限时预约。"

        # 4: cancel reservation
        rows = _list_reservable_events(connection, binding["game_platform"], player_id=binding["player_id"], limit=20)
        if not rows:
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "你还没有报名任何预约型限时赛。请先发送“报名”。"
        _set_flow(
            bot_platform,
            bot_user_id,
            {
                "action": "cancel_timed_reservation",
                "candidate_event_codes": [row["event_code"] for row in rows],
                "step": "event",
            },
            flow_scope=flow_scope,
        )
        return "请输入要取消预约的赛事序号。\n" + _format_events_for_dm(rows)

    return None


def _handle_submit_flow(connection, *, binding, bot_platform, bot_user_id, text, flow_scope=None):
    flow = _get_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    if flow is None or flow.get("action") != "submit_score":
        return None

    message = (text or "").strip()
    if message in {"取消", "cancel", "CANCEL"}:
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "已取消这次成绩提交流程。"

    data = flow.setdefault("data", {})
    step = flow.get("step")
    if step == "event":
        candidate_codes = list(flow.get("candidate_event_codes") or [])
        selected_index = _resolve_choice_index(message, len(candidate_codes))
        if selected_index is None:
            return "请输入序号。"
        resolved = candidate_codes[selected_index]
        data["event_code"] = resolved
        flow["step"] = "score"
        _set_flow(bot_platform, bot_user_id, flow, flow_scope=flow_scope)
        return "请发送成绩。\n普通赛事直接发分数，竞速可发 time=毫秒。"

    if step == "score":
        try:
            data.update(_parse_score_input(message))
        except Exception as exc:
            return "成绩格式不对：{}".format(exc)
        flow["step"] = "ended_at"
        _set_flow(bot_platform, bot_user_id, flow, flow_scope=flow_scope)
        return "请发送终局时间，格式 YYYY-MM-DD HH:MM:SS（年-月-日 时:分:秒，例如 2026-05-22 21:30:00）"

    if step == "ended_at":
        try:
            data["ended_at"] = _normalize_time(message, "ended_at")
        except Exception:
            return "时间格式不对。请按 YYYY-MM-DD HH:MM:SS 输入，例如 2026-05-22 21:30:00。"
        submit_outcome = _submit_score_by_event_mode(
            connection,
            event_code=data["event_code"],
            username=binding["account_key"],
            display_name=binding["display_name"],
            submitter_platform=bot_platform,
            submitter_account=bot_user_id,
            source_record_id=None,
            started_at=None,
            ended_at=data["ended_at"],
            raw_score=data.get("raw_score"),
            final_score=data.get("final_score"),
            competition_score=data.get("competition_score"),
            primary_time_ms=data.get("primary_time_ms"),
            target_tile_value=None,
            score_before_target=None,
            evidence=None,
            payload={"source": "private_bot_flow", "flow_version": 2},
        )
        score_text = data.get("primary_time_ms") if data.get("primary_time_ms") is not None else data.get("final_score")
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        if submit_outcome["mode"] == "direct":
            direct = submit_outcome["result"]
            return (
                "成绩已直通入榜（无需审核）。\n"
                "成绩: {}\n终局时间: {}\n记录ID: {}"
            ).format(score_text, data["ended_at"], direct["source_record_id"])
        result = submit_outcome["result"]
        return (
            "成绩已提交，等待审核。\n"
            "提交编号: {}\n成绩: {}\n终局时间: {}"
        ).format(result["submission_id"], score_text, data["ended_at"])

    _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    return "提交流程状态异常，已重置。请重新发送“提交成绩”。"


def _handle_floor_or_finish_flow(connection, *, binding, bot_platform, bot_user_id, text, file_segments, flow_scope=None):
    flow = _get_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    if flow is None or flow.get("action") not in {
        "floor_select_event",
        "await_early_replay",
        "finish_select_session",
        "await_final_replay",
    }:
        return None

    message = (text or "").strip()
    if message in {"取消", "cancel", "CANCEL"}:
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "已取消当前锁局流程。"

    if flow["action"] == "floor_select_event":
        candidates = _list_lockable_registered_events(connection, binding)
        selected_index = _resolve_choice_index(message, len(candidates))
        if selected_index is None:
            if not candidates:
                _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
                return "你当前没有可锁局的赛事。"
            return "请输入序号。\n" + _as_numbered_lines(candidates)
        selected_event_code = candidates[selected_index]["event_code"]
        existing = _find_existing_open_session(connection, selected_event_code, binding["player_id"])
        if existing is not None:
            metadata = _parse_json(existing["metadata_json"])
            if isinstance(metadata.get("early_replay"), dict) and not isinstance(metadata.get("final_replay"), dict):
                _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
                return "这场赛事你已经提交过 early 回放了，请直接发送 finish。"
            session_id = existing["id"]
        else:
            result = start_attempt_session(
                connection,
                event_code=selected_event_code,
                username=binding["account_key"],
                display_name=binding["display_name"],
            )
            session_id = result["attempt_session_id"]
        _set_flow(
            bot_platform,
            bot_user_id,
            {
                "action": "await_early_replay",
                "event_code": selected_event_code,
                "session_id": session_id,
            },
            flow_scope=flow_scope,
        )
        return "已进入锁局流程。\n请直接发送当前的 early 回放文件。"

    if flow["action"] == "await_early_replay":
        if not file_segments:
            return "现在请直接发送 early 回放文件。"
        segment = file_segments[0]
        local_path = materialize_segment_file(segment)
        try:
            result = attach_early_replay(
                connection,
                flow["event_code"],
                binding["account_key"],
                str(local_path),
                session_id=flow["session_id"],
            )
        except ValueError as exc:
            return "early 回放提交失败：{}\n请重新发送更早阶段的回放文件。".format(exc)
        chain = get_replay_chain_status(connection, flow["event_code"], binding["account_key"], session_id=flow["session_id"])
        early = chain.get("early_replay") or {}
        write_audit_log(
            connection,
            actor_type="qq_bot_user",
            actor_id=_user_actor_id(bot_platform, bot_user_id),
            action_type="upload_early_replay",
            target_table="attempt_sessions",
            target_id=flow["session_id"],
            reason="uploaded early replay via floor",
            after={
                "event_code": flow["event_code"],
                "file_name": describe_segment_file(segment),
                "stored_path": result["stored_path"],
                "sha256": early.get("sha256"),
            },
        )
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return (
            "已收到 early 回放。\n"
            "文件: {}\n"
            "大小: {} 字节\n"
            "SHA256: {}\n"
            "下一步：打完后发送 finish，再上传终局回放。"
        ).format(
            describe_segment_file(segment),
            early.get("size_bytes") or "-",
            (early.get("sha256") or "-")[:12],
        )

    if flow["action"] == "finish_select_session":
        candidate_session_ids = list(flow.get("candidate_session_ids") or [])
        selected_index = _resolve_choice_index(message, len(candidate_session_ids))
        if selected_index is None:
            return "请输入序号。"
        session_id = int(candidate_session_ids[selected_index])
        session = _load_session_for_player(connection, session_id, binding["player_id"])
        metadata = _parse_json(session["metadata_json"])
        if not isinstance(metadata.get("early_replay"), dict):
            return "这条锁局还没有 early 回放，不能 finish。"
        if isinstance(metadata.get("final_replay"), dict):
            return "这条锁局已经提交过终局回放了。"
        _set_flow(
            bot_platform,
            bot_user_id,
            {
                "action": "await_final_replay",
                "event_code": session["event_code"],
                "session_id": session_id,
            },
            flow_scope=flow_scope,
        )
        return "请直接发送这局的终局回放文件。"

    if flow["action"] == "await_final_replay":
        if not file_segments:
            return "现在请直接发送终局回放文件。"
        segment = file_segments[0]
        local_path = materialize_segment_file(segment)
        result = attach_final_replay(
            connection,
            flow["event_code"],
            binding["account_key"],
            str(local_path),
            session_id=flow["session_id"],
        )
        check_result = run_replay_prefix_check(
            connection,
            flow["event_code"],
            binding["account_key"],
            session_id=flow["session_id"],
        )
        write_audit_log(
            connection,
            actor_type="qq_bot_user",
            actor_id=_user_actor_id(bot_platform, bot_user_id),
            action_type="upload_final_replay",
            target_table="attempt_sessions",
            target_id=flow["session_id"],
            reason="uploaded final replay via finish",
            after={
                "event_code": flow["event_code"],
                "file_name": describe_segment_file(segment),
                "stored_path": result["stored_path"],
                "prefix_check": check_result["status"],
                "prefix_reason": check_result["reason"],
            },
        )
        if check_result["status"] == "passed":
            set_replay_review_status(
                connection,
                flow["event_code"],
                binding["account_key"],
                approved=True,
                reviewer="auto:qq_bot",
                note="Auto approved after qq bot prefix check",
                session_id=flow["session_id"],
            )
            write_audit_log(
                connection,
                actor_type="qq_bot_admin",
                actor_id="auto:qq_bot",
                action_type="auto_complete_replay_chain",
                target_table="attempt_sessions",
                target_id=flow["session_id"],
                reason="prefix check passed",
                after={"event_code": flow["event_code"], "status": "completed"},
            )
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "已收到终局回放。\n前缀校验通过，这局锁局已完成。"
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "已收到终局回放，但前缀校验失败。\n原因: {}\n请联系管理员处理。".format(check_result["reason"])

    return None


def _format_pending_rows(rows):
    if not rows:
        return "你当前没有提交记录。"
    lines = []
    for row in rows[:5]:
        score = row["competition_score"]
        if score is None:
            score = row["final_score"] if row["final_score"] is not None else row["raw_score"]
        line = "{} | score={} | ended={} | {}".format(
            row["status"],
            score if score is not None else "-",
            row["ended_at"] or "-",
            row["submitted_at"],
        )
        if row["status"] == "rejected" and row["review_reason"]:
            line += " | reason={}".format(row["review_reason"])
        lines.append(line)
    return "\n".join(lines)


def _load_my_submissions(connection, binding, explicit_event_code=None):
    query = """
        SELECT
            pss.status,
            pss.review_reason,
            pss.ended_at,
            pss.raw_score,
            pss.final_score,
            pss.competition_score,
            pss.submitted_at,
            e.event_code
        FROM pending_score_submissions pss
        JOIN events e ON e.id = pss.event_id
        WHERE pss.player_id = ?
    """
    params = [binding["player_id"]]
    if explicit_event_code:
        query += " AND e.event_code = ?"
        params.append(explicit_event_code)
    query += " ORDER BY pss.submitted_at DESC, pss.id DESC LIMIT 5"
    return connection.execute(query, tuple(params)).fetchall()


def _handle_admin_command(connection, *, bot_platform, bot_user_id, message):
    if message == "管理员帮助":
        return _build_admin_help_text()

    actor_id = _admin_actor_id(bot_platform, bot_user_id)

    if message.startswith("待审核列表"):
        fields = {}
        if " " in message:
            fields = _parse_kv_text(message.split(" ", 1)[1].strip())
        event_code = fields.get("event")
        if not event_code:
            return "请发送：待审核列表 event=赛事ID"
        payload = list_pending_scores(connection, event_code, status="pending")
        rows = payload["rows"]
        if not rows:
            return "这场赛事当前没有待审核成绩。"
        lines = []
        for row in rows[:10]:
            score = row["competition_score"]
            if score is None:
                score = row["final_score"] if row["final_score"] is not None else row["raw_score"]
            lines.append(
                "- {} | {} | score={} | ended={}".format(
                    row["id"],
                    row["username"] or row["display_name"] or "-",
                    score if score is not None else "-",
                    row["ended_at"] or "-",
                )
            )
        write_audit_log(
            connection,
            actor_type="qq_bot_admin",
            actor_id=actor_id,
            action_type="list_pending_scores",
            target_table="events",
            target_id=payload["event"]["id"],
            reason="list pending scores",
            after={"event_code": event_code, "count": len(rows)},
        )
        return "待审核成绩\n" + "\n".join(lines)

    if message.startswith("通过成绩"):
        fields = _parse_kv_text(message[len("通过成绩") :].strip())
        submission_id = _parse_int_or_none(fields.get("submission"), "submission")
        result = approve_pending_score(
            connection,
            submission_id,
            reviewer=actor_id,
            actor_type="qq_bot_admin",
        )
        return "已通过成绩审核。\nsubmission={}\nrecord={}".format(result["submission_id"], result["source_record_id"])

    if message.startswith("驳回成绩"):
        fields = _parse_kv_text(message[len("驳回成绩") :].strip())
        submission_id = _parse_int_or_none(fields.get("submission"), "submission")
        reason = (fields.get("reason") or "").strip()
        if not reason:
            return "请发送：驳回成绩 submission=编号 reason=原因"
        result = reject_pending_score(
            connection,
            submission_id,
            reason=reason,
            reviewer=actor_id,
            actor_type="qq_bot_admin",
        )
        return "已驳回成绩。\nsubmission={}\n原因={}".format(result["submission_id"], result["reason"])

    if message.startswith("解绑用户"):
        fields = _parse_kv_text(message[len("解绑用户") :].strip())
        bot_user_id = (fields.get("bot_user_id") or fields.get("user_id") or "").strip()
        account_key = (fields.get("user") or fields.get("username") or fields.get("account") or "").strip()
        game_platform = (fields.get("platform") or fields.get("game_platform") or "2048verse").strip()
        reason = (fields.get("reason") or "").strip() or None
        if not bot_user_id and not account_key:
            return "请发送：解绑用户 bot_user_id=QQ号 或 user=Verse用户名 reason=原因"
        binding = find_active_bot_binding(
            connection,
            bot_platform=bot_platform,
            bot_user_id=bot_user_id or None,
            game_platform=game_platform or None,
            account_key=account_key or None,
        )
        if binding is None:
            return "未找到可解绑的账号绑定。请提供 bot_user_id=QQ号 或 user=Verse用户名。"
        before = dict(binding)
        deactivate_bot_binding(connection, binding_id=binding["id"])
        after = dict(binding)
        after["is_active"] = 0
        write_audit_log(
            connection,
            actor_type="qq_bot_admin",
            actor_id=actor_id,
            action_type="unbind_bot_account",
            target_table="bot_account_bindings",
            target_id=binding["id"],
            reason=reason or "manual unbind in hub",
            before=before,
            after=after,
        )
        return "已解绑 {} 账号：{}（bot_user_id={}）".format(
            binding["game_platform"],
            binding["account_key"],
            binding["bot_user_id"],
        )

    if message.startswith("刷新锁局"):
        fields = {}
        if " " in message:
            fields = _parse_kv_text(message.split(" ", 1)[1].strip())
        event_code = fields.get("event")
        if not event_code:
            return "请发送：刷新锁局 event=赛事ID"
        outcome = refresh_locking_event_scores_from_discord(connection, event_code)
        if not outcome.get("enabled"):
            return "锁局刷新未执行。\nevent={}\nreason={}".format(event_code, outcome.get("reason") or "-")
        text = (
            "锁局刷新完成。\n"
            "event={}\n"
            "processed={}\nmatched={}\nmanual_required={}\nno_match={}\nskipped={}"
        ).format(
            event_code,
            outcome.get("processed_sessions", 0),
            outcome.get("matched_sessions", 0),
            outcome.get("manual_required_sessions", 0),
            outcome.get("no_match_sessions", 0),
            outcome.get("skipped_sessions", 0),
        )
        diagnostics = ((outcome.get("sync") or {}).get("diagnostics") or {})
        if diagnostics.get("likely_redacted"):
            text += (
                "\n提示：Discord 同步消息疑似被内容权限裁剪（content/embed/attachment 为空），"
                "请在 Discord Developer Portal 为该 Bot 打开 Message Content Intent。"
            )
        return text

    if message.startswith("锁局状态"):
        fields = _parse_kv_text(message[len("锁局状态") :].strip())
        event_code = fields.get("event")
        username = fields.get("user")
        if not event_code or not username:
            return "请发送：锁局状态 event=赛事ID user=用户名"
        chain = get_replay_chain_status(connection, event_code, username)
        early = chain.get("early_replay") or {}
        final = chain.get("final_replay") or {}
        prefix_check = chain.get("prefix_check") or {}
        review = chain.get("review") or {}
        return (
            "锁局状态\n"
            "赛事: {}\n用户: {}\nsession: {}\n状态: {}\n"
            "early: {}\nfinal: {}\n前缀校验: {}\n复核: {}"
        ).format(
            event_code,
            username,
            chain["session_id"],
            chain["status"],
            "已收到" if early else "缺少",
            "已收到" if final else "缺少",
            prefix_check.get("status") or "missing",
            review.get("status") or "missing",
        )

    if message.startswith("异常锁局列表"):
        rows = connection.execute(
            """
            SELECT
                e.id,
                e.event_code,
                e.event_name,
                e.competition_type,
                p.code AS platform_code,
                v.code AS variant_code
            FROM events e
            JOIN platforms p ON p.id = e.platform_id
            LEFT JOIN variants v ON v.id = e.variant_id
            WHERE e.status IN ('ready', 'active')
            ORDER BY COALESCE(e.start_time, '') DESC, e.id DESC
            """
        ).fetchall()
        lines = []
        for row in rows:
            if not event_uses_locking(row["competition_type"], row["platform_code"], row["variant_code"]):
                continue
            issues = list_replay_chain_issues(connection, row["event_code"])
            if not issues:
                continue
            lines.append("{} | {} 条异常".format(row["event_code"], len(issues)))
            for issue in issues[:3]:
                lines.append("  - {}".format(issue))
        if not lines:
            return "当前没有锁局异常。"
        return "异常锁局列表\n" + "\n".join(lines)

    return None


def handle_private_message(connection, *, bot_platform, bot_user_id, text, message_segments=None, flow_scope=None):
    started = perf_counter()
    steps = []
    message = (text or "").strip()
    file_segments = _profile_sync_step(steps, "extract_file_segments", lambda: extract_file_segments(message_segments))
    admin = _profile_sync_step(steps, "is_bot_admin", lambda: is_bot_admin(bot_platform, bot_user_id))
    existing_flow = _get_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    if existing_flow is not None and _flow_expired(existing_flow):
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "当前流程已超时，请重新开始。"
    if existing_flow is not None and message not in {"取消", "cancel", "CANCEL"} and _is_top_level_command(message):
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)

    if message.lower() in {"help", "帮助", "菜单"}:
        image = _help_image_cq(mode="admin_full" if admin else "player")
        base = _build_help_text()
        if admin and not image:
            return base + "\n\n" + _build_admin_help_text()
        return image or base

    if message == "管理员帮助":
        if not admin:
            return "你没有管理员权限。"
        return _help_image_cq(mode="admin_full") or (_build_help_text() + "\n\n" + _build_admin_help_text())

    if _is_admin_command(message):
        if not admin:
            return "你没有管理员权限。"
        result = _profile_sync_step(
            steps,
            "handle_admin_command",
            lambda: _handle_admin_command(connection, bot_platform=bot_platform, bot_user_id=bot_user_id, message=message),
        )
        if result is not None:
            return result

    if message in {"解绑", "取消绑定"}:
        binding = _profile_sync_step(
            steps,
            "get_bot_binding",
            lambda: get_bot_binding(
                connection,
                bot_platform=bot_platform,
                bot_user_id=bot_user_id,
                game_platform="2048verse",
            ),
        )
        if binding is None:
            return "你还没有绑定账号。"
        _profile_sync_step(
            steps,
            "deactivate_bot_binding",
            lambda: deactivate_bot_binding(
                connection,
                bot_platform=bot_platform,
                bot_user_id=bot_user_id,
                game_platform="2048verse",
            ),
        )
        return "已解绑 {} 账号：{}".format(binding["game_platform"], binding["account_key"])

    if message == "绑定":
        _set_flow(bot_platform, bot_user_id, {"action": "bind_account", "step": "username"}, flow_scope=flow_scope)
        return "请输入verse用户名。"

    if message.startswith("绑定 "):
        bind_args = message[len("绑定 ") :].strip().split()
        if not bind_args:
            _set_flow(bot_platform, bot_user_id, {"action": "bind_account", "step": "username"}, flow_scope=flow_scope)
            return "请输入verse用户名。"
        if bind_args[0].lower() == "2048verse":
            account_key = bind_args[1].strip() if len(bind_args) > 1 else ""
            bind_pin = bind_args[2].strip() if len(bind_args) > 2 else ""
        else:
            account_key = bind_args[0].strip()
            bind_pin = bind_args[1].strip() if len(bind_args) > 1 else ""
        if not account_key:
            _set_flow(bot_platform, bot_user_id, {"action": "bind_account", "step": "username"}, flow_scope=flow_scope)
            return "请输入verse用户名。"
        if not bind_pin:
            _set_flow(
                bot_platform,
                bot_user_id,
                {"action": "bind_account", "step": "pin", "username": account_key},
                flow_scope=flow_scope,
            )
            return "请输入4-5位绑定密码。"
        try:
            result = _profile_sync_step(
                steps,
                "bind_bot_account",
                lambda: bind_bot_account(
                    connection,
                    bot_platform=bot_platform,
                    bot_user_id=bot_user_id,
                    game_platform="2048verse",
                    account_key=account_key,
                    bind_pin=bind_pin,
                    metadata={"source": "private_bot"},
                ),
            )
        except ValueError as exc:
            return str(exc)
        return "已绑定 {} 账号：{}\n发送 help 查看可用命令。".format(result["game_platform"], result["account_key"])

    bind_flow_reply = _profile_sync_step(
        steps,
        "handle_bind_flow",
        lambda: _handle_bind_flow(
            connection,
            bot_platform=bot_platform,
            bot_user_id=bot_user_id,
            text=message,
            flow_scope=flow_scope,
        ),
    )
    if bind_flow_reply is not None:
        return bind_flow_reply

    verse_query_reply = _profile_sync_step(
        steps,
        "handle_verse_query_message",
        lambda: handle_verse_query_message(
            connection,
            bot_platform=bot_platform,
            bot_user_id=bot_user_id,
            text=message,
        ),
    )
    if verse_query_reply is not None:
        return verse_query_reply

    binding = _profile_sync_step(
        steps,
        "get_bot_binding",
        lambda: get_bot_binding(connection, bot_platform=bot_platform, bot_user_id=bot_user_id),
    )
    if binding is None:
        if file_segments:
            return "你还没有绑定账号。请先发送：绑定"
        return "你还没有绑定账号。请先发送：绑定"

    dashboard_flow_reply = _handle_dashboard_flow(
        connection,
        binding=binding,
        bot_platform=bot_platform,
        bot_user_id=bot_user_id,
        text=message,
        flow_scope=flow_scope,
    )
    if dashboard_flow_reply is not None:
        return dashboard_flow_reply

    if message in {"看板", "设置看板"}:
        _set_flow(
            bot_platform,
            bot_user_id,
            {"action": "dashboard_edit", "step": "content"},
            flow_scope=flow_scope,
        )
        current = get_player_dashboard(
            connection,
            game_platform=binding["game_platform"],
            player_id=binding["player_id"],
        )
        if current is None:
            return (
                "请直接发送个人看板完整内容，最多 10 行、总非空格字符不超过 200。\n"
                "发送“清空”可删除当前看板，发送“取消”可退出。"
            )
        return (
            "当前个人看板\n"
            "{content}\n\n"
            "请直接发送新的完整内容更新，最多 10 行、总非空格字符不超过 200。\n"
            "发送“清空”可删除当前看板，发送“取消”可退出。"
        ).format(content=current["dashboard_text"])

    if message in {"查看看板"}:
        current = get_player_dashboard(
            connection,
            game_platform=binding["game_platform"],
            player_id=binding["player_id"],
        )
        if current is None:
            return "当前还没有设置个人看板。发送“看板”开始设置。"
        return "当前个人看板\n{}".format(current["dashboard_text"])

    if message in {"清空看板"}:
        clear_player_dashboard(
            connection,
            game_platform=binding["game_platform"],
            player_id=binding["player_id"],
        )
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "个人看板已清空。"

    if message.startswith("看板 "):
        suffix = message[len("看板 ") :].strip()
        if suffix in {"查看", "预览"}:
            current = get_player_dashboard(
                connection,
                game_platform=binding["game_platform"],
                player_id=binding["player_id"],
            )
            if current is None:
                return "当前还没有设置个人看板。发送“看板”开始设置。"
            return "当前个人看板\n{}".format(current["dashboard_text"])
        if suffix in {"清空", "删除"}:
            clear_player_dashboard(
                connection,
                game_platform=binding["game_platform"],
                player_id=binding["player_id"],
            )
            _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
            return "个人看板已清空。"
        try:
            payload = set_player_dashboard(
                connection,
                game_platform=binding["game_platform"],
                player_id=binding["player_id"],
                dashboard_text=suffix,
            )
        except ValueError as exc:
            return "个人看板设置失败：{}".format(exc)
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return (
            "个人看板已更新。\n"
            "行数：{line_count}/10\n"
            "非空格字符：{nonspace}/200\n"
            "群里可发送“@bot 你的用户名”查看。"
        ).format(
            line_count=payload["line_count"],
            nonspace=payload["nonspace_char_count"],
        )

    registration_flow_reply = _profile_sync_step(
        steps,
        "handle_registration_flow",
        lambda: _handle_registration_flow(
            connection,
            binding=binding,
            bot_platform=bot_platform,
            bot_user_id=bot_user_id,
            text=message,
            flow_scope=flow_scope,
        ),
    )
    if registration_flow_reply is not None:
        return registration_flow_reply

    flow_reply = _profile_sync_step(
        steps,
        "handle_submit_flow",
        lambda: _handle_submit_flow(
            connection,
            binding=binding,
            bot_platform=bot_platform,
            bot_user_id=bot_user_id,
            text=message,
            flow_scope=flow_scope,
        ),
    )
    if flow_reply is not None:
        return flow_reply

    lock_flow_reply = _profile_sync_step(
        steps,
        "handle_floor_or_finish_flow",
        lambda: _handle_floor_or_finish_flow(
            connection,
            binding=binding,
            bot_platform=bot_platform,
            bot_user_id=bot_user_id,
            text=message,
            file_segments=file_segments,
            flow_scope=flow_scope,
        ),
    )
    if lock_flow_reply is not None:
        return lock_flow_reply

    if message in {"取消", "cancel", "CANCEL"}:
        _clear_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
        return "当前没有进行中的流程。"

    try:
        if message.startswith("提交成绩 ") or message.startswith("提交 "):
            prefix = "提交成绩 " if message.startswith("提交成绩 ") else "提交 "
            fields = _parse_kv_text(message[len(prefix) :])
            try:
                ended_at = _normalize_time(fields.get("ended_at"), "ended_at")
                started_at = _normalize_time(fields.get("started_at"), "started_at")
            except ValueError:
                return "时间格式不对。请按 YYYY-MM-DD HH:MM:SS 输入，例如 2026-05-22 21:30:00。"
            event_code = _resolve_event(connection, binding["game_platform"], fields.get("event"))
            raw_score = _parse_int_or_none(fields.get("raw_score"), "raw_score")
            final_score = _parse_int_or_none(fields.get("final_score") or fields.get("score"), "final_score")
            competition_score = _parse_int_or_none(fields.get("competition_score"), "competition_score")
            primary_time_ms = _parse_int_or_none(fields.get("primary_time_ms"), "primary_time_ms")
            target_tile_value = _parse_int_or_none(fields.get("target_tile_value"), "target_tile_value")
            score_before_target = _parse_int_or_none(fields.get("score_before_target"), "score_before_target")
            if raw_score is None and final_score is None and competition_score is None and primary_time_ms is None:
                return "提交失败：至少需要一种成绩字段，例如 score=12345 或 primary_time_ms=54321"
            evidence = {"note": fields.get("note")} if fields.get("note") else None
            submit_outcome = _profile_sync_step(
                steps,
                "submit_score_by_event_mode",
                lambda: _submit_score_by_event_mode(
                    connection,
                    event_code=event_code,
                    username=binding["account_key"],
                    display_name=binding["display_name"],
                    submitter_platform=bot_platform,
                    submitter_account=bot_user_id,
                    source_record_id=fields.get("source_record_id"),
                    started_at=started_at,
                    ended_at=ended_at,
                    raw_score=raw_score,
                    final_score=final_score,
                    competition_score=competition_score,
                    primary_time_ms=primary_time_ms,
                    target_tile_value=target_tile_value,
                    score_before_target=score_before_target,
                    evidence=evidence,
                    payload={"source": "private_bot_command", "raw_text": message},
                ),
            )
            score_text = competition_score if competition_score is not None else final_score if final_score is not None else raw_score if raw_score is not None else primary_time_ms
            if submit_outcome["mode"] == "direct":
                direct = submit_outcome["result"]
                return "成绩已直通入榜（无需审核）。\n成绩={}\n终局时间={}\nrecord={}".format(
                    score_text,
                    ended_at,
                    direct["source_record_id"],
                )
            result = submit_outcome["result"]
            return "成绩已提交，等待审核。\nsubmission={}\n成绩={}\n终局时间={}".format(
                result["submission_id"],
                score_text,
                ended_at,
            )

        if message in {"提交成绩", "提交"}:
            if not BOT_SUBMIT_SCORE_ENABLED:
                return "当前已关闭 bot 提交成绩入口，请改用 Hub 手动录入。"
            events = _profile_sync_step(
                steps,
                "list_open_events_for_submit",
                lambda: _list_open_events(connection, binding["game_platform"], player_id=binding["player_id"]),
            )
            _set_flow(
                bot_platform,
                bot_user_id,
                {"action": "submit_score", "step": "event", "data": {}, "candidate_event_codes": [row["event_code"] for row in events]},
                flow_scope=flow_scope,
            )
            if events:
                return "开始提交成绩。\n请选择赛事序号。\n" + _format_events_for_dm(events)
            return "开始提交成绩。\n当前没有可提交成绩的赛事。"

        if message == "floor":
            if not BOT_LOCK_UPLOAD_ENABLED:
                return "当前已关闭 bot 锁局上传入口，请改用 Hub 手动处理。"
            rows = _profile_sync_step(steps, "list_lockable_registered_events", lambda: _list_lockable_registered_events(connection, binding))
            if not rows:
                return "你当前没有可锁局的赛事。"
            _set_flow(bot_platform, bot_user_id, {"action": "floor_select_event"}, flow_scope=flow_scope)
            return "请选择要锁局的赛事序号。\n" + _as_numbered_lines(rows)

        if message == "finish":
            if not BOT_LOCK_UPLOAD_ENABLED:
                return "当前已关闭 bot 锁局上传入口，请改用 Hub 手动处理。"
            sessions = _profile_sync_step(steps, "list_open_finish_sessions", lambda: _list_open_finish_sessions(connection, binding))
            if not sessions:
                return "你当前没有等待提交终局回放的锁局。"
            _set_flow(
                bot_platform,
                bot_user_id,
                {"action": "finish_select_session", "candidate_session_ids": [row["id"] for row in sessions]},
                flow_scope=flow_scope,
            )
            lines = []
            for index, row in enumerate(sessions, start=1):
                lines.append(
                    "{}. {} | floor at {}".format(
                        index,
                        row["event_name"],
                        row["start_command_time"],
                    )
                )
            return "请选择要提交终局回放的序号。\n" + "\n".join(lines)

        if message.startswith("我的提交"):
            explicit_event_code = None
            if " " in message:
                rest = message.split(" ", 1)[1].strip()
                if rest:
                    fields = _parse_kv_text(rest)
                    explicit_event_code = fields.get("event")
            rows = _profile_sync_step(
                steps,
                "load_my_submissions",
                lambda: _load_my_submissions(connection, binding, explicit_event_code=explicit_event_code),
            )
            return _format_pending_rows(rows)

        if message == "我的档案":
            profile = _profile_sync_step(steps, "build_player_profile", lambda: build_player_profile(connection, binding["player_id"]))
            return format_player_profile(profile)

        if message in {"赛事", "赛事列表", "查看赛事"}:
            _profile_sync_step(steps, "maybe_settle_due_reservations", lambda: _maybe_settle_due_reservations(connection))
            rows = _profile_sync_step(
                steps,
                "list_recent_events",
                lambda: _list_recent_events(connection, binding["game_platform"], player_id=binding["player_id"], limit=10),
            )
            return _format_events_for_dm(rows)

        if message in {"归档比赛", "查看归档比赛"}:
            flow = {"action": "my_scores_browse", "archived_only": True, "month_key": None, "offset": 0}
            _set_flow(bot_platform, bot_user_id, flow, flow_scope=flow_scope)
            return _profile_sync_step(steps, "build_archived_score_page_reply", lambda: _build_my_score_page_reply(connection, binding, flow))

        if message == "我的报名":
            _profile_sync_step(steps, "maybe_settle_due_reservations", lambda: _maybe_settle_due_reservations(connection))
            rows = _profile_sync_step(
                steps,
                "list_player_registrations",
                lambda: list_player_registrations(connection, binding["player_id"], platform_code=binding["game_platform"], active_only=True),
            )
            return _format_my_registrations(rows)

        if message == "我的限时预约":
            _profile_sync_step(steps, "maybe_settle_due_reservations", lambda: _maybe_settle_due_reservations(connection))
            rows = _profile_sync_step(
                steps,
                "list_open_events_for_reservation",
                lambda: _list_open_events(connection, binding["game_platform"], player_id=binding["player_id"], limit=20),
            )
            if not rows:
                return "当前没有可查看预约的进行中/待开始赛事。"
            for row in rows:
                if not _is_reservation_timed_event(connection, row["event_code"]):
                    continue
                reservation = _profile_sync_step(
                    steps,
                    "get_my_reservation",
                    lambda row=row: get_my_reservation(
                        connection,
                        event_code=row["event_code"],
                        bot_platform=bot_platform,
                        bot_user_id=bot_user_id,
                    ),
                )
                if reservation is None:
                    continue
                if (reservation["status"] or "").lower() not in {"reserved", "confirmed_late"}:
                    continue
                return "我的限时预约\n{} | {} -> {} | 状态 {}".format(
                    row["event_name"],
                    reservation["reserved_start_time"],
                    reservation["reserved_end_time"],
                    _reservation_status_label(
                        reservation["status"],
                        reservation["reserved_start_time"],
                        reservation["reserved_end_time"],
                    ),
                )
            return "你当前没有有效的限时预约。"

        if message in {"我的限时赛成绩", "我的成绩"}:
            flow = {"action": "my_scores_browse", "archived_only": False, "month_key": None, "offset": 0}
            _set_flow(bot_platform, bot_user_id, flow, flow_scope=flow_scope)
            return _profile_sync_step(steps, "build_my_score_page_reply", lambda: _build_my_score_page_reply(connection, binding, flow))

        if message == "预约":
            rows = _profile_sync_step(
                steps,
                "load_event_rows_for_reservation",
                lambda: _load_event_rows(
                    connection,
                    platform_code=binding["game_platform"],
                    player_id=binding["player_id"],
                    include_finished=True,
                    include_archived=False,
                    include_history_hidden=False,
                ),
            )
            rows = [row for row in rows if _is_reservation_timed_event(connection, row["event_code"])]
            rows = [row for row in rows if row.get("registration_status") == "active"]
            if not rows:
                return "你还没有报名任何预约型限时赛。请先发送“报名”。"
            _set_flow(
                bot_platform,
                bot_user_id,
                {"action": "reservation_hub_menu"},
                flow_scope=flow_scope,
            )
            return (
                "预约功能\n"
                "1. 预约限时赛\n"
                "2. 查看我的预约\n"
                "3. 修改限时预约\n"
                "4. 取消限时预约\n"
                "请输入序号。"
            )

        if message in {"预约限时赛", "修改限时预约"}:
            rows = _profile_sync_step(
                steps,
                "list_reservable_events",
                lambda: _list_reservable_events(connection, binding["game_platform"], player_id=binding["player_id"], limit=20),
            )
            if not rows:
                return "当前没有可预约的限时赛。"
            _set_flow(
                bot_platform,
                bot_user_id,
                {
                    "action": "reserve_timed_event",
                    "candidate_event_codes": [row["event_code"] for row in rows],
                    "step": "event",
                },
                flow_scope=flow_scope,
            )
            return "请输入要预约的赛事序号。\n" + _format_events_for_dm(rows)

        if message == "取消限时预约":
            rows = _profile_sync_step(
                steps,
                "list_reservable_events",
                lambda: _list_reservable_events(connection, binding["game_platform"], player_id=binding["player_id"], limit=20),
            )
            if not rows:
                return "当前没有可取消预约的限时赛。"
            _set_flow(
                bot_platform,
                bot_user_id,
                {
                    "action": "cancel_timed_reservation",
                    "candidate_event_codes": [row["event_code"] for row in rows],
                    "step": "event",
                },
                flow_scope=flow_scope,
            )
            return "请输入要取消预约的赛事序号。\n" + _format_events_for_dm(rows)

        if message.startswith("报名"):
            if message == "报名":
                rows = _profile_sync_step(
                    steps,
                    "list_open_events_for_register",
                    lambda: _list_open_events(connection, binding["game_platform"], player_id=binding["player_id"], limit=10),
                )
                if not rows:
                    return "当前没有可报名赛事。"
                _set_flow(
                    bot_platform,
                    bot_user_id,
                    {
                        "action": "register_event",
                        "candidate_event_codes": [row["event_code"] for row in rows],
                    },
                    flow_scope=flow_scope,
                )
                return (
                    "可报名项目\n"
                    + _as_numbered_lines(rows)
                    + "\n\n其余项目当前不可报名。\n提示：已开始的比赛不可取消报名。\n请输入要报名的赛事序号。"
                )
            fields = _parse_kv_text(message[len("报名") :].strip())
            event_code = fields.get("event")
            if not event_code:
                return "请先发送“报名”，再按提示输入赛事序号。"
            if is_registered(connection, event_code, binding["account_key"]):
                return "你已经报名了这场赛事。"
            try:
                result = _profile_sync_step(
                    steps,
                    "register_player",
                    lambda: register_player(
                        connection,
                        event_code,
                        binding["account_key"],
                        display_name=binding["display_name"],
                        registered_via="qq_private_bot",
                        metadata={"bot_platform": bot_platform, "bot_user_id": bot_user_id},
                    ),
                )
            except ValueError as exc:
                return "报名失败：{}".format(exc)
            extra = ""
            if _is_reservation_timed_event(connection, event_code):
                extra = "\n你已报名，可发送“预约限时赛”进行预约。"
            return "报名成功。\n{} ({}){}".format(result["display_name"], result["username"], extra)

        if message.startswith("取消报名"):
            fields_text = message[len("取消报名") :].strip()
            if not fields_text:
                rows = _profile_sync_step(
                    steps,
                    "list_player_registrations_for_cancel",
                    lambda: list_player_registrations(connection, binding["player_id"], platform_code=binding["game_platform"], active_only=True),
                )
                rows = [row for row in rows if _is_registration_cancellable(row)]
                if not rows:
                    return "你当前没有可取消的报名项目。"
                lines = ["{}. {} | {}".format(i + 1, row["event_name"], _format_event_time_range(row)) for i, row in enumerate(rows)]
                _set_flow(
                    bot_platform,
                    bot_user_id,
                    {
                        "action": "cancel_registration",
                        "candidate_event_codes": [row["event_code"] for row in rows],
                    },
                    flow_scope=flow_scope,
                )
                return (
                    "你已报名项目\n"
                    + "\n".join(lines)
                    + "\n\n请输入要取消的赛事序号。\n提示：已开始的比赛不可取消报名。"
                )
            fields = _parse_kv_text(fields_text)
            event_code = fields.get("event")
            if not event_code:
                return "请先发送“取消报名”，再按提示输入赛事序号。"
            if not is_registered(connection, event_code, binding["account_key"]):
                return "你当前不在这场赛事的报名名单中。"
            try:
                _profile_sync_step(steps, "cancel_registration", lambda: cancel_registration(connection, event_code, binding["account_key"]))
            except ValueError as exc:
                return "取消报名失败：{}".format(exc)
            return "已取消报名。"

        if file_segments:
            return "我收到了文件，但当前没有等待中的上传流程。\n如果你要锁局，请先发送 floor 或 finish。"

        return "未识别的命令。发送 help 查看可用命令。"
    finally:
        total_elapsed_ms = int((perf_counter() - started) * 1000)
        if total_elapsed_ms >= BOT_PRIVATE_SLOW_LOG_MS:
            LOGGER.warning(
                "bot_private slow total_ms=%s bot_platform=%s bot_user_id=%s flow_scope=%s text=%r steps=%s",
                total_elapsed_ms,
                bot_platform,
                bot_user_id,
                flow_scope or "-",
                message,
                ",".join("{}:{}ms".format(name, elapsed_ms) for name, elapsed_ms in steps) or "-",
            )


def handle_group_message(
    connection,
    *,
    bot_platform,
    bot_user_id,
    group_id,
    text,
    message_segments=None,
    is_at_bot=True,
):
    if not GROUP_CHAT_ENABLED:
        _append_group_debug("drop reason=group_disabled group_id={} user_id={}".format(group_id, bot_user_id))
        return None

    group_id = str(group_id)
    bot_user_id = str(bot_user_id)
    message = (text or "").strip()
    flow_scope = _group_flow_scope(group_id)
    existing_flow = _get_flow(bot_platform, bot_user_id, flow_scope=flow_scope)
    _append_group_debug(
        "recv group_id={} user_id={} at_bot={} has_flow={} text={!r}".format(
            group_id,
            bot_user_id,
            bool(is_at_bot),
            existing_flow is not None,
            message,
        )
    )

    if group_id not in GROUP_CHAT_WHITELIST:
        _append_group_debug("drop reason=whitelist group_id={} user_id={}".format(group_id, bot_user_id))
        return None
    if not is_at_bot:
        _append_group_debug("drop reason=not_at_bot group_id={} user_id={}".format(group_id, bot_user_id))
        return None
    if not message:
        _append_group_debug("reply reason=empty_text_help group_id={} user_id={}".format(group_id, bot_user_id))
        return _group_help_summary()
    if message.startswith("绑定"):
        _append_group_debug("reply reason=bind_private_only group_id={} user_id={} text={!r}".format(group_id, bot_user_id, message))
        return "绑定仅限私聊，请私聊 bot 发送：绑定 你的verse用户名"
    if _group_is_forbidden_command(message):
        _append_group_debug("reply reason=forbidden_command group_id={} user_id={} text={!r}".format(group_id, bot_user_id, message))
        return "该指令仅限私聊使用，输入help查看帮助。"
    if not _group_is_allowed_command(message, has_flow=existing_flow is not None):
        _append_group_debug("reply reason=not_allowed group_id={} user_id={} text={!r}".format(group_id, bot_user_id, message))
        return "无效指令，输入help查看帮助。"
    if _group_should_consume_rate_limit(message, has_flow=existing_flow is not None) and not _consume_group_rate_limit(group_id, bot_user_id):
        _append_group_debug("reply reason=rate_limit group_id={} user_id={}".format(group_id, bot_user_id))
        return "发送太快了，请稍后再试。"
    if _group_should_consume_rate_limit(message, has_flow=existing_flow is not None) and not _consume_group_global_rate_limit(group_id):
        _append_group_debug("reply reason=global_rate_limit group_id={} user_id={}".format(group_id, bot_user_id))
        return "群里请求太多了，请稍后再试。"

    if message.lower() in {"help", "帮助", "菜单"}:
        _append_group_debug("reply reason=group_help group_id={} user_id={}".format(group_id, bot_user_id))
        return _group_help_summary()

    dashboard_reply = _handle_group_dashboard_query(connection, message=message)
    if dashboard_reply is not None:
        _append_group_debug("reply reason=dashboard_query group_id={} user_id={} text={!r} reply={!r}".format(group_id, bot_user_id, message, str(dashboard_reply or "").strip()))
        return dashboard_reply

    if not is_verse_query_message(message):
        binding = get_bot_binding(connection, bot_platform=bot_platform, bot_user_id=bot_user_id)
        if binding is None:
            _append_group_debug("reply reason=not_bound group_id={} user_id={} text={!r}".format(group_id, bot_user_id, message))
            return "你还没有绑定账号。请私聊 bot 发送：绑定 你的verse用户名"

    reply_slot_acquired = False
    if _group_should_consume_rate_limit(message, has_flow=existing_flow is not None):
        if not _acquire_group_reply_slot(group_id):
            _append_group_debug("reply reason=reply_slot_limit group_id={} user_id={}".format(group_id, bot_user_id))
            return "群里当前正在处理的请求太多，请稍后再试。"
        reply_slot_acquired = True
    try:
        reply = handle_private_message(
            connection,
            bot_platform=bot_platform,
            bot_user_id=bot_user_id,
            text=message,
            message_segments=message_segments,
            flow_scope=flow_scope,
        )
    finally:
        if reply_slot_acquired:
            _release_group_reply_slot(group_id)
    if _group_should_redirect_reply(reply) and not _group_should_keep_direct_reply(message, existing_flow):
        _append_group_debug("reply reason=redirect_long group_id={} user_id={} text={!r}".format(group_id, bot_user_id, message))
        return _group_long_reply_summary(message)
    _append_group_debug("reply reason=direct group_id={} user_id={} text={!r} reply={!r}".format(group_id, bot_user_id, message, str(reply or "").strip()))
    return str(reply or "").strip()
