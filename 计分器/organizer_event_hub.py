from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys


BASE_DIR = Path(__file__).resolve().parent
HUB_DIR = BASE_DIR.parent
if str(HUB_DIR) not in sys.path:
    sys.path.insert(0, str(HUB_DIR))

from tournament_common import load_players

from bootstrap import bootstrap_all
from db import connect, ensure_parent_dir, initialize_schema, transaction
from services.attempt_bind_service import bind_attempt_record, find_attempt_candidates_by_result, list_attempt_candidates
from services.attempt_service import process_open_attempt_sessions, start_attempt_session
from services.competition_mode_service import aggregation_label, get_competition_mode, get_target, get_variant, list_competition_modes
from services.competition_mode_service import event_uses_locking
from services.discord_lock_refresh_service import record_score_from_discord_final_replay, refresh_locking_player_scores_from_discord
from services.event_admin_service import create_or_update_event, infer_rating_bucket_code
from services.event_edit_service import update_event_basic_info
from services.event_progress_service import build_event_progress_snapshot, get_player_progress_detail
from services.ingest_service import ingest_organizer_json_file
from services.interim_ranking_service import export_interim_ranking_image, preview_ranking
from services.pending_score_review_service import (
    approve_pending_score,
    list_pending_scores,
    reject_pending_score,
    submit_pending_score,
)
from services.player_profile_service import (
    build_player_profile,
    export_player_profile,
    find_players,
    format_player_profile,
)
from services.replay_lock_service import (
    attach_early_replay,
    attach_final_replay,
    get_replay_chain_status,
    run_replay_prefix_check,
    set_replay_review_status,
)
from services.verse_adapter import (
    fetch_recent_games,
    get_game_end_time,
    get_game_start_time,
    get_game_terminal_board_sum,
    inspect_recent_game_fields,
)
from services.rating_service import (
    export_leaderboards,
    list_performance_leaderboard,
    list_rating_buckets,
    list_rating_leaderboard,
    recalculate_event_bucket_ratings,
    recalculate_ratings,
)
from services.raw_score_export_service import export_final_ranking
from services.raw_score_import_service import add_manual_score, import_score_file, upsert_event_attempt_record, upsert_performance_record
from services.registration_service import cancel_registration, is_registered, list_registrations, register_player, update_registered_player_username
from services.score_record_admin_service import list_event_score_records, void_score_record
from services.settlement_precheck_service import run_settlement_precheck
from services.settlement_service import list_suspicious_locking_records, settle_event
from services.timed_reservation_service import list_event_reservation_status, reserve_for_registered_player, settle_due_reservations
from settings import DATABASE_PATH


LOCAL_TIMEZONE = timezone(timedelta(hours=8))
ATTEMPT_STATUS_LABELS = {
    "pending_lock": "等待识别",
    "locked_in_progress": "已识别，待完赛",
    "completed": "已完成",
    "expired": "已过期",
    "cancelled": "已取消",
}
RECENT_EVENT_VISIBLE_DAYS = 7


def configure_console():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            continue


def ensure_hub_ready():
    ensure_parent_dir(DATABASE_PATH)
    connection = connect(DATABASE_PATH)
    with transaction(connection):
        initialize_schema(connection)
        bootstrap_all(connection)
    return connection


def prompt(text, default=None, allow_empty=False, allow_cancel=False):
    suffix = ""
    if default not in (None, ""):
        suffix = " [{}]".format(default)
    if allow_cancel:
        suffix = "{}{}".format(suffix, "（输入0取消）")
    value = input("{}{}: ".format(text, suffix)).strip()
    if allow_cancel and value == "0":
        return None
    if value:
        return value
    if default is not None:
        return default
    if allow_empty:
        return ""
    return prompt(text, default=default, allow_empty=allow_empty, allow_cancel=allow_cancel)


def prompt_int(text, default=None, allow_empty=False, allow_cancel=False):
    value = prompt(text, default=default, allow_empty=allow_empty, allow_cancel=allow_cancel)
    if value is None:
        return None
    if value == "" and allow_empty:
        return None
    try:
        return int(value)
    except ValueError:
        print("请输入整数。")
        return prompt_int(text, default=default, allow_empty=allow_empty, allow_cancel=allow_cancel)


def prompt_optional_int(text):
    value = prompt(text, allow_empty=True, allow_cancel=True)
    if value is None:
        return None
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        print("请输入整数，或直接回车留空。")
        return prompt_optional_int(text)


def prompt_event_time(text):
    while True:
        value = prompt("{}（格式 YYYY-MM-DD HH:MM:SS）".format(text), allow_cancel=True)
        if value is None:
            return None
        try:
            parse_event_time(value)
            return value
        except ValueError:
            print("时间格式不正确，请按 YYYY-MM-DD HH:MM:SS 输入，例如 2026-05-01 20:00:00。")


def prompt_duration_minutes(text="持续时间（分钟）", default=60):
    while True:
        value = prompt_int(text, default, allow_cancel=True)
        if value is None:
            return None
        if value <= 0:
            print("持续时间必须是正整数分钟。")
            continue
        return value


def prompt_optional_event_time(text):
    while True:
        value = prompt("{}（可空，格式 YYYY-MM-DD HH:MM:SS）".format(text), allow_empty=True, allow_cancel=True)
        if value is None:
            return None
        if not value:
            return None
        try:
            parse_event_time(value)
            return value
        except ValueError:
            print("时间格式不正确，请按 YYYY-MM-DD HH:MM:SS 输入，或直接回车留空。")


def parse_deadline_offset_input(value):
    text = (value or "").strip()
    if not text:
        return timedelta(0)
    if text.startswith("+"):
        raise ValueError("不支持带 + 号，请输入 HH:MM 或 -HH:MM。")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if ":" not in text:
        raise ValueError("格式错误，请输入 HH:MM 或 -HH:MM。")
    hh, mm = text.split(":", 1)
    if not (hh.isdigit() and mm.isdigit()):
        raise ValueError("格式错误，请输入 HH:MM 或 -HH:MM。")
    hours = int(hh)
    minutes = int(mm)
    if minutes >= 60:
        raise ValueError("分钟必须小于60。")
    delta = timedelta(hours=hours, minutes=minutes)
    return -delta if negative else delta


def prompt_registration_deadline(start_time_dt, *, force_at_start=False):
    if force_at_start:
        return start_time_dt.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"), "00:00"
    while True:
        raw = prompt("报名截止偏移（-HH:MM / HH:MM / 回车=00:00）", allow_empty=True, allow_cancel=True)
        if raw is None:
            return None, None
        try:
            offset = parse_deadline_offset_input(raw)
        except ValueError as exc:
            print(str(exc))
            continue
        deadline = start_time_dt + offset
        return deadline.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"), (raw.strip() or "00:00")


def prompt_event_time_with_default(text, default_value):
    while True:
        value = prompt("{}（格式 YYYY-MM-DD HH:MM:SS）".format(text), default_value, allow_cancel=True)
        if value is None:
            return None
        try:
            parse_event_time(value)
            return value
        except ValueError:
            print("时间格式不正确，请按 YYYY-MM-DD HH:MM:SS 输入。")


def prompt_yes_no(text, default=True):
    default_text = "Y/n" if default else "y/N"
    value = input("{} [{}]: ".format(text, default_text)).strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "1", "是", "对"}


def prompt_official_and_rated_defaults():
    is_official = prompt_yes_no("是否正式赛", True)
    rated_default = True if is_official else False
    is_rated = prompt_yes_no("是否计 rating", rated_default)
    return is_official, is_rated


def choose_from_list(title, options, default_index=0):
    while True:
        print("")
        print(title)
        for index, option in enumerate(options, start=1):
            print("{}. {}".format(index, option["label"]))
        print("0. 返回")
        default_value = str(default_index + 1)
        choice = prompt("请选择", default_value)
        if choice == "0":
            return None
        try:
            selected = int(choice) - 1
        except ValueError:
            print("请输入序号。")
            continue
        if selected < 0 or selected >= len(options):
            print("超出范围，请重新选择。")
            continue
        return options[selected]["value"]


def sanitize_code_part(value):
    cleaned = []
    for char in value.lower():
        if char.isalnum():
            cleaned.append(char)
        elif char in {" ", "-", "_"}:
            cleaned.append("_")
    text = "".join(cleaned).strip("_")
    while "__" in text:
        text = text.replace("__", "_")
    return text or "event"


def build_event_code(name, start_time_text):
    name_part = sanitize_code_part(name)
    compact = "".join(char for char in start_time_text if char.isdigit())
    compact = compact[:8] if len(compact) >= 8 else compact
    if compact:
        return "{}_{}".format(name_part, compact)
    return name_part


def parse_metadata_json(raw_value):
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def event_remark(row):
    metadata_json = row["metadata_json"] if row is not None and "metadata_json" in row.keys() else None
    metadata = parse_metadata_json(metadata_json)
    value = metadata.get("remark")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_event_time(value):
    if not value:
        return None
    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S%z",
    ]
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
        return parsed.astimezone(LOCAL_TIMEZONE)
    except ValueError:
        pass
    for fmt in candidates:
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
            return parsed.astimezone(LOCAL_TIMEZONE)
        except ValueError:
            continue
    raise ValueError("Unsupported event time format: {}".format(value))


def get_event_runtime_status(row):
    now = datetime.now(LOCAL_TIMEZONE)
    start_time = parse_event_time(row["start_time"])
    end_time = parse_event_time(row["end_time"])
    if start_time and now < start_time:
        return "待开始"
    if end_time and now > end_time:
        return "已完赛"
    if start_time or end_time:
        return "进行中"
    return "未设时间"


def format_display_time(value):
    if not value:
        return "-"
    parsed = parse_event_time(value)
    return parsed.strftime("%m-%d %H:%M")


def event_supports_locking(row):
    return event_uses_locking(row["competition_type"], row["platform_code"], row["variant_code"])


def is_reservation_timed_event(row):
    if row["competition_type"] != "timed_scoring":
        return False
    metadata = parse_metadata_json(row["metadata_json"])
    return metadata.get("timed_mode") == "reservation"


def event_supports_manual_verse_refresh(row):
    if row["platform_code"] != "2048verse":
        return False
    return bool(row["variant_code"])


def _is_hidden_by_time(row):
    end_time = parse_event_time(row["end_time"]) if row["end_time"] else None
    if end_time is None:
        return False
    now = datetime.now(LOCAL_TIMEZONE)
    return (now - end_time) > timedelta(days=RECENT_EVENT_VISIBLE_DAYS)


def fetch_recent_events(connection, limit=30, include_hidden=False, include_archived=False):
    rows = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            p.code AS platform_code,
            v.code AS variant_code,
            e.competition_type,
            e.metadata_json,
            e.start_time,
            e.end_time,
            e.status,
            (SELECT COUNT(*) FROM registrations r WHERE r.event_id = e.id AND r.status = 'active') AS registration_count,
            (SELECT COUNT(*) FROM event_results er WHERE er.event_id = e.id) AS result_count,
            (SELECT COUNT(*) FROM attempt_sessions s WHERE s.event_id = e.id AND s.status = 'pending_lock') AS pending_lock_count,
            (SELECT COUNT(*) FROM attempt_sessions s WHERE s.event_id = e.id AND s.status = 'locked_in_progress') AS locked_in_progress_count
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        ORDER BY COALESCE(e.start_time, '') DESC, e.event_code DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    grouped = {"进行中": [], "待开始": [], "已完赛": [], "未设时间": []}
    for row in rows:
        if row["status"] == "archived":
            if include_archived:
                grouped.setdefault("已归档", []).append(row)
            continue
        runtime_status = get_event_runtime_status(row)
        if runtime_status == "已完赛" and _is_hidden_by_time(row):
            if include_hidden:
                grouped.setdefault("历史隐藏", []).append(row)
            continue
        grouped[runtime_status].append(row)
    return grouped


def fetch_event_summary(connection, event_code):
    event = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            p.code AS platform_code,
            v.code AS variant_code,
            e.event_type,
            e.competition_type,
            e.status,
            e.is_official,
            e.is_rated,
            e.registration_close_time,
            e.metadata_json,
            e.start_time,
            e.end_time
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if event is None:
        raise ValueError("赛事不存在: {}".format(event_code))

    counts = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM registrations WHERE event_id = e.id AND status = 'active') AS registration_count,
            (SELECT COUNT(*) FROM event_attempt_records WHERE event_id = e.id) AS attempt_record_count,
            (SELECT COUNT(*) FROM event_results WHERE event_id = e.id) AS result_count,
            (SELECT COUNT(*) FROM attempt_sessions WHERE event_id = e.id AND status = 'pending_lock') AS pending_lock_count,
            (SELECT COUNT(*) FROM attempt_sessions WHERE event_id = e.id AND status = 'locked_in_progress') AS locked_in_progress_count,
            (SELECT COUNT(*) FROM attempt_sessions WHERE event_id = e.id AND status = 'completed') AS completed_lock_count
        FROM events e
        WHERE e.id = ?
        """,
        (event["id"],),
    ).fetchone()

    rule = connection.execute(
        """
        SELECT aggregation_method, rule_config_json
        FROM event_rule_sets
        WHERE event_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (event["id"],),
    ).fetchone()

    progress = build_event_progress_snapshot(connection, event_code)
    return {
        "event": event,
        "counts": counts,
        "rule": rule,
        "progress": progress,
        "runtime_status": get_event_runtime_status(event),
    }


def choose_event(connection, current_event_code=None):
    while True:
        grouped = fetch_recent_events(connection)
        total_count = sum(len(rows) for rows in grouped.values())
        if not total_count:
            print("当前还没有赛事。")
            return None
        print("")
        print("最近赛事")
        default_index = 0
        flat_rows = []
        for label in ("进行中", "待开始", "已完赛", "未设时间"):
            rows = grouped.get(label, [])
            if not rows:
                continue
            print("{}（{}）".format(label, len(rows)))
            for row in rows:
                flat_rows.append(row)
                index = len(flat_rows)
                print(
                    "{}. {} | {} | {} | {} -> {} | 报名{} 已结算{}{}".format(
                        index,
                        row["event_code"],
                        row["event_name"],
                        row["platform_code"],
                        row["start_time"] or "-",
                        row["end_time"] or "-",
                        row["registration_count"],
                        row["result_count"],
                        " | 当前" if row["event_code"] == current_event_code else "",
                    )
                )
                remark = event_remark(row)
                if remark:
                    print("   备注: {}".format(remark))
                if row["event_code"] == current_event_code:
                    default_index = index - 1
        options = [
            {
                "label": "{} | {} | {}".format(row["event_code"], row["event_name"], get_event_runtime_status(row)),
                "value": row["event_code"],
            }
            for row in flat_rows
        ]
        options.append({"label": "查看已归档赛事", "value": "__VIEW_ARCHIVED__"})
        selected = choose_from_list("选择一场比赛", options, default_index=default_index)
        if selected != "__VIEW_ARCHIVED__":
            return selected

        grouped_with_archived = fetch_recent_events(connection, include_archived=True)
        archived_rows = grouped_with_archived.get("已归档", [])
        if not archived_rows:
            print("当前没有已归档赛事。")
            continue
        archived_selected = choose_event_from_groups(
            {"已归档": archived_rows},
            "已归档赛事",
            current_event_code=current_event_code,
        )
        if archived_selected:
            return archived_selected


def choose_event_from_groups(grouped, title, current_event_code=None):
    total_count = sum(len(rows) for rows in grouped.values())
    if not total_count:
        print("没有可选赛事。")
        return None
    print("")
    print(title)
    flat_rows = []
    default_index = 0
    for label in ("进行中", "待开始", "已完赛", "未设时间", "历史隐藏", "已归档"):
        rows = grouped.get(label, [])
        if not rows:
            continue
        print("{}（{}）".format(label, len(rows)))
        for row in rows:
            flat_rows.append(row)
            index = len(flat_rows)
            print(
                "{}. {} | {} | {} | {} -> {} | 状态 {}".format(
                    index,
                    row["event_code"],
                    row["event_name"],
                    row["platform_code"],
                    row["start_time"] or "-",
                    row["end_time"] or "-",
                    row["status"],
                )
            )
            if row["event_code"] == current_event_code:
                default_index = index - 1
    return choose_from_list(
        "选择赛事",
        [{"label": "{} | {} | {}".format(row["event_code"], row["event_name"], row["status"]), "value": row["event_code"]} for row in flat_rows],
        default_index=default_index,
    )


def require_event(connection, current_event_code):
    if current_event_code:
        return current_event_code
    print("当前还没有选中比赛。")
    return choose_event(connection, current_event_code=current_event_code)


def print_header(current_event_code, current_event_name):
    print("")
    print("=== 原始分赛事主办方程序 ===")
    if current_event_code:
        print("当前比赛: {} | {}".format(current_event_code, current_event_name or "-"))
    else:
        print("当前比赛: 未选择")


def print_main_menu():
    print("1. 选择当前比赛")
    print("2. 新建比赛")
    print("3. 当前比赛工作台")
    print("4. 查看赛事列表")
    print("5. 长期统榜与 rating")
    print("6. 赛事归档与删除")
    print("0. 退出")


def attempt_status_label(status):
    return ATTEMPT_STATUS_LABELS.get(status, status or "-")


def parse_session_metadata(raw_value):
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def summarize_replay_chain(metadata):
    early = metadata.get("early_replay") if isinstance(metadata.get("early_replay"), dict) else None
    final = metadata.get("final_replay") if isinstance(metadata.get("final_replay"), dict) else None
    check = metadata.get("prefix_check") if isinstance(metadata.get("prefix_check"), dict) else None
    review = metadata.get("review") if isinstance(metadata.get("review"), dict) else None
    return {
        "early_status": "已录入" if early else "缺失",
        "final_status": "已录入" if final else "缺失",
        "prefix_status": (check.get("status") if check else "missing"),
        "review_status": (review.get("status") if review else "missing"),
    }


def show_attempt_sessions(connection, event_code):
    rows = connection.execute(
        """
        SELECT
            s.id,
            s.status,
            s.start_command_time,
            s.lock_deadline_time,
            s.completed_time,
            s.metadata_json,
            p.display_name,
            pa.account_key
        FROM attempt_sessions s
        JOIN players p ON p.id = s.player_id
        JOIN events e ON e.id = s.event_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = s.player_id
           AND pa.platform_id = e.platform_id
           AND pa.is_primary = 1
        WHERE e.event_code = ?
        ORDER BY
            CASE
                WHEN s.status = 'pending_lock' THEN 0
                WHEN s.status = 'locked_in_progress' THEN 1
                WHEN s.status = 'completed' THEN 2
                WHEN s.status = 'expired' THEN 3
                WHEN s.status = 'cancelled' THEN 4
                ELSE 5
            END,
            s.id DESC
        """,
        (event_code,),
    ).fetchall()
    if not rows:
        print("当前比赛还没有跟踪会话。")
        return []
    grouped = {
        "pending_lock": [],
        "locked_in_progress": [],
        "completed": [],
        "expired": [],
        "cancelled": [],
    }
    for row in rows:
        grouped.setdefault(row["status"], []).append(row)
    for status_key in ("pending_lock", "locked_in_progress", "completed", "expired", "cancelled"):
        status_rows = grouped.get(status_key, [])
        print("{}（{}）".format(attempt_status_label(status_key), len(status_rows)))
        if not status_rows:
            print("  - 无")
            continue
        for row in status_rows:
            replay_summary = summarize_replay_chain(parse_session_metadata(row["metadata_json"]))
            print(
                "  - session={} | {} ({}) | start={} | deadline={} | completed={} | early={} final={} prefix={} review={}".format(
                    row["id"],
                    row["display_name"],
                    row["account_key"] or "-",
                    row["start_command_time"],
                    row["lock_deadline_time"] or "-",
                    row["completed_time"] or "-",
                    replay_summary["early_status"],
                    replay_summary["final_status"],
                    replay_summary["prefix_status"],
                    replay_summary["review_status"],
                )
            )
    return rows


def start_tracking_attempt(connection, event_code, username=None):
    if not username:
        username = choose_registered_player(connection, event_code)
    if not username:
        username = prompt("选手 verse 用户名", allow_empty=True)
    if not username:
        return None
    deadline_minutes = prompt_int("识别等待时限（分钟）", 30)
    with transaction(connection):
        result = start_attempt_session(connection, event_code=event_code, username=username, deadline_minutes=deadline_minutes)
    print("已开始跟踪: session={} 截止={}".format(result["attempt_session_id"], result["deadline_time"]))
    print("说明：这里会跟踪锁定后出现的下一条 Verse 记录，不等同于严格实时监听“开局按钮”。")
    return result


def process_tracking_sessions(connection, event_code):
    limit = prompt_int("本次最多处理多少个会话", 20)
    with transaction(connection):
        results = process_open_attempt_sessions(connection, limit=limit, event_code=event_code)
    if not results:
        print("当前比赛没有待处理跟踪会话。")
        return results
    for item in results:
        line = "session={} status={}".format(item["session_id"], attempt_status_label(item["status"]))
        if "source_record_id" in item:
            line += " record={}".format(item["source_record_id"])
        if "score" in item:
            line += " score={}".format(item["score"])
        if item.get("matched_by") == "played_at_fallback":
            line += " basis=played_at_fallback"
        elif item.get("matched_by") == "started_at":
            line += " basis=started_at"
        if "reason" in item:
            line += " reason={}".format(item["reason"])
        print(line)
    return results


def prompt_classic_settings():
    event_type = choose_from_list(
        "比赛类型",
        [
            {"label": "单局赛", "value": "single_attempt"},
            {"label": "多局赛", "value": "multi_attempt"},
        ],
        default_index=0,
    )
    if event_type is None:
        return None
    aggregation_method = choose_from_list(
        "成绩规则",
        [
            {"label": "单局最高分", "value": "best_single"},
            {"label": "多局取最高分", "value": "best_of_n"},
            {"label": "多局平均分", "value": "average_of_n"},
            {"label": "取最高的X局求和", "value": "sum_of_best_n"},
        ],
        default_index=0,
    )
    if aggregation_method is None:
        return None
    aggregation_count = None
    missing_attempt_policy = None
    if aggregation_method in {"best_of_n", "average_of_n", "sum_of_best_n"}:
        aggregation_count = prompt_int("局数要求", 2)
    if aggregation_method == "average_of_n":
        missing_attempt_policy = "zero_fill"
    return event_type, aggregation_method, aggregation_count, missing_attempt_policy


def prompt_stone_settings():
    target_value = choose_from_list(
        "Stone 目标值",
        [
            {"label": "1k Stone", "value": 1024},
            {"label": "2k Stone", "value": 2048},
        ],
        default_index=1,
    )
    if target_value is None:
        return None
    event_type = choose_from_list(
        "比赛类型",
        [
            {"label": "单局赛", "value": "single_attempt"},
            {"label": "多局赛", "value": "multi_attempt"},
        ],
        default_index=1,
    )
    if event_type is None:
        return None
    aggregation_method = choose_from_list(
        "成绩规则",
        [
            {"label": "单局最高分", "value": "best_single"},
            {"label": "多局取最高分", "value": "best_of_n"},
            {"label": "多局平均分", "value": "average_of_n"},
            {"label": "取最高的X局求和", "value": "sum_of_best_n"},
        ],
        default_index=1,
    )
    if aggregation_method is None:
        return None
    aggregation_count = None
    missing_attempt_policy = None
    if aggregation_method in {"best_of_n", "average_of_n", "sum_of_best_n"}:
        aggregation_count = prompt_int("局数要求", 2)
    if aggregation_method == "average_of_n":
        missing_attempt_policy = "zero_fill"
    return target_value, event_type, aggregation_method, aggregation_count, missing_attempt_policy


def choose_event_type(default_value="single_attempt"):
    options = [
        {"label": "单局赛", "value": "single_attempt"},
        {"label": "多局赛", "value": "multi_attempt"},
    ]
    default_index = 1 if default_value == "multi_attempt" else 0
    return choose_from_list("比赛类型", options, default_index=default_index)


def choose_aggregation_method(options, default_value):
    choices = [{"label": aggregation_label(value), "value": value} for value in options]
    default_index = 0
    for index, item in enumerate(choices):
        if item["value"] == default_value:
            default_index = index
            break
    return choose_from_list("成绩规则", choices, default_index=default_index)


def prompt_mode_settings(mode):
    variant_code = mode["variant_code"]
    variant = None
    variants = mode.get("variants") or []
    if variants:
        default_index = 0
        for index, item in enumerate(variants):
            if item["value"] == mode.get("variant_code"):
                default_index = index
                break
        variant_code = choose_from_list(
            "棋盘/玩法",
            [{"label": item["label"], "value": item["value"]} for item in variants],
            default_index=default_index,
        )
        if variant_code is None:
            return None
        variant = get_variant(mode, variant_code)

    target_value = None
    target = None
    targets = mode.get("targets") or []
    if targets:
        default_index = 0
        for index, item in enumerate(targets):
            if item["value"] == mode.get("default_target_value"):
                default_index = index
                break
        target_value = choose_from_list(
            "目标值",
            [{"label": item["label"], "value": item["value"]} for item in targets],
            default_index=default_index,
        )
        if target_value is None:
            return None
        target = get_target(mode, target_value)

    competition_type = mode.get("competition_type")
    mode_event_type_default = mode.get("event_type_default", "single_attempt")
    mode_agg_options = mode.get("aggregation_options") or ["best_single"]
    mode_agg_default = mode.get("aggregation_default", "best_single")

    if competition_type == "timed_scoring":
        event_type = "timed_scoring"
        aggregation_method = "sum"
    elif competition_type == "points_series_3x4":
        event_type = mode_event_type_default
        aggregation_method = "weighted_top_n"
    else:
        event_type = mode_event_type_default
        if len(mode_agg_options) == 1:
            aggregation_method = mode_agg_options[0]
        else:
            aggregation_method = choose_aggregation_method(mode_agg_options, mode_agg_default)
            if aggregation_method is None:
                return None
    aggregation_count = None
    missing_attempt_policy = None
    if aggregation_method in {"best_of_n", "average_of_n", "sum_of_best_n"}:
        aggregation_count = prompt_int("局数要求", 2)
    if aggregation_method == "average_of_n":
        missing_attempt_policy = "zero_fill"
    return variant_code, variant, target_value, target, event_type, aggregation_method, aggregation_count, missing_attempt_policy


def create_mode_event(connection, mode_code):
    if not mode_code:
        return None
    mode = get_competition_mode(mode_code)
    print("")
    print("创建{}".format(mode["label"]))
    timed_mode = "legacy"
    if mode["competition_type"] == "timed_scoring":
        timed_mode = choose_from_list(
            "限时赛类型",
            [
                {"label": "预约型限时赛", "value": "reservation"},
                {"label": "原始型限时赛", "value": "legacy"},
            ],
            default_index=0,
        )
        if timed_mode is None:
            return None
    mode_settings = prompt_mode_settings(mode)
    if mode_settings is None:
        return None
    variant_code, variant, target_value, target, event_type, aggregation_method, aggregation_count, missing_attempt_policy = mode_settings
    default_name = (
        target.get("default_name")
        if target
        else variant.get("default_name")
        if variant
        else mode["default_name"]
    )
    event_name = prompt("比赛名称", default_name)
    if event_name is None:
        return None
    start_time = prompt_event_time("开始时间")
    if start_time is None:
        return None
    start_dt = parse_event_time(start_time)
    end_time = None
    end_dt = None
    if mode["competition_type"] == "timed_scoring" and timed_mode == "legacy":
        duration_minutes = prompt_duration_minutes("原始型比赛持续时间（分钟）", 60)
        if duration_minutes is None:
            return None
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        end_time = end_dt.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
        print("自动计算结束时间: {}".format(end_time))
    else:
        end_time = prompt_event_time("结束时间")
        if end_time is None:
            return None
        end_dt = parse_event_time(end_time)
    remark = prompt("remark (optional)", allow_empty=True) or None
    registration_close_time = None
    registration_deadline_offset_text = "00:00"
    reservation_duration_minutes = None
    if mode["competition_type"] == "timed_scoring" and timed_mode == "legacy":
        registration_close_time, registration_deadline_offset_text = prompt_registration_deadline(start_dt, force_at_start=True)
        if registration_close_time is None:
            return None
    else:
        registration_close_time, registration_deadline_offset_text = prompt_registration_deadline(start_dt, force_at_start=False)
        if registration_close_time is None:
            return None
    if mode["competition_type"] == "timed_scoring":
        if timed_mode != "legacy":
            reservation_duration_minutes = prompt_int("预约型个人时长（分钟）", 60, allow_cancel=True)
            if reservation_duration_minutes is None:
                return None
            latest_start = end_dt - timedelta(minutes=reservation_duration_minutes)
            if latest_start < start_dt:
                raise ValueError("预约时长超过赛事总时长，请缩短预约时长或延长赛事时间。")

    is_official, is_rated = prompt_official_and_rated_defaults()
    rating_bucket_code = infer_rating_bucket_code(variant_code, mode["competition_type"], target_value)
    rule_overrides = {
        "aggregation_method": aggregation_method,
        "aggregation_count": aggregation_count,
        "missing_attempt_policy": missing_attempt_policy,
    }
    if mode["competition_type"] == "points_series_3x4":
        rule_overrides.update(
            {
                "aggregation_method": "weighted_top_n",
                "top_n": 5,
                "weight_base": 0.6,
                "single_metric": "terminal_board_sum",
            }
        )

    with transaction(connection):
        result = create_or_update_event(
            connection,
            event_code=None,
            event_name=event_name,
            platform_code=mode["platform_code"],
            variant_code=variant_code,
            event_type=event_type,
            competition_type=mode["competition_type"],
            rating_bucket_code=rating_bucket_code,
            status="ready",
            is_official=is_official,
            is_rated=is_rated,
            registration_close_time=registration_close_time,
            start_time=start_time,
            end_time=end_time,
            seal_time=None,
            source="organizer_event_hub",
            tags=mode["tags"],
            target_value=target_value,
            metadata={
                "notes": "created by organizer_event_hub.py",
                "mode_code": mode["code"],
                "variant_code": variant_code,
                "uses_locking": mode["uses_locking"],
                "score_import_notes": mode["score_import_notes"],
                "remark": remark,
                "timed_mode": timed_mode,
                "reservation_duration_minutes": reservation_duration_minutes,
                "registration_deadline_offset": registration_deadline_offset_text,
            },
            rule_overrides=rule_overrides,
        )
    print("已创建/更新赛事: {event_code}".format(**result))
    print("rating bucket: {}".format(result["rating_bucket_code"] or "-"))
    print("成绩来源: {}".format(mode["score_import_notes"]))
    return result["event_code"]


def create_classic_event(connection):
    print("")
    print("创建经典 4x4 比赛")
    event_name = prompt("比赛名称", "经典4x4")
    if event_name is None:
        return None
    start_time = prompt_event_time("开始时间")
    if start_time is None:
        return None
    start_dt = parse_event_time(start_time)
    end_time = prompt_event_time("结束时间")
    if end_time is None:
        return None
    registration_close_time, registration_deadline_offset_text = prompt_registration_deadline(start_dt, force_at_start=False)
    if registration_close_time is None:
        return None
    remark = prompt("remark (optional)", allow_empty=True) or None
    classic_settings = prompt_classic_settings()
    if classic_settings is None:
        return None
    event_type, aggregation_method, aggregation_count, missing_attempt_policy = classic_settings

    is_official, is_rated = prompt_official_and_rated_defaults()

    with transaction(connection):
        result = create_or_update_event(
            connection,
            event_code=None,
            event_name=event_name,
            platform_code="2048verse",
            variant_code="4x4",
            event_type=event_type,
            competition_type="classic_raw_score",
            rating_bucket_code=infer_rating_bucket_code("4x4", "classic_raw_score", None),
            status="ready",
            is_official=is_official,
            is_rated=is_rated,
            registration_close_time=registration_close_time,
            start_time=start_time,
            end_time=end_time,
            seal_time=None,
            source="organizer_event_hub",
            tags=["raw_score_hosted"],
            target_value=None,
            metadata={
                "notes": "created by organizer_event_hub.py",
                "remark": remark,
                "registration_deadline_offset": registration_deadline_offset_text,
            },
            rule_overrides={
                "aggregation_method": aggregation_method,
                "aggregation_count": aggregation_count,
                "missing_attempt_policy": missing_attempt_policy,
            },
        )
    print("已创建/更新赛事: {event_code}".format(**result))
    print("下一步建议: 先做报名，再开始跟踪选手记录。")
    return result["event_code"]


def create_stone_event(connection):
    print("")
    print("创建 Stone 比赛")
    stone_settings = prompt_stone_settings()
    if stone_settings is None:
        return None
    target_value, event_type, aggregation_method, aggregation_count, missing_attempt_policy = stone_settings
    default_name = "1k Stone" if target_value == 1024 else "2k Stone"
    event_name = prompt("比赛名称", default_name)
    if event_name is None:
        return None
    start_time = prompt_event_time("开始时间")
    if start_time is None:
        return None
    start_dt = parse_event_time(start_time)
    end_time = prompt_event_time("结束时间")
    if end_time is None:
        return None
    registration_close_time, registration_deadline_offset_text = prompt_registration_deadline(start_dt, force_at_start=False)
    if registration_close_time is None:
        return None
    remark = prompt("remark (optional)", allow_empty=True) or None
    is_official, is_rated = prompt_official_and_rated_defaults()

    with transaction(connection):
        result = create_or_update_event(
            connection,
            event_code=None,
            event_name=event_name,
            platform_code="taihe",
            variant_code="4x4",
            event_type=event_type,
            competition_type="stone_x",
            rating_bucket_code=infer_rating_bucket_code("4x4", "stone_x", target_value),
            status="ready",
            is_official=is_official,
            is_rated=is_rated,
            registration_close_time=registration_close_time,
            start_time=start_time,
            end_time=end_time,
            seal_time=None,
            source="organizer_event_hub",
            tags=["raw_score_hosted", "stone"],
            target_value=target_value,
            metadata={
                "notes": "created by organizer_event_hub.py",
                "remark": remark,
                "registration_deadline_offset": registration_deadline_offset_text,
            },
            rule_overrides={
                "aggregation_method": aggregation_method,
                "aggregation_count": aggregation_count,
                "missing_attempt_policy": missing_attempt_policy,
            },
        )
    print("已创建/更新赛事: {event_code}".format(**result))
    print("下一步建议: 先做报名，比赛中再导入成绩文件。")
    return result["event_code"]


def edit_current_event_info(connection, event_code):
    summary = fetch_event_summary(connection, event_code)
    event = summary["event"]
    print("")
    print("修改比赛信息")
    print("这版当前支持修改：名称、报名截止时间、开始时间、结束时间、正式赛、是否计 rating。")
    print("赛制、平台、玩法、聚合规则暂不在这里修改。")

    event_name = prompt("比赛名称", event["event_name"])
    if event_name is None:
        print("已取消修改。")
        return
    start_time = prompt_event_time_with_default("开始时间", event["start_time"]) if event["start_time"] else prompt_event_time("开始时间")
    end_time = prompt_event_time_with_default("结束时间", event["end_time"]) if event["end_time"] else prompt_event_time("结束时间")
    if start_time is None or end_time is None:
        print("已取消修改。")
        return
    start_dt = parse_event_time(start_time)
    current_registration_deadline = event["registration_close_time"] or event["start_time"] or "-"
    print("当前报名截止时间: {}".format(current_registration_deadline))
    registration_close_time = event["registration_close_time"] or start_time
    registration_deadline_offset_text = "保持不变"
    if prompt_yes_no("是否修改报名截止时间", False):
        registration_close_time, registration_deadline_offset_text = prompt_registration_deadline(start_dt, force_at_start=False)
        if registration_close_time is None:
            print("已取消修改。")
            return

    is_official = prompt_yes_no("是否正式赛", bool(event["is_official"]))
    is_rated = prompt_yes_no("是否计 rating", bool(event["is_rated"]))

    print("")
    print("修改预览")
    print("名称: {} -> {}".format(event["event_name"], event_name))
    print("报名截止: {} -> {}".format(current_registration_deadline, registration_close_time or "-"))
    if registration_deadline_offset_text != "保持不变":
        print("报名截止偏移: {}".format(registration_deadline_offset_text))
    print("开始时间: {} -> {}".format(event["start_time"] or "-", start_time))
    print("结束时间: {} -> {}".format(event["end_time"] or "-", end_time))
    print("正式赛: {} -> {}".format("是" if event["is_official"] else "否", "是" if is_official else "否"))
    print("计 rating: {} -> {}".format("是" if event["is_rated"] else "否", "是" if is_rated else "否"))
    if summary["counts"]["result_count"]:
        print("注意：该赛事已有结算结果，修改后请重新执行“结算比赛”以刷新结果。")
    if event["is_rated"] or is_rated:
        print("注意：涉及 rating 的修改，重新结算后再检查 rating 榜是否符合预期。")
    if not prompt_yes_no("确认保存这些修改吗", False):
        print("已取消修改。")
        return

    try:
        with transaction(connection):
            result = update_event_basic_info(
                connection,
                event_code,
                event_name=event_name,
                registration_close_time=registration_close_time,
                start_time=start_time,
                end_time=end_time,
                is_official=is_official,
                is_rated=is_rated,
            )
    except ValueError as exc:
        print("修改失败: {}".format(exc))
        return
    print("已更新比赛信息: {}。".format(result["event_code"]))
    if result["result_count"]:
        print("这场比赛之前已有结算结果，记得重新执行“结算比赛”。")


def show_current_event_info(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code
    while True:
        summary = fetch_event_summary(connection, event_code)
        event = summary["event"]
        counts = summary["counts"]
        rule = summary["rule"]
        progress = summary["progress"]
        supports_locking = event_supports_locking(event)
        print("")
        print("比赛信息")
        print("赛事ID: {}".format(event["event_code"]))
        print("名称: {}".format(event["event_name"]))
        remark = event_remark(event)
        if remark:
            print("备注: {}".format(remark))
        print("平台: {}".format(event["platform_code"]))
        print("玩法: {}".format(event["variant_code"] or "-"))
        print("赛制: {}".format(event["competition_type"]))
        print("状态: {} | {}".format(summary["runtime_status"], event["status"]))
        print("时间: {} -> {}".format(event["start_time"] or "-", event["end_time"] or "-"))
        print("正式赛: {}".format("是" if event["is_official"] else "否"))
        print("计 rating: {}".format("是" if event["is_rated"] else "否"))
        if rule is not None:
            print("聚合方式: {}".format(rule["aggregation_method"] or "-"))
        print("报名人数: {}".format(counts["registration_count"]))
        print("候选成绩数: {}".format(counts["attempt_record_count"]))
        print("已结算人数: {}".format(counts["result_count"]))
        if supports_locking:
            print("等待识别: {}".format(counts["pending_lock_count"]))
            print("已识别，待完赛: {}".format(counts["locked_in_progress_count"]))
            print("已完成跟踪: {}".format(counts["completed_lock_count"]))
        print("选手待开始: {}".format(progress["counts"].get("待开始", 0)))
        print("选手进行中: {}".format(progress["counts"].get("进行中", 0)))
        print("选手已完赛: {}".format(progress["counts"].get("已完赛", 0)))
        print("")
        print("1. 查看参赛选手状态总览")
        print("2. 查看单个选手赛中信息")
        if is_reservation_timed_event(event):
            print("3. 查看选手预约情况")
            print("4. 手动录入/修改选手预约")
            print("5. 修改选手平台用户名")
            print("6. 修改比赛信息")
        else:
            print("3. 修改选手平台用户名")
            print("4. 修改比赛信息")
        print("0. 返回")
        choice = prompt("请选择操作", "0")
        if choice == "1":
            view_participant_from_overview(connection, event_code)
        elif choice == "2":
            snapshot = build_event_progress_snapshot(connection, event_code)
            username = choose_participant_from_snapshot(snapshot)
            if username:
                show_player_detail(connection, event_code, username)
        elif choice == "3" and is_reservation_timed_event(event):
            show_reservation_status_overview(connection, event_code)
        elif choice == "3" and not is_reservation_timed_event(event):
            manual_update_registered_username(connection, event_code)
        elif choice == "4" and is_reservation_timed_event(event):
            manual_reservation_entry(connection, event_code)
        elif choice == "4" and not is_reservation_timed_event(event):
            edit_current_event_info(connection, event_code)
        elif choice == "5" and is_reservation_timed_event(event):
            manual_update_registered_username(connection, event_code)
        elif choice == "6" and is_reservation_timed_event(event):
            edit_current_event_info(connection, event_code)
        elif choice == "0":
            return event_code
        else:
            print("无效选项，请重新输入。")
    return event_code


def show_participant_groups(connection, event_code):
    snapshot = build_event_progress_snapshot(connection, event_code)
    supports_locking = event_supports_locking(snapshot["event"])
    print("")
    print("参赛选手状态")
    indexed_participants = []
    for label in ("待开始", "进行中", "已完赛"):
        rows = snapshot["grouped"].get(label, [])
        print("{}（{}）".format(label, len(rows)))
        if not rows:
            print("  - 无")
            continue
        for item in rows:
            indexed_participants.append(item)
            print(
                "  {}. {} ({}) | 已计局数 {}/{} | 当前最好 {} | 最近提交 {}".format(
                    len(indexed_participants),
                    item["display_name"],
                    item["username"] or "-",
                    item["valid_record_count"],
                    item["required_attempts"] or "-",
                    item["best_score"] if item["best_score"] is not None else "-",
                    format_display_time(item.get("last_activity_time")),
                )
            )
    return snapshot, indexed_participants


def show_unfinished_participants(connection, event_code):
    snapshot, indexed_participants = show_participant_groups(connection, event_code)
    unfinished = [
        item
        for item in indexed_participants
        if item["status"] in {"待开始", "进行中"}
    ]
    print("")
    print("未完赛名单")
    if not unfinished:
        print("- 当前参赛选手都已完赛。")
        return
    waiting_count = sum(1 for item in unfinished if item["status"] == "待开始")
    in_progress_count = sum(1 for item in unfinished if item["status"] == "进行中")
    print("待开始 {} 人 | 进行中 {} 人 | 未完赛共 {} 人".format(waiting_count, in_progress_count, len(unfinished)))
    unfinished = sorted(
        unfinished,
        key=lambda item: (
            0 if item["status"] == "进行中" else 1,
            item.get("last_activity_time") or "",
            (item["username"] or item["display_name"] or "").lower(),
        ),
        reverse=False,
    )
    for index, item in enumerate(unfinished, start=1):
        print(
            "{}. {} ({}) | {} | 已计局数 {}/{} | 当前最好 {} | 最近动静 {}".format(
                index,
                item["display_name"],
                item["username"] or "-",
                item["status"],
                item["valid_record_count"],
                item["required_attempts"] or "-",
                item["best_score"] if item["best_score"] is not None else "-",
                format_display_time(item.get("last_activity_time")),
            )
        )
    while True:
        choice = prompt("输入未完赛选手序号查看详情，直接回车返回", allow_empty=True)
        if not choice:
            return
        try:
            selected = int(choice)
        except ValueError:
            print("请输入序号。")
            continue
        if selected < 1 or selected > len(unfinished):
            print("超出范围，请重新输入。")
            continue
        username = unfinished[selected - 1]["username"]
        if not username:
            print("该选手没有可用的平台用户名，暂时无法查看详情。")
            return
        show_player_detail(connection, event_code, username)
        return


def view_participant_from_overview(connection, event_code):
    _snapshot, indexed_participants = show_participant_groups(connection, event_code)
    if not indexed_participants:
        return
    while True:
        choice = prompt("输入选手序号查看详情，直接回车返回", allow_empty=True)
        if not choice:
            return
        try:
            selected = int(choice)
        except ValueError:
            print("请输入序号。")
            continue
        if selected < 1 or selected > len(indexed_participants):
            print("超出范围，请重新输入。")
            continue
        username = indexed_participants[selected - 1]["username"]
        if not username:
            print("该选手没有可用的平台用户名，暂时无法查看详情。")
            return
        show_player_detail(connection, event_code, username)
        return


def choose_participant_from_snapshot(snapshot):
    participants = snapshot["participants"]
    if not participants:
        print("当前比赛还没有参赛选手。")
        return None
    options = []
    default_index = 0
    for index, item in enumerate(participants):
        options.append(
            {
                "label": "{} | {} | {} | {}/{}".format(
                    item["display_name"],
                    item["username"] or "-",
                    item["status"],
                    item["valid_record_count"],
                    item["required_attempts"] or "-",
                ),
                "value": item["username"],
            }
        )
        if item["status"] == "进行中" and default_index == 0:
            default_index = index
    return choose_from_list("选择参赛选手", options, default_index=default_index)


def show_player_detail(connection, event_code, username):
    detail = get_player_progress_detail(connection, event_code, username)
    player = detail["player"]
    supports_locking = event_supports_locking(detail["event"])
    print("")
    print("选手赛中信息")
    print("选手: {} ({})".format(player["display_name"], player["username"] or "-"))
    print("状态: {}".format(player["status"]))
    print("已计局数: {}/{}".format(player["valid_record_count"], player["required_attempts"] or "-"))
    print("当前最好: {}".format(player["best_score"] if player["best_score"] is not None else "-"))
    print("最近成绩: {}".format(player["latest_score"] if player["latest_score"] is not None else "-"))
    print("最近提交: {}".format(format_display_time(player.get("last_activity_time"))))
    if detail["result"] is not None:
        print(
            "当前排名: {} | 当前成绩: {} | 最佳单局: {}".format(
                detail["result"]["rank_value"],
                detail["result"]["primary_metric_value"],
                detail["result"]["best_single_score"],
            )
        )
    if supports_locking:
        print("")
        print("跟踪会话")
        if not detail["sessions"]:
            print("- 无")
        else:
            for session in detail["sessions"]:
                chain_summary = summarize_replay_chain(parse_session_metadata(session["metadata_json"]))
                print(
                    "- session={} | {} | start={} | deadline={} | completed={} | early={} final={} prefix={} review={}".format(
                        session["id"],
                        attempt_status_label(session["status"]),
                        session["start_command_time"],
                        session["lock_deadline_time"] or "-",
                        session["completed_time"] or "-",
                        chain_summary["early_status"],
                        chain_summary["final_status"],
                        chain_summary["prefix_status"],
                        chain_summary["review_status"],
                    )
                )
    print("")
    print("成绩记录")
    if not detail["records"]:
        print("- 无")
    else:
        for record in detail["records"]:
            print(
                "- id={} | score={} | {} -> {} | {}{}".format(
                    record["source_record_id"],
                    record["final_score"] if record["final_score"] is not None else record["raw_score"],
                    record["started_at"] or "-",
                    record["ended_at"] or "-",
                    record["evaluation_status"],
                    "" if record["is_valid_source"] else " | 已作废",
                )
            )
    print("")
    print("快捷操作")
    if supports_locking:
        print("1. 开始跟踪这名选手的下一条记录")
        print("2. 查看这名选手的候选记录")
        print("3. 按补录信息绑定 Verse 记录")
        print("4. 手动录入/补录成绩")
        print("5. 回放证据链操作")
        print("6. 从 Discord 自动匹配终局回放并录成绩")
    else:
        print("4. 手动录入/补录成绩")
    print("0. 返回")
    choice = prompt("请选择操作", "0")
    if supports_locking and choice == "1":
        start_tracking_attempt(connection, event_code, username=username)
    elif supports_locking and choice == "2":
        show_attempt_candidates(connection, event_code, username=username)
    elif supports_locking and choice == "3":
        manual_bind_verse_record(connection, event_code, username=username)
    elif choice == "4":
        manual_score_entry(connection, event_code)
    elif supports_locking and choice == "5":
        replay_lock_chain_menu(connection, event_code, username=username)
    elif supports_locking and choice == "6":
        manual_refresh_locking_player_from_discord(connection, event_code, username=username)


def choose_registered_player(connection, event_code):
    payload = list_registrations(connection, event_code)
    rows = [row for row in payload["rows"] if row["status"] == "active" and row["username"]]
    if not rows:
        print("当前比赛还没有可用的报名选手。")
        return None
    options = []
    for row in rows:
        via = row["registered_via"] or "-"
        options.append(
            {
                "label": "{} ({}) [{}]".format(row["display_name"], row["username"], via),
                "value": row["username"],
            }
        )
    return choose_from_list("选择选手", options, default_index=0)


def choose_attempt_session(connection, event_code, username):
    rows = connection.execute(
        """
        SELECT
            s.id,
            s.status,
            s.start_command_time,
            s.lock_deadline_time,
            s.completed_time
        FROM attempt_sessions s
        JOIN events e ON e.id = s.event_id
        JOIN player_accounts pa
            ON pa.player_id = s.player_id
           AND pa.platform_id = e.platform_id
           AND pa.is_primary = 1
        WHERE e.event_code = ? AND pa.account_key = ?
        ORDER BY
            CASE
                WHEN s.status = 'pending_lock' THEN 0
                WHEN s.status = 'locked_in_progress' THEN 1
                WHEN s.status = 'completed' THEN 2
                WHEN s.status = 'expired' THEN 3
                WHEN s.status = 'cancelled' THEN 4
                ELSE 5
            END,
            s.id DESC
        """,
        (event_code, username),
    ).fetchall()
    if not rows:
        print("该选手当前没有跟踪会话。")
        return None
    if len(rows) == 1:
        return rows[0]["id"]

    options = []
    for row in rows:
        options.append(
            {
                "label": "session={} | {} | start={} | deadline={} | completed={}".format(
                    row["id"],
                    attempt_status_label(row["status"]),
                    row["start_command_time"] or "-",
                    row["lock_deadline_time"] or "-",
                    row["completed_time"] or "-",
                ),
                "value": row["id"],
            }
        )
    return choose_from_list("选择跟踪会话", options, default_index=0)


def show_attempt_candidates(connection, event_code, username=None):
    if not username:
        username = choose_registered_player(connection, event_code)
    if not username:
        return
    session_id = choose_attempt_session(connection, event_code, username)
    if session_id is None:
        return
    result = list_attempt_candidates(connection, event_code, username, session_id=session_id)
    session = result["session"]
    print(
        "session={} status={} start={} deadline={}".format(
            session["id"],
            attempt_status_label(session["status"]),
            session["start_command_time"],
            session["lock_deadline_time"],
        )
    )
    if not result["candidates"]:
        print("没有找到候选记录。")
        return
    for item in result["candidates"]:
        print(
            "- id={} score={} started={} ended={} max={} user_id={}".format(
                item["source_record_id"],
                item["score"],
                item["started_at"],
                item["ended_at"],
                item["max_tile_value"] if item["max_tile_value"] is not None else "-",
                item["user_id"] or "-",
            )
        )


def replay_lock_chain_menu(connection, event_code, username=None):
    if not username:
        username = choose_registered_player(connection, event_code)
    if not username:
        return
    session_id = choose_attempt_session(connection, event_code, username)
    if session_id is None:
        return
    while True:
        print("")
        print("=== 回放证据链 | {} | {} | session={} ===".format(event_code, username, session_id))
        try:
            chain = get_replay_chain_status(connection, event_code, username, session_id=session_id)
        except ValueError as exc:
            print("读取失败: {}".format(exc))
            return
        early = chain.get("early_replay") or {}
        final = chain.get("final_replay") or {}
        prefix_check = chain.get("prefix_check") or {}
        review = chain.get("review") or {}
        print("状态: {}".format(attempt_status_label(chain.get("status"))))
        print("早期回放: {} | tile={} | sha={}".format(
            early.get("path") or "-",
            early.get("checkpoint_tile") if early.get("checkpoint_tile") is not None else "-",
            (early.get("sha256") or "-")[:12],
        ))
        print("终局回放: {} | sha={}".format(
            final.get("path") or "-",
            (final.get("sha256") or "-")[:12],
        ))
        print("前缀校验: {} | reason={}".format(prefix_check.get("status") or "missing", prefix_check.get("reason") or "-"))
        print("人工复核: {} | reviewer={} | note={}".format(
            review.get("status") or "missing",
            review.get("reviewer") or "-",
            review.get("note") or "-",
        ))
        print("1. 录入早期回放（默认128）")
        print("2. 录入终局回放")
        print("3. 执行前缀校验")
        print("4. 标记人工复核通过")
        print("5. 标记人工复核驳回")
        print("0. 返回")
        choice = prompt("请选择操作", "0")
        if choice == "1":
            source_path = Path(prompt("早期回放文件路径")).expanduser().resolve()
            checkpoint_tile = prompt_int("早期节点（默认128）", 128)
            with transaction(connection):
                result = attach_early_replay(
                    connection,
                    event_code,
                    username,
                    str(source_path),
                    checkpoint_tile=checkpoint_tile,
                    session_id=session_id,
                )
            print("已录入早期回放: {}".format(result["stored_path"]))
        elif choice == "2":
            source_path = Path(prompt("终局回放文件路径")).expanduser().resolve()
            with transaction(connection):
                result = attach_final_replay(connection, event_code, username, str(source_path), session_id=session_id)
            print("已录入终局回放: {}".format(result["stored_path"]))
        elif choice == "3":
            with transaction(connection):
                result = run_replay_prefix_check(connection, event_code, username, session_id=session_id)
            print("校验结果: {} | reason={}".format(result["status"], result["reason"]))
            if result.get("details"):
                print("details={}".format(result["details"]))
        elif choice == "4":
            note = prompt("复核备注（可空）", allow_empty=True)
            reviewer = prompt("复核人", "organizer_event_hub")
            with transaction(connection):
                result = set_replay_review_status(
                    connection,
                    event_code,
                    username,
                    approved=True,
                    reviewer=reviewer,
                    note=note,
                    session_id=session_id,
                )
            print("已标记复核通过: session={} status={}".format(result["session_id"], attempt_status_label(result["status"])))
        elif choice == "5":
            note = prompt("驳回原因")
            reviewer = prompt("复核人", "organizer_event_hub")
            with transaction(connection):
                result = set_replay_review_status(
                    connection,
                    event_code,
                    username,
                    approved=False,
                    reviewer=reviewer,
                    note=note,
                    session_id=session_id,
                )
            print("已标记复核驳回: session={} status={}".format(result["session_id"], attempt_status_label(result["status"])))
        elif choice == "0":
            return
        else:
            print("无效选项，请重新输入。")


def inspect_verse_api_fields_for_player(connection, event_code):
    username = choose_registered_player(connection, event_code)
    if not username:
        return
    row = connection.execute(
        """
        SELECT v.code AS variant_code
        FROM events e
        LEFT JOIN variants v ON v.id = e.variant_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    variant_code = row["variant_code"] if row is not None and row["variant_code"] else "4x4"
    since = datetime.now(LOCAL_TIMEZONE) - timedelta(days=3650)
    result = inspect_recent_game_fields(username, variant_code, since, max_pages=2)
    print("")
    print("Verse API 字段诊断")
    print("选手: {} | variant={}".format(username, variant_code))
    print("抓取记录数: {}".format(result["game_count"]))
    if not result["key_counts"]:
        print("未拿到可解析字段。")
        return
    keys_sorted = sorted(result["key_counts"].items(), key=lambda kv: kv[1], reverse=True)
    print("字段统计:")
    for key, count in keys_sorted[:20]:
        print("- {} ({})".format(key, count))
    if result["replay_like_keys"]:
        print("检测到回放相关字段: {}".format(", ".join(result["replay_like_keys"])))
    else:
        print("未检测到 replay/vrs 相关字段，当前无法直接从该接口自动提取终局回放文件信息。")


def current_event_workbench_menu(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code
    while True:
        summary = fetch_event_summary(connection, event_code)
        counts = summary["counts"]
        supports_locking = event_supports_locking(summary["event"])
        print("")
        print("=== 当前比赛工作台 | {} ===".format(event_code))
        print(
            "当前比赛: {} | 报名{} 已结算{}".format(
                summary["event"]["event_name"],
                counts["registration_count"],
                counts["result_count"],
            )
        )
        if supports_locking:
            print("锁局会话: 待识别{} 识别中{}".format(counts["pending_lock_count"], counts["locked_in_progress_count"]))
        print("1. 比赛信息模块")
        print("2. 报名模块")
        if supports_locking:
            print("3. 跟踪锁局模块")
            print("4. 成绩模块")
            print("5. 排名模块")
            print("6. 结算模块")
        else:
            print("3. 成绩模块")
            print("4. 排名模块")
            print("5. 结算模块")
        print("0. 返回")
        choice = prompt("请选择操作", "0")
        if choice == "1":
            show_current_event_info(connection, event_code)
        elif choice == "2":
            registration_menu(connection, event_code)
        elif supports_locking and choice == "3":
            lock_menu(connection, event_code)
        elif (supports_locking and choice == "4") or ((not supports_locking) and choice == "3"):
            score_menu(connection, event_code)
        elif (supports_locking and choice == "5") or ((not supports_locking) and choice == "4"):
            while True:
                print("")
                print("=== 排名模块 | {} ===".format(event_code))
                print("1. 查看当前临时排名")
                print("2. 导出当前排名图片")
                print("3. 查看未完赛名单")
                print("4. 查看参赛选手状态总览")
                print("5. 查看单个选手赛中信息")
                print("0. 返回")
                ranking_choice = prompt("请选择操作", "0")
                if ranking_choice == "1":
                    preview = preview_ranking(connection, event_code)
                    rows = preview["rows"]
                    if not rows:
                        print("当前还没有可用于排名的记录。")
                        continue
                    snapshot = build_event_progress_snapshot(connection, event_code)
                    participant_by_username = {
                        item["username"]: item
                        for item in snapshot["participants"]
                        if item.get("username")
                    }
                    print("")
                    print("当前临时排名")
                    supports_locking = event_supports_locking(preview["event"])
                    detail_candidates = []
                    for row in rows:
                        account_name = row["account_name"] if "account_name" in row.keys() else None
                        participant = participant_by_username.get(account_name)
                        detail_candidates.append(account_name if account_name else None)
                        display_index = len(detail_candidates)
                        if supports_locking:
                            lock_status = participant.get("lock_status_short") if participant else "-"
                            print(
                                "{}. [{}] {} | {} | 当前成绩 {} | 已计局数 {} | 最佳单局 {}".format(
                                    row["rank_value"],
                                    display_index,
                                    row["display_name"],
                                    lock_status,
                                    row["primary_metric_value"] if row["primary_metric_value"] is not None else "-",
                                    row["secondary_metric_value"] or 0,
                                    row["best_single_score"] if row["best_single_score"] is not None else "-",
                                )
                            )
                        else:
                            print(
                                "{}. [{}] {} | 当前成绩 {} | 已计局数 {} | 最佳单局 {}".format(
                                    row["rank_value"],
                                    display_index,
                                    row["display_name"],
                                    row["primary_metric_value"] if row["primary_metric_value"] is not None else "-",
                                    row["secondary_metric_value"] or 0,
                                    row["best_single_score"] if row["best_single_score"] is not None else "-",
                                )
                            )
                    if detail_candidates:
                        while True:
                            detail_choice = prompt("输入选手序号查看详情，直接回车返回", allow_empty=True)
                            if not detail_choice:
                                break
                            try:
                                selected = int(detail_choice)
                            except ValueError:
                                print("请输入序号。")
                                continue
                            if selected < 1 or selected > len(detail_candidates):
                                print("超出范围，请重新输入。")
                                continue
                            username = detail_candidates[selected - 1]
                            if not username:
                                print("该选手没有可用的平台用户名，暂时无法查看详情。")
                                break
                            show_player_detail(connection, event_code, username)
                            break
                elif ranking_choice == "2":
                    result = export_interim_ranking_image(connection, event_code)
                    print("已导出当前排名图: {}".format(result["image_path"]))
                elif ranking_choice == "3":
                    show_current_event_unfinished(connection, event_code)
                elif ranking_choice == "4":
                    view_participant_from_overview(connection, event_code)
                elif ranking_choice == "5":
                    snapshot = build_event_progress_snapshot(connection, event_code)
                    username = choose_participant_from_snapshot(snapshot)
                    if username:
                        show_player_detail(connection, event_code, username)
                elif ranking_choice == "0":
                    break
                else:
                    print("无效选项，请重新输入。")
        elif (supports_locking and choice == "6") or ((not supports_locking) and choice == "5"):
            while True:
                print("")
                print("=== 结算模块 | {} ===".format(event_code))
                print("1. 结算前预检查")
                print("2. 执行结算比赛")
                print("3. 导出最终结果")
                print("4. 强制结束当前比赛")
                print("0. 返回")
                settle_choice = prompt("请选择操作", "0")
                if settle_choice == "1":
                    run_settlement_precheck_preview(connection, event_code)
                elif settle_choice == "2":
                    settle_current_event(connection, event_code)
                elif settle_choice == "3":
                    export_current_event_results(connection, event_code)
                elif settle_choice == "4":
                    force_end_current_event(connection, event_code)
                elif settle_choice == "0":
                    break
                else:
                    print("无效选项，请重新输入。")
        elif choice == "0":
            return event_code
        else:
            print("无效选项，请重新输入。")


def manual_score_entry(connection, event_code):
    print("")
    print("手动录入/补录成绩")
    username = choose_registered_player(connection, event_code)
    if not username:
        username = prompt("选手用户名", allow_cancel=True)
        if username is None:
            print("已取消录入。")
            return
    display_name = prompt("显示名（可空，默认同用户名）", allow_empty=True) or username
    source_record_id = prompt("记录ID（可空，系统自动生成）", allow_empty=True) or None
    started_at = prompt_optional_event_time("开始时间")
    ended_at = prompt_event_time("结束时间")
    print("如果开始时间留空，系统会用结束时间作为这条补录记录的开始时间，用于比赛时间窗判断。")
    raw_score = prompt_optional_int("原始分 raw_score（可空）")
    final_score = prompt_optional_int("最终分 final_score（可空，默认同原始分）")
    competition_score = prompt_optional_int("比赛分 competition_score（积分赛/Stone/No-X 用，可空）")
    primary_time_ms = prompt_optional_int("竞速时间 primary_time_ms，单位毫秒（Speedrun 用，可空）")
    target_tile_value = prompt_optional_int("目标块 target_tile_value（可空）")
    score_before_target = prompt_optional_int("达成目标前分数 score_before_target（可空）")
    evidence_note = prompt("备注/证据说明（可空）", allow_empty=True) or None

    if raw_score is None and final_score is None and competition_score is None and primary_time_ms is None:
        print("至少需要填写一种成绩：raw_score / final_score / competition_score / primary_time_ms。")
        return
    with transaction(connection):
        result = add_manual_score(
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
            evidence_note=evidence_note,
        )
    print("已录入成绩记录: {}".format(result["source_record_id"]))


def submit_pending_score_entry(connection, event_code):
    print("")
    print("创建待审核成绩")
    username = choose_registered_player(connection, event_code)
    if not username:
        username = prompt("选手用户名", allow_cancel=True)
        if username is None:
            print("已取消提交。")
            return
    display_name = prompt("显示名（可空，默认同用户名）", allow_empty=True) or username
    submitter_platform = prompt("提交来源平台", "qqbot")
    submitter_account = prompt("提交者账号（可空）", allow_empty=True) or None
    source_record_id = prompt("记录ID（可空）", allow_empty=True) or None
    started_at = prompt_optional_event_time("开始时间")
    ended_at = prompt_event_time("结束时间")
    raw_score = prompt_optional_int("原始分 raw_score（可空）")
    final_score = prompt_optional_int("最终分 final_score（可空，默认同原始分）")
    competition_score = prompt_optional_int("比赛分 competition_score（可空）")
    primary_time_ms = prompt_optional_int("竞速时间 primary_time_ms（可空）")
    target_tile_value = prompt_optional_int("目标块 target_tile_value（可空）")
    score_before_target = prompt_optional_int("达成目标前分数 score_before_target（可空）")
    evidence_note = prompt("备注/证据说明（可空）", allow_empty=True) or None

    if raw_score is None and final_score is None and competition_score is None and primary_time_ms is None:
        print("至少需要填写一种成绩：raw_score / final_score / competition_score / primary_time_ms。")
        return
    with transaction(connection):
        result = submit_pending_score(
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
            evidence={"note": evidence_note} if evidence_note else None,
            payload={"source": "manual_pending_submission"},
        )
    print("已创建待审核成绩: submission={}。".format(result["submission_id"]))


def pending_score_review_menu(connection, event_code):
    while True:
        print("")
        print("=== 待审核成绩 | {} ===".format(event_code))
        print("1. 查看待审核列表")
        print("2. 手动创建待审核成绩")
        print("3. 审核通过一条成绩")
        print("4. 驳回一条成绩")
        print("0. 返回")
        choice = prompt("请选择操作", "0")

        if choice == "1":
            payload = list_pending_scores(connection, event_code)
            rows = payload["rows"]
            if not rows:
                print("当前没有待审核成绩。")
                continue
            for row in rows:
                score = row["competition_score"]
                if score is None:
                    score = row["final_score"] if row["final_score"] is not None else row["raw_score"]
                print(
                    "{} | {} ({}) | score={} | {} -> {} | status={} | submitter={}{}".format(
                        row["id"],
                        row["display_name"] or row["username"] or "-",
                        row["username"] or "-",
                        score if score is not None else "-",
                        row["started_at"] or "-",
                        row["ended_at"] or "-",
                        row["status"],
                        row["submitter_platform"] or "-",
                        ":{}".format(row["submitter_account"]) if row["submitter_account"] else "",
                    )
                )
                if row["review_reason"]:
                    print("   review_reason={}".format(row["review_reason"]))
        elif choice == "2":
            submit_pending_score_entry(connection, event_code)
        elif choice == "3":
            payload = list_pending_scores(connection, event_code, status="pending")
            rows = payload["rows"]
            if not rows:
                print("当前没有待审核中的成绩。")
                continue
            options = []
            for row in rows:
                score = row["competition_score"]
                if score is None:
                    score = row["final_score"] if row["final_score"] is not None else row["raw_score"]
                label = "{} | {} | score={} | ended={}".format(
                    row["id"],
                    row["username"] or row["display_name"] or "-",
                    score if score is not None else "-",
                    row["ended_at"] or "-",
                )
                options.append({"label": label, "value": row["id"]})
            submission_id = choose_from_list("选择要通过的待审核成绩", options, default_index=0)
            if submission_id is None:
                continue
            with transaction(connection):
                result = approve_pending_score(connection, submission_id)
            print("已审核通过，正式成绩记录: {}。".format(result["source_record_id"]))
        elif choice == "4":
            payload = list_pending_scores(connection, event_code, status="pending")
            rows = payload["rows"]
            if not rows:
                print("当前没有待审核中的成绩。")
                continue
            options = []
            for row in rows:
                score = row["competition_score"]
                if score is None:
                    score = row["final_score"] if row["final_score"] is not None else row["raw_score"]
                label = "{} | {} | score={} | ended={}".format(
                    row["id"],
                    row["username"] or row["display_name"] or "-",
                    score if score is not None else "-",
                    row["ended_at"] or "-",
                )
                options.append({"label": label, "value": row["id"]})
            submission_id = choose_from_list("选择要驳回的待审核成绩", options, default_index=0)
            if submission_id is None:
                continue
            reason = prompt("驳回原因")
            with transaction(connection):
                result = reject_pending_score(connection, submission_id, reason=reason)
            print("已驳回待审核成绩: {}。".format(result["submission_id"]))
        elif choice == "0":
            return
        else:
            print("无效选项，请重新输入。")


def void_score_entry(connection, event_code):
    print("")
    print("作废错误成绩记录")
    username = choose_registered_player(connection, event_code)
    if not username:
        return
    payload = list_event_score_records(connection, event_code, username=username, include_voided=True)
    rows = payload["rows"]
    if not rows:
        print("该选手当前没有成绩记录。")
        return

    options = []
    for row in rows:
        score = row["final_score"] if row["final_score"] is not None else row["raw_score"]
        status = "有效" if row["is_valid_source"] else "已作废"
        label = "{} | score={} | {} -> {} | {} | {}".format(
            row["event_attempt_record_id"],
            score if score is not None else "-",
            row["started_at"] or "-",
            row["ended_at"] or "-",
            row["record_type"],
            status,
        )
        print(label)
        if row["notes"]:
            print("   notes={}".format(row["notes"]))
        options.append({"label": label, "value": row["event_attempt_record_id"]})

    selected_record_id = choose_from_list("选择要作废的成绩记录", options, default_index=0)
    if selected_record_id is None:
        return
    selected_row = next(item for item in rows if item["event_attempt_record_id"] == selected_record_id)
    if not selected_row["is_valid_source"]:
        print("这条成绩已经是作废状态了。")
        return

    reason = prompt("作废原因")
    score = selected_row["final_score"] if selected_row["final_score"] is not None else selected_row["raw_score"]
    print(
        "将作废: id={} | score={} | {} -> {}".format(
            selected_row["event_attempt_record_id"],
            score if score is not None else "-",
            selected_row["started_at"] or "-",
            selected_row["ended_at"] or "-",
        )
    )
    if not prompt_yes_no("确认作废这条成绩记录吗", False):
        print("已取消作废。")
        return

    with transaction(connection):
        result = void_score_record(connection, event_code, selected_record_id, reason=reason)
    print("已作废成绩记录: {}。".format(result["source_record_id"]))
    if result["result_count"]:
        print("注意：该赛事已有结算结果，作废后请重新执行“结算比赛”以刷新最终排名。")


def manual_bind_verse_record(connection, event_code, username=None):
    print("")
    print("按补录信息匹配 Verse 记录")
    if not username:
        username = choose_registered_player(connection, event_code)
    if not username:
        return
    session_id = choose_attempt_session(connection, event_code, username)
    if session_id is None:
        return
    score = prompt_int("补录显示的分数 score")
    ended_at = prompt_event_time("补录显示的终局时间")
    max_tile_value = prompt_optional_int("补录显示的盘面最大数（可空）")
    user_id = prompt("补录显示的用户ID（可空）", allow_empty=True) or None
    tolerance_minutes = prompt_int("终局时间容差（分钟，通常只用于兼容秒数偏差）", 2)

    result = find_attempt_candidates_by_result(
        connection,
        event_code,
        username,
        score=score,
        ended_at=ended_at,
        max_tile_value=max_tile_value,
        user_id=user_id,
        tolerance_minutes=tolerance_minutes,
        session_id=session_id,
    )
    session = result["session"]
    candidates = result["candidates"]
    print(
        "session={} status={} start={} deadline={}".format(
            session["id"],
            attempt_status_label(session["status"]),
            session["start_command_time"],
            session["lock_deadline_time"],
        )
    )
    if not candidates:
        print("没有找到匹配的 Verse 记录。")
        print("可以核对终局时间、分数，或适当放宽容差后再试。")
        return

    print("找到 {} 条候选记录：".format(len(candidates)))
    options = []
    for index, item in enumerate(candidates, start=1):
        label = "{} | score={} | ended={} | delta={}s | rec={} | max={} | user_id={}".format(
            item["source_record_id"],
            item["score"],
            item["ended_at"] or "-",
            item["end_time_delta_seconds"] if item["end_time_delta_seconds"] is not None else "-",
            item["recommendation_score"],
            item["max_tile_value"] if item["max_tile_value"] is not None else "-",
            item["user_id"] or "-",
        )
        print("{}. {}".format(index, label))
        print("   started={}".format(item["started_at"] or "-"))
        print(
            "   window={} start_offset={}s deadline_overrun={}s".format(
                "yes" if item.get("within_lock_window") else "no",
                item["start_offset_seconds"] if item.get("start_offset_seconds") is not None else "-",
                item["deadline_overrun_seconds"] if item.get("deadline_overrun_seconds") is not None else "-",
            )
        )
        print(
            "   exact_second={} exact_minute={} max_tile_match={} user_id_match={}".format(
                "yes" if item.get("exact_second_match") else "no",
                "yes" if item.get("exact_minute_match") else "no",
                item.get("max_tile_match") or "-",
                item.get("user_id_match") or "-",
            )
        )
        print("   tags={}".format(", ".join(item["match_tags"]) if item.get("match_tags") else "-"))
        print("   payload_keys={}".format(", ".join(item["payload_keys"]) if item.get("payload_keys") else "-"))
        options.append({"label": label, "value": item["source_record_id"]})

    selected_record_id = options[0]["value"] if len(options) == 1 else choose_from_list("选择要绑定的 Verse 记录", options)
    if selected_record_id is None:
        print("已取消绑定。")
        return
    if len(options) > 1:
        print("已按综合推荐度排序；通常优先看 rec 更高、同时 in_window / exact_minute 的候选。")
    with transaction(connection):
        bound = bind_attempt_record(connection, event_code, username, selected_record_id, session_id=session["id"], max_pages=30)
    print("已绑定局 {}，分数 {}。".format(bound["source_record_id"], bound["score"]))


def registration_menu(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code

    while True:
        print("")
        print("=== 报名管理 | {} ===".format(event_code))
        print("1. 手动添加报名")
        print("2. 从文件导入报名名单")
        print("3. 查看已报名名单")
        print("4. 取消报名")
        print("0. 返回")
        choice = prompt("请选择操作", "0")

        if choice == "1":
            username = prompt("选手平台用户名", allow_cancel=True)
            if username is None:
                print("已取消报名。")
                continue
            display_name = prompt("显示名", username, allow_cancel=True)
            if display_name is None:
                print("已取消报名。")
                continue
            via = prompt("报名来源", "manual", allow_cancel=True)
            if via is None:
                print("已取消报名。")
                continue
            with transaction(connection):
                result = register_player(
                    connection,
                    event_code,
                    username,
                    display_name=display_name,
                    registered_via=via,
                    enforce_deadline=False,
                )
            print("已报名: {} ({})".format(result["display_name"], result["username"]))
        elif choice == "2":
            file_path = Path(prompt("报名文件路径")).expanduser().resolve()
            players = load_players(file_path)
            added = 0
            with transaction(connection):
                for player in players:
                    register_player(
                        connection,
                        event_code,
                        player["username"],
                        registered_via="file_import",
                        enforce_deadline=False,
                    )
                    added += 1
            print("已从文件导入 {} 名选手。".format(added))
            print("已预留报名来源，后面接 bot 时会走同一张报名表。")
        elif choice == "3":
            payload = list_registrations(connection, event_code)
            rows = [row for row in payload["rows"] if row["status"] == "active"]
            if not rows:
                print("当前还没有已报名选手。")
            else:
                print("已报名名单")
                for index, row in enumerate(rows, start=1):
                    print(
                        "{}. {} | {} | {} | {}".format(
                            index,
                            row["display_name"],
                            row["username"] or "-",
                            row["registered_via"] or "-",
                            row["status"],
                        )
                    )
        elif choice == "4":
            username = choose_registered_player(connection, event_code)
            if not username:
                continue
            if not prompt_yes_no("确认取消 {} 的报名吗".format(username), False):
                continue
            with transaction(connection):
                cancel_registration(connection, event_code, username, enforce_deadline=False)
            print("已取消报名: {}".format(username))
        elif choice == "0":
            return event_code
        else:
            print("无效选项，请重新输入。")


def lock_menu(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code

    while True:
        print("")
        print("=== 跟踪锁局管理 | {} ===".format(event_code))
        print("说明：当前逻辑是跟踪锁定后出现的下一条 Verse 记录，用于自动识别或后续人工绑定。")
        print("1. 开始跟踪选手下一条记录")
        print("2. 处理跟踪会话")
        print("3. 查看跟踪会话")
        print("4. 查看候选记录")
        print("5. 按补录信息匹配并绑定 verse 记录")
        print("6. 回放证据链操作（早期/终局/校验/复核）")
        print("7. 检查 Verse API 字段（是否包含回放信息）")
        print("8. 从 Discord 自动匹配终局回放并录成绩")
        print("0. 返回")
        choice = prompt("请选择操作", "0")

        if choice == "1":
            start_tracking_attempt(connection, event_code)
        elif choice == "2":
            process_tracking_sessions(connection, event_code)
        elif choice == "3":
            show_attempt_sessions(connection, event_code)
        elif choice == "4":
            show_attempt_candidates(connection, event_code)
        elif choice == "5":
            manual_bind_verse_record(connection, event_code)
        elif choice == "6":
            replay_lock_chain_menu(connection, event_code)
        elif choice == "7":
            inspect_verse_api_fields_for_player(connection, event_code)
        elif choice == "8":
            manual_refresh_locking_player_from_discord(connection, event_code)
        elif choice == "0":
            return event_code
        else:
            print("无效选项，请重新输入。")


def _print_discord_refresh_outcome(outcome):
    if not outcome.get("enabled"):
        print("Discord 自动匹配未执行: {}".format(outcome.get("reason") or "unknown"))
        sync = outcome.get("sync") or {}
        if sync.get("reason"):
            print("同步状态: {}".format(sync["reason"]))
        return

    print(
        "Discord 同步完成: 处理{} 命中{} 需人工{} 未命中{} 跳过{}".format(
            outcome.get("processed_sessions", 0),
            outcome.get("matched_sessions", 0),
            outcome.get("manual_required_sessions", 0),
            outcome.get("no_match_sessions", 0),
            outcome.get("skipped_sessions", 0),
        )
    )
    for item in outcome.get("details") or []:
        line = "session={} status={}".format(item.get("session_id"), item.get("status"))
        if item.get("reason"):
            line += " reason={}".format(item["reason"])
        if item.get("score") is not None:
            line += " score={}".format(item["score"])
        if item.get("message_id"):
            line += " message={}".format(item["message_id"])
        if item.get("candidate_scope"):
            line += " scope={}".format(item["candidate_scope"])
        print(line)


def manual_refresh_locking_player_from_discord(connection, event_code, username=None):
    print("")
    print("从 Discord 自动匹配终局回放并录成绩")
    if not username:
        username = choose_registered_player(connection, event_code)
    if not username:
        return
    detail = get_player_progress_detail(connection, event_code, username)
    player = detail["player"]
    if not player:
        print("未找到该选手。")
        return
    print("选手: {} ({})".format(player["display_name"], player["username"] or "-"))
    print("说明：该功能会同步 Discord review 频道，查找这名选手新发的终局回放，并尝试用 early 回放做前缀匹配。")
    with transaction(connection):
        outcome = refresh_locking_player_scores_from_discord(
            connection,
            event_code,
            player_id=player["player_id"],
        )
    _print_discord_refresh_outcome(outcome)


def manual_record_score_from_discord_final_replay(connection, event_code):
    print("")
    print("提供终局回放，去 Discord 匹配并录成绩")
    username = choose_registered_player(connection, event_code)
    if not username:
        return
    replay_path_text = prompt("终局回放文件路径", allow_cancel=True)
    if replay_path_text is None:
        print("已取消。")
        return
    replay_path_text = str(replay_path_text).strip().strip('"').strip("'")
    replay_path = Path(replay_path_text).expanduser().resolve()
    print("选手: {}".format(username))
    print("说明：系统会同步 Discord review 频道，用这份终局回放的 sha256 精确匹配 Discord 附件；命中且用户名一致时直接录成绩。")
    with transaction(connection):
        outcome = record_score_from_discord_final_replay(
            connection,
            event_code,
            username=username,
            replay_file_path=str(replay_path),
        )
    if not outcome.get("enabled"):
        print("未执行: {}".format(outcome.get("reason") or "unknown"))
        sync = outcome.get("sync") or {}
        if sync.get("reason"):
            print("同步状态: {}".format(sync["reason"]))
        return
    if outcome.get("reason") == "matched":
        print(
            "匹配成功: score={} message={} source_record={}".format(
                outcome.get("score"),
                outcome.get("message_id"),
                outcome.get("source_record_id"),
            )
        )
        return
    print("未自动录入: {}".format(outcome.get("reason") or "unknown"))
    if outcome.get("matched_usernames"):
        print("同回放在 Discord 中命中的用户名: {}".format(", ".join(outcome["matched_usernames"])))
    if outcome.get("message_ids"):
        print("候选消息: {}".format(", ".join(outcome["message_ids"])))


def show_suspicious_locking_records(connection, event_code):
    print("")
    print("疑似未锁局误计成绩")
    payload = list_suspicious_locking_records(connection, event_code)
    if payload.get("reason") == "event_not_locking":
        print("当前比赛不是锁局赛。")
        return
    rows = payload.get("rows") or []
    if not rows:
        print("当前没有发现疑似误计入的非锁局成绩。")
        return
    for row in rows:
        score = row["final_score"] if row["final_score"] is not None else row["raw_score"]
        print(
            "{} ({}) | score={} | {} -> {} | record_type={} | source={}".format(
                row["display_name"] or "-",
                row["account_name"] or "-",
                score if score is not None else "-",
                row["started_at"] or "-",
                row["ended_at"] or "-",
                row["record_type"] or "-",
                row["source_record_id"] or "-",
            )
        )


def score_menu(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code

    while True:
        print("")
        print("=== 成绩管理 | {} ===".format(event_code))
        print("1. 导入成绩文件")
        print("2. 导入限时赛总排名 JSON")
        print("3. 手动录入/补录单条成绩")
        print("4. 待审核成绩")
        print("5. 作废错误成绩记录")
        print("6. 查看参赛选手状态总览")
        print("7. 查看单个选手赛中信息")
        event_row = connection.execute(
            """
            SELECT
                e.competition_type,
                e.metadata_json,
                p.code AS platform_code,
                v.code AS variant_code
            FROM events e
            JOIN platforms p ON p.id = e.platform_id
            LEFT JOIN variants v ON v.id = e.variant_id
            WHERE e.event_code = ?
            """,
            (event_code,),
        ).fetchone()
        show_manual_refresh = bool(event_row) and event_supports_manual_verse_refresh(event_row)
        is_reservation_event = bool(event_row) and is_reservation_timed_event(event_row)
        supports_locking = bool(event_row) and event_supports_locking(event_row)
        if supports_locking:
            print("8. 提供终局回放，去 Discord 匹配并录成绩")
            print("11. 查看疑似未锁局误计成绩")
        if show_manual_refresh:
            print("9. 从 Verse 手动刷新当前赛事成绩")
        if is_reservation_event:
            print("10. 重建预约型限时赛结算（V2）")
        print("0. 返回")
        choice = prompt("请选择操作", "0")

        if choice == "1":
            file_path = Path(prompt("成绩文件路径")).expanduser().resolve()
            with transaction(connection):
                result = import_score_file(connection, event_code, file_path)
            print("已导入 {} 条记录，新建玩家 {} 名。".format(result["record_count"], result["new_players"]))
        elif choice == "2":
            file_path = Path(prompt("限时赛总排名 JSON 路径")).expanduser().resolve()
            with transaction(connection):
                result = ingest_organizer_json_file(connection, file_path)
            print("已导入限时赛结果：event_id={}，结果 {} 条，新建玩家 {} 名。".format(result["event_id"], result["result_count"], result["new_players"]))
        elif choice == "3":
            manual_score_entry(connection, event_code)
        elif choice == "4":
            pending_score_review_menu(connection, event_code)
        elif choice == "5":
            void_score_entry(connection, event_code)
        elif choice == "6":
            view_participant_from_overview(connection, event_code)
        elif choice == "7":
            snapshot = build_event_progress_snapshot(connection, event_code)
            username = choose_participant_from_snapshot(snapshot)
            if not username:
                continue
            show_player_detail(connection, event_code, username)
        elif choice == "8" and supports_locking:
            manual_record_score_from_discord_final_replay(connection, event_code)
        elif choice == "9" and show_manual_refresh:
            if is_reservation_timed_event(event_row):
                result = settle_due_reservations(connection, event_code=event_code)
                print("预约结算刷新完成：结算 {} 人，影响赛事 {} 场。".format(result["settled_count"], result["event_count"]))
            else:
                refresh_scores_from_verse(connection, event_code)
        elif choice == "10" and is_reservation_event:
            result = settle_due_reservations(connection, event_code=event_code)
            print("预约型限时赛结算重建完成：结算 {} 人，影响赛事 {} 场。".format(result["settled_count"], result["event_count"]))
        elif choice == "11" and supports_locking:
            show_suspicious_locking_records(connection, event_code)
        elif choice == "0":
            return event_code
        else:
            print("无效选项，请重新输入。")


def _parse_local_dt(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace(" ", "T"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TIMEZONE)
    return dt.astimezone(LOCAL_TIMEZONE)


def refresh_scores_from_verse(connection, event_code):
    event = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.start_time,
            e.end_time,
            e.platform_id,
            e.variant_id,
            e.competition_type,
            p.code AS platform_code,
            v.code AS variant_code
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if event is None:
        print("比赛不存在：{}".format(event_code))
        return
    if event["platform_code"] != "2048verse":
        print("当前只支持 2048verse 赛事刷新。")
        return

    start_dt = _parse_local_dt(event["start_time"])
    end_dt = _parse_local_dt(event["end_time"])
    if start_dt is None:
        print("比赛未设置开始时间，无法确定抓取窗口。")
        return
    if not event["variant_code"]:
        print("比赛未配置 variant，无法从 Verse 拉取。")
        return

    players = connection.execute(
        """
        SELECT
            pa.account_key AS username,
            p.display_name
        FROM registrations r
        JOIN players p ON p.id = r.player_id
        JOIN player_accounts pa
            ON pa.player_id = r.player_id
           AND pa.platform_id = ?
           AND pa.is_primary = 1
        WHERE r.event_id = ?
          AND r.status = 'active'
        ORDER BY r.id ASC
        """,
        (event["platform_id"], event["id"]),
    ).fetchall()
    if not players:
        print("当前比赛没有报名选手。")
        return

    event_row = {
        "id": event["id"],
        "event_code": event["event_code"],
        "platform_id": event["platform_id"],
        "variant_id": event["variant_id"],
    }
    seen_source_ids = set()
    inserted = 0
    scanned = 0
    max_pages = 50
    player_batches = []
    for player in players:
        games = fetch_recent_games(player["username"], event["variant_code"], start_dt, max_pages=max_pages)
        player_rows = []
        for game in games:
            source_id = str(game.get("id") or "")
            if not source_id or source_id in seen_source_ids:
                continue
            seen_source_ids.add(source_id)
            start_time = get_game_start_time(game)
            end_time = get_game_end_time(game)
            candidate_time = start_time or end_time
            if candidate_time is None:
                continue
            if candidate_time < start_dt:
                continue
            if end_dt is not None and candidate_time > end_dt:
                continue
            scanned += 1
            score_value = int(game.get("score") or 0)
            competition_score = score_value
            if event["competition_type"] == "points_series_3x4":
                board_sum = get_game_terminal_board_sum(game)
                if board_sum is not None:
                    competition_score = board_sum
            player_rows.append(
                {
                    "username": player["username"],
                    "display_name": player["display_name"],
                    "source_record_id": source_id,
                    "record_type": "verse_manual_refresh",
                    "started_at": (start_time or candidate_time).replace(tzinfo=None).isoformat(timespec="seconds"),
                    "ended_at": (end_time or candidate_time).replace(tzinfo=None).isoformat(timespec="seconds"),
                    "raw_score": score_value,
                    "final_score": score_value,
                    "competition_score": competition_score,
                    "primary_time_ms": None,
                    "target_tile_value": None,
                    "score_before_target": None,
                    "result_state": "completed" if end_time is not None else "in_progress",
                    "evidence": None,
                    "raw_payload": game,
                }
            )
        if player_rows:
            player_batches.append(player_rows)

    for player_rows in player_batches:
        with transaction(connection):
            for row in player_rows:
                existing = connection.execute(
                    "SELECT id FROM performance_records WHERE platform_id = ? AND source_record_id = ?",
                    (event["platform_id"], row["source_record_id"]),
                ).fetchone()
                saved = upsert_performance_record(connection, event_row, row)
                upsert_event_attempt_record(
                    connection,
                    event_id=event["id"],
                    player_id=saved["player_id"],
                    performance_record_id=saved["performance_record_id"],
                    row=row,
                )
                if existing is None:
                    inserted += 1
    print("Verse 刷新完成：扫描 {} 条，新增 {} 条（其余为更新/已存在）。".format(scanned, inserted))


def interim_menu(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code

    while True:
        print("")
        print("=== 中途排名与导出 | {} ===".format(event_code))
        print("1. 查看当前临时排名")
        print("2. 导出当前排名图片")
        print("0. 返回")
        choice = prompt("请选择操作", "0")

        if choice == "1":
            settle_due_reservations(connection, event_code=event_code)
            preview = preview_ranking(connection, event_code)
            rows = preview["rows"]
            if not rows:
                print("当前还没有可用于排名的记录。")
                continue
            snapshot = build_event_progress_snapshot(connection, event_code)
            participant_by_username = {
                item["username"]: item
                for item in snapshot["participants"]
                if item.get("username")
            }
            print("")
            print("当前临时排名")
            supports_locking = event_supports_locking(preview["event"])
            detail_candidates = []
            for row in rows:
                account_name = row["account_name"] if "account_name" in row.keys() else None
                participant = participant_by_username.get(account_name)
                detail_candidates.append(account_name if account_name else None)
                display_index = len(detail_candidates)
                if supports_locking:
                    lock_status = participant.get("lock_status_short") if participant else "-"
                    print(
                        "{}. [{}] {} | {} | 当前成绩 {} | 已计局数 {} | 最佳单局 {}".format(
                            row["rank_value"],
                            display_index,
                            row["display_name"],
                            lock_status,
                            row["primary_metric_value"] if row["primary_metric_value"] is not None else "-",
                            row["secondary_metric_value"] or 0,
                            row["best_single_score"] if row["best_single_score"] is not None else "-",
                        )
                    )
                else:
                    print(
                        "{}. [{}] {} | 当前成绩 {} | 已计局数 {} | 最佳单局 {}".format(
                            row["rank_value"],
                            display_index,
                            row["display_name"],
                            row["primary_metric_value"] if row["primary_metric_value"] is not None else "-",
                            row["secondary_metric_value"] or 0,
                            row["best_single_score"] if row["best_single_score"] is not None else "-",
                        )
                    )
            if detail_candidates:
                while True:
                    detail_choice = prompt("输入选手序号查看详情，直接回车返回", allow_empty=True)
                    if not detail_choice:
                        break
                    try:
                        selected = int(detail_choice)
                    except ValueError:
                        print("请输入序号。")
                        continue
                    if selected < 1 or selected > len(detail_candidates):
                        print("超出范围，请重新输入。")
                        continue
                    username = detail_candidates[selected - 1]
                    if not username:
                        print("该选手没有可用的平台用户名，暂时无法查看详情。")
                        break
                    show_player_detail(connection, event_code, username)
                    break
        elif choice == "2":
            settle_due_reservations(connection, event_code=event_code)
            result = export_interim_ranking_image(connection, event_code)
            print("已导出当前排名图: {}".format(result["image_path"]))
        elif choice == "0":
            return event_code
        else:
            print("无效选项，请重新输入。")


def run_settlement_precheck_preview(connection, event_code):
    summary = fetch_event_summary(connection, event_code)
    counts = summary["counts"]
    supports_locking = event_supports_locking(summary["event"])
    print("")
    print("结算预检查: {}".format(event_code))
    print("报名人数: {}".format(counts["registration_count"]))
    print("候选成绩数: {}".format(counts["attempt_record_count"]))
    if supports_locking:
        print("等待识别: {}".format(counts["pending_lock_count"]))
        print("已识别，待完赛: {}".format(counts["locked_in_progress_count"]))
    precheck = run_settlement_precheck(connection, event_code)
    print("")
    print("预检查结果")
    if precheck["blockers"]:
        print("阻断项（{}）".format(len(precheck["blockers"])))
        for item in precheck["blockers"]:
            print("- {}".format(item))
    else:
        print("阻断项：无")
    if precheck["warnings"]:
        print("提醒（{}）".format(len(precheck["warnings"])))
        for item in precheck["warnings"]:
            print("- {}".format(item))
    else:
        print("提醒：无")
    if precheck["infos"]:
        print("补充信息")
        for item in precheck["infos"]:
            print("- {}".format(item))
    return precheck


def settle_current_event(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code
    summary = fetch_event_summary(connection, event_code)
    settle_due_reservations(connection, event_code=event_code)
    precheck = run_settlement_precheck_preview(connection, event_code)
    if precheck["blockers"]:
        print("请先处理阻断项，再重新执行结算。")
        return event_code
    if not prompt_yes_no("确认现在结算这场比赛吗", True):
        return event_code
    with transaction(connection):
        result = settle_event(connection, event_code)
        export_result = export_final_ranking(connection, event_code)
        rating_result = None
        if summary["event"]["is_rated"]:
            rating_result = recalculate_event_bucket_ratings(connection, event_code)
    print(
        "已结算 {}，选手 {} 人，候选记录 {} 条，聚合方式 {}。".format(
            result["event_code"],
            result["player_count"],
            result["record_count"],
            result["aggregation_method"],
        )
    )
    print("已导出最终结果到: {}（玩家明细 {} 份）".format(export_result["output_dir"], export_result["detail_count"]))
    if rating_result:
        print(
            "已重算当前 rating bucket：赛事 {} 场，选手 {} 人，历史记录 {} 条。".format(
                rating_result["event_count"],
                rating_result["player_count"],
                rating_result["history_count"],
            )
        )
    return event_code


def force_end_current_event(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code
    summary = fetch_event_summary(connection, event_code)
    event = summary["event"]
    end_time = datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None).isoformat(timespec="seconds")
    print("")
    print("准备强制结束比赛: {}".format(event_code))
    print("名称: {}".format(event["event_name"]))
    print("原时间: {} -> {}".format(event["start_time"] or "-", event["end_time"] or "-"))
    print("新的结束时间: {}".format(end_time))
    print("这只会修改比赛结束时间和状态，不会自动结算或导出最终结果。")
    confirm = prompt("请输入 END 确认强制结束", allow_empty=True)
    if confirm != "END":
        print("已取消强制结束。")
        return event_code

    with transaction(connection):
        connection.execute(
            """
            UPDATE events
            SET end_time = ?,
                status = 'finished',
                updated_at = ?
            WHERE event_code = ?
            """,
            (end_time, end_time, event_code),
        )
        connection.execute(
            """
            UPDATE attempt_sessions
            SET status = 'expired',
                updated_at = ?
            WHERE event_id = ?
              AND status IN ('pending_lock', 'locked_in_progress')
            """,
            (end_time, event["id"]),
        )
    print("已强制结束比赛: {}。之后可以执行“结算比赛”。".format(event_code))
    return event_code


def export_current_event_results(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code
    result = export_final_ranking(connection, event_code)
    print("")
    print("已导出最终结果")
    print("目录: {}".format(result["output_dir"]))
    print("TXT: {}".format(result["txt_path"]))
    print("JSON: {}".format(result["json_path"]))
    print("PNG: {}".format(result["png_path"]))
    print("玩家明细: {} 份".format(result["detail_count"]))
    return event_code


def show_current_event_unfinished(connection, current_event_code):
    event_code = require_event(connection, current_event_code)
    if not event_code:
        return current_event_code
    show_unfinished_participants(connection, event_code)
    return event_code


def show_reservation_status_overview(connection, event_code):
    settle_due_reservations(connection, event_code=event_code)
    try:
        result = list_event_reservation_status(connection, event_code=event_code)
    except ValueError as exc:
        print(str(exc))
        return
    rows = result["rows"]
    print("")
    print("预约情况 | {} {}".format(result["event_code"], result["event_name"]))
    print(
        "赛事时间窗: {} -> {} | 个人时长: {} 分钟".format(
            result["event_start_time"] or "-",
            result["event_end_time"] or "-",
            result["reservation_duration_minutes"],
        )
    )
    if not rows:
        print("当前没有已报名选手。")
        return
    status_labels = {
        "unreserved": "未预约",
        "reserved": "已预约",
        "confirmed_late": "已预约",
        "settled": "已结算",
        "cancelled": "未预约",
    }
    now_dt = datetime.now(LOCAL_TIMEZONE)
    for index, row in enumerate(rows, start=1):
        username = row["username"] or "-"
        status_code = row["status"]
        status_text = status_labels.get(status_code, status_code)
        if status_code in {"reserved", "confirmed_late"}:
            start_dt = parse_event_time(row["reserved_start_time"])
            end_dt = parse_event_time(row["reserved_end_time"])
            if start_dt is not None and end_dt is not None and start_dt <= now_dt < end_dt:
                status_text = "进行中"
        if row["status"] == "unreserved":
            print("{}. {} | {} | {}".format(index, row["display_name"], username, status_text))
            continue
        window_text = "{} -> {}".format(row["reserved_start_time"] or "-", row["reserved_end_time"] or "-")
        extra = ""
        if row["status"] == "settled":
            extra = " | best_score {}".format(row["best_score"] if row["best_score"] is not None else "-")
        print("{}. {} | {} | {} | {}{}".format(index, row["display_name"], username, window_text, status_text, extra))


def manual_reservation_entry(connection, event_code):
    print("")
    print("手动录入/修改选手预约")
    username = choose_registered_player(connection, event_code)
    if not username:
        username = prompt("选手 verse 用户名", allow_cancel=True)
        if username is None:
            print("已取消录入。")
            return
    reserved_start_text = prompt_event_time("预约开始时间")
    if reserved_start_text is None:
        print("已取消录入。")
        return
    reserved_start_dt = parse_event_time(reserved_start_text)
    note = prompt("备注（可空，例如私聊来源/截图说明）", allow_empty=True) or None
    operator = "organizer_event_hub"
    try:
        preview = reserve_for_registered_player(
            connection,
            event_code=event_code,
            username=username,
            reserved_start_dt=reserved_start_dt,
            late_confirmed=False,
            operator=operator,
            note=note,
        )
    except ValueError as exc:
        print("录入失败：{}".format(exc))
        return
    if preview.get("requires_confirm"):
        confirmed = prompt_yes_no("该预约开始时间早于当前时间，确认仍要录入并标记为已晚到确认吗", False)
        if not confirmed:
            print("已取消录入。")
            return
        try:
            with transaction(connection):
                result = reserve_for_registered_player(
                    connection,
                    event_code=event_code,
                    username=username,
                    reserved_start_dt=reserved_start_dt,
                    late_confirmed=True,
                    operator=operator,
                    note=note,
                )
        except ValueError as exc:
            print("录入失败：{}".format(exc))
            return
    else:
        try:
            with transaction(connection):
                result = reserve_for_registered_player(
                    connection,
                    event_code=event_code,
                    username=username,
                    reserved_start_dt=reserved_start_dt,
                    late_confirmed=False,
                    operator=operator,
                    note=note,
                )
        except ValueError as exc:
            print("录入失败：{}".format(exc))
            return
    print(
        "已录入预约：{} ({}) | {} -> {} | 状态 {}".format(
            result["display_name"],
            result["username"],
            result["reserved_start_time"],
            result["reserved_end_time"],
            result["status"],
        )
    )


def manual_update_registered_username(connection, event_code):
    print("")
    print("修改选手平台用户名")
    old_username = choose_registered_player(connection, event_code)
    if not old_username:
        old_username = prompt("当前旧用户名", allow_cancel=True)
        if old_username is None:
            print("已取消修改。")
            return
    new_username = prompt("新用户名", allow_cancel=True)
    if new_username is None:
        print("已取消修改。")
        return
    new_username = str(new_username).strip()
    if not new_username:
        print("新用户名不能为空。")
        return
    if not prompt_yes_no("确认把 {} 改成 {} 吗".format(old_username, new_username), False):
        print("已取消修改。")
        return
    try:
        with transaction(connection):
            result = update_registered_player_username(connection, event_code, old_username, new_username)
    except ValueError as exc:
        print("修改失败：{}".format(exc))
        return
    print(
        "已更新用户名：{} | {} -> {}".format(
            result["display_name"],
            result["old_username"],
            result["new_username"],
        )
    )


def show_event_list(connection):
    grouped = fetch_recent_events(connection, include_hidden=True, include_archived=True, limit=200)
    print("")
    print("赛事列表（含历史隐藏/已归档）")
    total_count = sum(len(rows) for rows in grouped.values())
    if not total_count:
        print("当前没有赛事。")
        return
    for label in ("进行中", "待开始", "已完赛", "未设时间", "历史隐藏", "已归档"):
        rows = grouped.get(label, [])
        if not rows:
            continue
        print("{}（{}）".format(label, len(rows)))
        for row in rows:
            print(
                "- {} | {} | {} | {} | {} -> {} | 状态 {} | 报名{} 已结算{} 待识别{} 识别中{}".format(
                    row["event_code"],
                    row["event_name"],
                    row["platform_code"],
                    row["competition_type"],
                    row["start_time"] or "-",
                    row["end_time"] or "-",
                    row["status"],
                    row["registration_count"],
                    row["result_count"],
                    row["pending_lock_count"],
                    row["locked_in_progress_count"],
                )
            )


def show_hidden_event_list(connection):
    grouped = fetch_recent_events(connection, include_hidden=True, include_archived=True, limit=200)
    print("")
    print("隐藏赛事（历史隐藏 + 已归档）")
    any_rows = False
    for label in ("历史隐藏", "已归档"):
        rows = grouped.get(label, [])
        if not rows:
            continue
        any_rows = True
        print("{}（{}）".format(label, len(rows)))
        for row in rows:
            print(
                "- {} | {} | {} -> {} | 状态 {}".format(
                    row["event_code"],
                    row["event_name"],
                    row["start_time"] or "-",
                    row["end_time"] or "-",
                    row["status"],
                )
            )
    if not any_rows:
        print("当前没有隐藏赛事。")


def archive_event(connection, event_code, actor_id="organizer_event_hub"):
    row = connection.execute("SELECT id, status FROM events WHERE event_code = ?", (event_code,)).fetchone()
    if row is None:
        raise ValueError("赛事不存在: {}".format(event_code))
    now_text = datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None).isoformat(timespec="seconds")
    connection.execute(
        "UPDATE events SET status = 'archived', updated_at = ? WHERE event_code = ?",
        (now_text, event_code),
    )
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_type, actor_id, action_type, target_table, target_id,
            reason, before_json, after_json, created_at
        )
        VALUES ('organizer_cli', ?, 'archive_event', 'events', ?, ?, ?, ?, ?)
        """,
        (
            actor_id,
            row["id"],
            "archive from organizer menu",
            json.dumps({"status": row["status"]}, ensure_ascii=False),
            json.dumps({"status": "archived"}, ensure_ascii=False),
            now_text,
        ),
    )


def restore_archived_event(connection, event_code, actor_id="organizer_event_hub"):
    row = connection.execute("SELECT id, status FROM events WHERE event_code = ?", (event_code,)).fetchone()
    if row is None:
        raise ValueError("赛事不存在: {}".format(event_code))
    now_text = datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None).isoformat(timespec="seconds")
    restored_status = "finished"
    connection.execute(
        "UPDATE events SET status = ?, updated_at = ? WHERE event_code = ?",
        (restored_status, now_text, event_code),
    )
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_type, actor_id, action_type, target_table, target_id,
            reason, before_json, after_json, created_at
        )
        VALUES ('organizer_cli', ?, 'restore_archived_event', 'events', ?, ?, ?, ?, ?)
        """,
        (
            actor_id,
            row["id"],
            "restore from organizer menu",
            json.dumps({"status": row["status"]}, ensure_ascii=False),
            json.dumps({"status": restored_status}, ensure_ascii=False),
            now_text,
        ),
    )


def event_dependency_counts(connection, event_code):
    row = connection.execute("SELECT id FROM events WHERE event_code = ?", (event_code,)).fetchone()
    if row is None:
        raise ValueError("赛事不存在: {}".format(event_code))
    event_id = row["id"]
    checks = {
        "registrations": "SELECT COUNT(*) AS c FROM registrations WHERE event_id = ?",
        "attempt_sessions": "SELECT COUNT(*) AS c FROM attempt_sessions WHERE event_id = ?",
        "event_attempt_records": "SELECT COUNT(*) AS c FROM event_attempt_records WHERE event_id = ?",
        "event_results": "SELECT COUNT(*) AS c FROM event_results WHERE event_id = ?",
        "result_snapshots": "SELECT COUNT(*) AS c FROM result_snapshots WHERE event_id = ?",
        "pending_score_submissions": "SELECT COUNT(*) AS c FROM pending_score_submissions WHERE event_id = ?",
        "sync_runs": "SELECT COUNT(*) AS c FROM sync_runs WHERE event_id = ?",
        "rating_history": "SELECT COUNT(*) AS c FROM rating_history WHERE event_id = ?",
    }
    result = {}
    for key, sql in checks.items():
        result[key] = connection.execute(sql, (event_id,)).fetchone()["c"]
    result["event_rule_sets"] = connection.execute("SELECT COUNT(*) AS c FROM event_rule_sets WHERE event_id = ?", (event_id,)).fetchone()["c"]
    result["safe_to_delete"] = all(count == 0 for key, count in result.items() if key != "safe_to_delete" and key != "event_rule_sets")
    result["event_id"] = event_id
    return result


def delete_event_if_safe(connection, event_code):
    dep = event_dependency_counts(connection, event_code)
    if not dep["safe_to_delete"]:
        raise ValueError("该赛事存在依赖数据，禁止硬删除，建议改用归档。")
    connection.execute("DELETE FROM event_rule_sets WHERE event_id = ?", (dep["event_id"],))
    connection.execute("DELETE FROM events WHERE id = ?", (dep["event_id"],))


def hidden_events_menu(connection, current_event_code):
    while True:
        print("")
        print("=== 赛事归档与删除 ===")
        print("1. 归档当前比赛")
        print("2. 恢复已归档比赛")
        print("3. 删除向导（仅无依赖赛事）")
        print("0. 返回")
        choice = prompt("请选择操作", "0")
        if choice == "1":
            event_code = require_event(connection, current_event_code)
            if not event_code:
                continue
            if not prompt_yes_no("确认归档当前比赛 {} 吗".format(event_code), False):
                continue
            with transaction(connection):
                archive_event(connection, event_code)
            print("已归档: {}".format(event_code))
        elif choice == "2":
            grouped = fetch_recent_events(connection, include_hidden=True, include_archived=True, limit=200)
            archived_rows = grouped.get("已归档", [])
            if not archived_rows:
                print("当前没有已归档比赛。")
                continue
            selected = choose_from_list(
                "选择要恢复的比赛",
                [{"label": "{} | {}".format(row["event_code"], row["event_name"]), "value": row["event_code"]} for row in archived_rows],
                default_index=0,
            )
            if not selected:
                continue
            with transaction(connection):
                restore_archived_event(connection, selected)
            print("已恢复比赛: {}".format(selected))
        elif choice == "3":
            grouped = fetch_recent_events(connection, include_hidden=True, include_archived=True, limit=200)
            selected = choose_event_from_groups(grouped, "删除向导：选择比赛", current_event_code=current_event_code)
            if not selected:
                continue
            dep = event_dependency_counts(connection, selected)
            print("依赖检查：")
            for key in ("registrations", "attempt_sessions", "event_attempt_records", "event_results", "result_snapshots", "pending_score_submissions", "sync_runs", "rating_history"):
                print("- {}: {}".format(key, dep[key]))
            if not dep["safe_to_delete"]:
                print("该赛事存在依赖，禁止硬删除，建议归档。")
                continue
            if not prompt_yes_no("确认永久删除 {} 吗（不可恢复）".format(selected), False):
                continue
            with transaction(connection):
                delete_event_if_safe(connection, selected)
            print("已删除比赛: {}".format(selected))
            if current_event_code == selected:
                current_event_code = None
        elif choice == "0":
            return current_event_code
        else:
            print("无效选项，请重新输入。")


def choose_rating_bucket(connection):
    rows = list_rating_buckets(connection)
    if not rows:
        print("当前还没有 rating bucket。")
        return None
    return choose_from_list(
        "请选择榜单分类",
        [
            {
                "label": "{} | {} | rating选手{} | 已计赛事{}".format(
                    row["code"],
                    row["name"],
                    row["rated_player_count"],
                    row["rated_event_count"],
                ),
                "value": row["code"],
            }
            for row in rows
        ],
        default_index=0,
    )


def show_rating_leaderboard(connection, bucket_code):
    result = list_rating_leaderboard(connection, bucket_code, limit=50)
    rows = result["rows"]
    print("")
    print("Rating 榜 | {} ({})".format(result["bucket"]["name"], result["bucket"]["code"]))
    if not rows:
        print("当前还没有 rating 数据。可以先重算 rating。")
        return
    for index, row in enumerate(rows, start=1):
        print(
            "{}. {} | rating {:.1f} | RD {:.1f} | 参赛 {} | 最高 {:.1f} | 最近 {}".format(
                index,
                row["display_name"],
                row["rating_value"],
                row["rating_deviation"],
                row["event_count"],
                row["best_rating"] if row["best_rating"] is not None else row["rating_value"],
                row["last_event_code"] or "-",
            )
        )


def show_performance_leaderboard(connection, bucket_code):
    result = list_performance_leaderboard(connection, bucket_code, limit=50)
    rows = result["rows"]
    print("")
    print("历史成绩榜 | {} ({})".format(result["bucket"]["name"], result["bucket"]["code"]))
    if not rows:
        print("当前还没有可用于统榜的成绩。")
        return
    for index, row in enumerate(rows, start=1):
        print(
            "{}. {} | {} {} | 单局最好 {} | {}".format(
                index,
                row["display_name"],
                row["primary_metric_type"],
                row["primary_metric_value"],
                row["best_single_score"] if row["best_single_score"] is not None else "-",
                row["event_code"],
            )
        )


def choose_player_by_search(connection):
    keyword = prompt("输入选手名/账号", allow_empty=True)
    if not keyword:
        return None
    rows = find_players(connection, keyword)
    if not rows:
        print("没有找到选手。")
        return None
    return choose_from_list(
        "请选择选手",
        [
            {
                "label": "{} | id={} | accounts={} | keys={}".format(
                    row["display_name"],
                    row["id"],
                    row["account_names"] or "-",
                    row["account_keys"] or "-",
                ),
                "value": row["id"],
            }
            for row in rows
        ],
        default_index=0,
    )


def show_global_player_profile(connection):
    player_id = choose_player_by_search(connection)
    if not player_id:
        return
    profile = build_player_profile(connection, player_id)
    print("")
    print(format_player_profile(profile))


def export_global_player_profile(connection):
    player_id = choose_player_by_search(connection)
    if not player_id:
        return
    result = export_player_profile(connection, player_id)
    print("已导出选手档案: {}".format(result["output_dir"]))
    print("TXT: {}".format(result["txt_path"]))
    print("JSON: {}".format(result["json_path"]))


def leaderboard_rating_menu(connection, current_event_code):
    while True:
        print("")
        print("=== 长期统榜与 rating ===")
        print("1. 查看 rating bucket")
        print("2. 重算全部 rating")
        print("3. 重算当前比赛所在 bucket")
        print("4. 查看 rating 榜")
        print("5. 查看历史成绩榜")
        print("6. 导出长期统榜")
        print("7. 查看选手档案")
        print("8. 导出选手档案")
        print("0. 返回")
        choice = prompt("请选择操作", "0")

        if choice == "1":
            rows = list_rating_buckets(connection)
            if not rows:
                print("当前还没有 rating bucket。")
                continue
            for row in rows:
                print(
                    "- {} | {} | family={} | 已计赛事{} | rating选手{}".format(
                        row["code"],
                        row["name"],
                        row["family_code"],
                        row["rated_event_count"],
                        row["rated_player_count"],
                    )
                )
        elif choice == "2":
            if not prompt_yes_no("确认重算全部 rating 吗", True):
                continue
            with transaction(connection):
                result = recalculate_ratings(connection)
            print(
                "已重算全部 rating：赛事 {} 场，跳过 {} 场，选手 {} 人，历史记录 {} 条。".format(
                    result["event_count"],
                    result["skipped_event_count"],
                    result["player_count"],
                    result["history_count"],
                )
            )
        elif choice == "3":
            event_code = require_event(connection, current_event_code)
            if not event_code:
                continue
            with transaction(connection):
                result = recalculate_event_bucket_ratings(connection, event_code)
            print(
                "已重算当前 bucket：赛事 {} 场，跳过 {} 场，选手 {} 人，历史记录 {} 条。".format(
                    result["event_count"],
                    result["skipped_event_count"],
                    result["player_count"],
                    result["history_count"],
                )
            )
        elif choice == "4":
            bucket_code = choose_rating_bucket(connection)
            if bucket_code:
                show_rating_leaderboard(connection, bucket_code)
        elif choice == "5":
            bucket_code = choose_rating_bucket(connection)
            if bucket_code:
                show_performance_leaderboard(connection, bucket_code)
        elif choice == "6":
            bucket_code = choose_rating_bucket(connection)
            if not bucket_code:
                continue
            result = export_leaderboards(connection, bucket_code)
            print("已导出长期统榜: {}".format(result["output_dir"]))
            print("TXT: {}".format(result["txt_path"]))
            print("JSON: {}".format(result["json_path"]))
            print("PNG: {}".format(result["image_path"]))
        elif choice == "7":
            show_global_player_profile(connection)
        elif choice == "8":
            export_global_player_profile(connection)
        elif choice == "0":
            return current_event_code
        else:
            print("无效选项，请重新输入。")


def new_event_menu(connection):
    print("")
    print("新建比赛")
    modes = list_competition_modes()
    choice = choose_from_list(
        "请选择比赛类型",
        [
            {
                "label": "{} | {}{}".format(
                    mode["label"],
                    mode["code"],
                    " | 支持预约型" if mode.get("competition_type") == "timed_scoring" else "",
                ),
                "value": mode["code"],
            }
            for mode in modes
        ],
        default_index=0,
    )
    if choice is None:
        return None
    return create_mode_event(connection, choice)


def current_event_name(connection, current_event_code):
    if not current_event_code:
        return None
    row = connection.execute(
        "SELECT event_name FROM events WHERE event_code = ?",
        (current_event_code,),
    ).fetchone()
    return None if row is None else row["event_name"]


def main():
    configure_console()
    connection = ensure_hub_ready()
    current_event_code = None
    try:
        while True:
            print_header(current_event_code, current_event_name(connection, current_event_code))
            print_main_menu()
            choice = prompt("请选择操作", "0")
            try:
                if choice == "1":
                    selected = choose_event(connection, current_event_code=current_event_code)
                    if selected:
                        current_event_code = selected
                        print("已切换到比赛: {}".format(current_event_code))
                elif choice == "2":
                    created = new_event_menu(connection)
                    if created:
                        current_event_code = created
                elif choice == "3":
                    current_event_code = current_event_workbench_menu(connection, current_event_code)
                elif choice == "4":
                    show_event_list(connection)
                elif choice == "5":
                    current_event_code = leaderboard_rating_menu(connection, current_event_code)
                elif choice == "6":
                    current_event_code = hidden_events_menu(connection, current_event_code)
                elif choice == "0":
                    print("已退出。")
                    break
                else:
                    print("无效选项，请重新输入。")
            except Exception as error:
                print("操作失败：{}".format(error))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
