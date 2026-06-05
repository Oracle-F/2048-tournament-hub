import ctypes
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
import traceback

from tournament_common import (
    CHECK_INTERVAL,
    HTML_REFRESH_SECONDS,
    NOTIFICATION_SECONDS,
    build_event_info,
    build_full_board_notifications,
    event_output_dir,
    export_player_text,
    export_ranking_files,
    fetch_games_for_window,
    format_clock,
    format_datetime,
    freeze_results_at,
    get_display_name,
    get_remaining_time_text,
    get_seal_status_text,
    get_status_text,
    get_top_full_board_players,
    load_players,
    now_local,
    parse_duration_hours,
    parse_optional_seal_minutes,
    parse_start_time,
    score_games,
    sort_results,
    summarize_results,
)

BASE_DIR = Path(__file__).resolve().parent
HUB_DIR = BASE_DIR.parent
if str(HUB_DIR) not in sys.path:
    sys.path.insert(0, str(HUB_DIR))

from bootstrap import bootstrap_all
from db import connect, ensure_parent_dir, initialize_schema, transaction
from services.event_admin_service import create_or_update_event, infer_rating_bucket_code
from services.registration_service import register_player
from settings import DATABASE_PATH

HTML_FILE = BASE_DIR / "主办方比赛看板.html"
BASE_OUTPUT_DIR = BASE_DIR.parent / "比赛导出" / "主办方"
RUNTIME_LOG_FILE = BASE_DIR / "organizer_timed_scoring_runtime.log"
STATE_FILE = BASE_DIR / "organizer_timed_scoring_state.json"
CANCEL_FILE = BASE_DIR / "organizer_timed_scoring_cancel.json"
ACTIVE_SOON_WINDOW = timedelta(minutes=15)
HEARTBEAT_STALE_SECONDS = CHECK_INTERVAL * 4 + 5
CTRL_CLOSE_EVENT = 2
SW_MINIMIZE = 6

ACTIVE_EVENT_INFO = None
CONSOLE_HANDLER_REF = None


def ensure_hub_ready():
    ensure_parent_dir(DATABASE_PATH)
    connection = connect(DATABASE_PATH)
    with transaction(connection):
        initialize_schema(connection)
        bootstrap_all(connection)
    return connection



def append_runtime_log(message):
    with RUNTIME_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write("[{}] {}\n".format(format_datetime(now_local()), message))


def confirm_exit():
    while True:
        answer = input("Confirm exit and run one final sync/export? (y/n): ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please enter y or n.")


def process_is_running(pid):
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, process_id)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def parse_state_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def load_runtime_state():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def event_info_is_active_or_soon(event_info):
    if event_info is None:
        return False
    now = now_local()
    if event_info.start_time <= now <= event_info.end_time:
        return True
    return now < event_info.start_time <= now + ACTIVE_SOON_WINDOW


def state_is_active_or_soon(state):
    now = now_local()
    start_time = parse_state_time(state.get("start_time"))
    end_time = parse_state_time(state.get("end_time"))
    if start_time is None or end_time is None:
        return False
    if start_time <= now <= end_time:
        return True
    return now < start_time <= now + ACTIVE_SOON_WINDOW


def state_is_recent(state):
    updated_at = parse_state_time(state.get("updated_at"))
    if updated_at is None:
        return False
    return (now_local() - updated_at).total_seconds() <= HEARTBEAT_STALE_SECONDS


def runtime_state_is_protected(state):
    if not state:
        return False
    return (
        process_is_running(state.get("pid"))
        and state_is_active_or_soon(state)
        and state_is_recent(state)
    )


def minimize_console_window():
    window = ctypes.windll.kernel32.GetConsoleWindow()
    if window:
        ctypes.windll.user32.ShowWindow(window, SW_MINIMIZE)
        return True
    return False


def register_close_minimize_handler(event_info):
    global ACTIVE_EVENT_INFO, CONSOLE_HANDLER_REF
    ACTIVE_EVENT_INFO = event_info

    if CONSOLE_HANDLER_REF is not None:
        return

    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    @handler_type
    def console_handler(ctrl_type):
        if ctrl_type == CTRL_CLOSE_EVENT and event_info_is_active_or_soon(ACTIVE_EVENT_INFO):
            append_runtime_log('Close button intercepted during active/soon match; minimizing instead of exiting.')
            minimize_console_window()
            return True
        return False

    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(console_handler, True):
        raise OSError('Failed to register console close handler.')
    CONSOLE_HANDLER_REF = console_handler


def unregister_close_minimize_handler():
    global CONSOLE_HANDLER_REF, ACTIVE_EVENT_INFO
    ACTIVE_EVENT_INFO = None
    if CONSOLE_HANDLER_REF is not None:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(CONSOLE_HANDLER_REF, False)
        CONSOLE_HANDLER_REF = None


def write_runtime_state(event_info, root_dir):
    payload = {
        "pid": os.getpid(),
        "event_id": event_info.event_id,
        "variant": event_info.variant,
        "start_time": event_info.start_time.isoformat(),
        "end_time": event_info.end_time.isoformat(),
        "updated_at": now_local().isoformat(),
        "output_dir": str(root_dir.resolve()),
        "html_file": str(HTML_FILE.resolve()),
    }
    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup_runtime_state():
    state = load_runtime_state()
    if state and state.get("pid") == os.getpid() and STATE_FILE.exists():
        STATE_FILE.unlink()
    if CANCEL_FILE.exists():
        try:
            payload = json.loads(CANCEL_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = None
        if payload is None or payload.get("target_pid") == os.getpid():
            CANCEL_FILE.unlink()


def cancel_requested_for_current_process():
    if not CANCEL_FILE.exists():
        return False
    try:
        payload = json.loads(CANCEL_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    target_pid = payload.get("target_pid")
    return target_pid is None or target_pid == os.getpid()


def request_cancel_running_match(state):
    target_pid = state.get("pid")
    payload = {
        "target_pid": target_pid,
        "requested_at": now_local().isoformat(),
        "requested_by_pid": os.getpid(),
    }
    CANCEL_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for _ in range(20):
        sleep(0.5)
        latest = load_runtime_state()
        if latest is None or latest.get("pid") != target_pid:
            return True
        if not process_is_running(target_pid):
            return True
    return False


def prompt_existing_match_action(state):
    print("\n检测到已有一个限时赛主办方实例正在运行：")
    print("event_id: {}".format(state.get("event_id") or "-"))
    print("variant: {}".format(state.get("variant") or "-"))
    print("start: {}".format(state.get("start_time") or "-"))
    print("end: {}".format(state.get("end_time") or "-"))
    print("last heartbeat: {}".format(state.get("updated_at") or "-"))
    print("")
    print("1. 返回当前比赛（本次不新开）")
    print("2. 取消当前比赛，然后继续开新比赛")
    print("3. 强制新开（不推荐）")
    while True:
        choice = input("请选择 (1/2/3): ").strip()
        if choice in {"1", "2", "3"}:
            return choice
        print("请输入 1、2 或 3。")


def collect_results(players, variant, start_time, end_time):
    results = []
    for player in players:
        username = player["username"]
        display_name = get_display_name(username)
        games = fetch_games_for_window(username, variant, start_time, end_time)
        results.append(score_games(username, display_name, variant, games, start_time, end_time))
    return sort_results(results)


def export_all(results, event_info, root_dir):
    export_ranking_files(results, event_info, root_dir)
    details_dir = root_dir / "玩家明细"
    for result in results:
        export_player_text(result, event_info, details_dir)


def build_ranking_rows(results):
    top_full_board_players, max_full_boards = get_top_full_board_players(results)
    medal_class = {1: "gold", 2: "silver", 3: "bronze"}
    rows = []
    if not results:
        return "<tr><td colspan='5'>暂无参赛数据</td></tr>"

    for index, item in enumerate(results, start=1):
        marker = ""
        if item.username in top_full_board_players and max_full_boards > 0:
            marker = " <span class='tag'>★最多满盘</span>"
        rows.append(
            "<tr class='{}'>"
            "<td>{}</td>"
            "<td>{}{}</td>"
            "<td>{}</td>"
            "<td>{}</td>"
            "<td>{}</td>"
            "</tr>".format(
                medal_class.get(index, ""),
                index,
                item.display_name,
                marker,
                item.total_points,
                item.scoring_game_count,
                item.total_full_boards,
            )
        )
    return "".join(rows)


def write_html(event_info, players, live_results, display_results, notifications, html_path):
    now = now_local()
    total_points, total_games, total_full_boards = summarize_results(display_results)
    status_text = get_status_text(now, event_info.start_time, event_info.end_time, event_info.duration)
    remaining_text = get_remaining_time_text(now, event_info.start_time, event_info.end_time)
    seal_status = get_seal_status_text(now, event_info.seal_time)
    seal_timestamp = (
        "null" if event_info.seal_time is None else str(int(event_info.seal_time.timestamp() * 1000))
    )
    notifications_json = str(notifications).replace("'", '"')

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="{refresh}">
    <title>2048 比赛主办方看板</title>
    <style>
        :root {{
            --bg: #f5efe1;
            --panel: #fff8ee;
            --line: #ddcfb9;
            --text: #2e2418;
            --muted: #756650;
            --accent: #9f4f29;
            --accent-soft: #f1ddc0;
            --gold: #b98517;
            --silver: #7f8a97;
            --bronze: #b36d40;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
            background:
                radial-gradient(circle at top right, #fbe4bf, transparent 24%),
                linear-gradient(180deg, #f7f1e8 0%, #efe4d0 100%);
            color: var(--text);
            overflow: hidden;
        }}
        .layout {{
            display: grid;
            grid-template-columns: 320px 1fr;
            height: 100vh;
            gap: 18px;
            padding: 18px;
        }}
        .side, .main {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 22px;
            box-shadow: 0 14px 32px rgba(71, 50, 21, 0.08);
        }}
        .side {{
            padding: 16px;
            display: grid;
            grid-template-rows: auto auto 1fr;
            gap: 10px;
            overflow: hidden;
        }}
        .main {{
            padding: 16px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }}
        .title {{
            font-size: 26px;
            font-weight: 700;
            margin-bottom: 4px;
        }}
        .subtitle {{
            color: var(--muted);
            font-size: 12px;
            line-height: 1.5;
        }}
        .cards {{
            display: grid;
            gap: 8px;
            align-content: start;
        }}
        .card {{
            background: #fff;
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 10px 12px;
        }}
        .label {{
            color: var(--muted);
            font-size: 11px;
            margin-bottom: 4px;
        }}
        .value {{
            font-size: 20px;
            font-weight: 700;
        }}
        .value.small {{
            font-size: 14px;
            line-height: 1.4;
            font-weight: 600;
        }}
        h2 {{
            margin: 0 0 12px;
            font-size: 22px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: #fff;
            border-radius: 16px;
            overflow: hidden;
        }}
        th, td {{
            padding: 12px 14px;
            border-bottom: 1px solid #eee1ce;
            text-align: left;
            font-size: 15px;
        }}
        th {{
            background: var(--accent-soft);
            position: sticky;
            top: 0;
            z-index: 1;
        }}
        tr:last-child td {{ border-bottom: none; }}
        tr.gold td {{
            background: linear-gradient(90deg, rgba(233, 196, 95, 0.28), rgba(255, 248, 230, 0.94));
        }}
        tr.silver td {{
            background: linear-gradient(90deg, rgba(183, 192, 204, 0.28), rgba(251, 251, 251, 0.94));
        }}
        tr.bronze td {{
            background: linear-gradient(90deg, rgba(201, 143, 102, 0.25), rgba(255, 247, 241, 0.94));
        }}
        tr.gold td:first-child {{ color: var(--gold); font-weight: 800; }}
        tr.silver td:first-child {{ color: var(--silver); font-weight: 800; }}
        tr.bronze td:first-child {{ color: var(--bronze); font-weight: 800; }}
        .table-wrap {{
            flex: 1;
            overflow: auto;
            border: 1px solid var(--line);
            border-radius: 16px;
        }}
        .tag {{
            display: inline-block;
            margin-left: 6px;
            padding: 2px 8px;
            border-radius: 999px;
            background: #f8ebcf;
            color: #8d5a22;
            font-size: 12px;
            font-weight: 700;
            vertical-align: middle;
        }}
        .notifications {{
            position: fixed;
            top: 20px;
            right: 20px;
            display: grid;
            gap: 10px;
            z-index: 1000;
            width: 320px;
        }}
        .toast {{
            padding: 14px 16px;
            border-radius: 16px;
            color: #fff;
            font-weight: 700;
            box-shadow: 0 10px 26px rgba(60, 37, 8, 0.2);
            animation: rise 0.28s ease;
        }}
        .level-1 {{ background: linear-gradient(135deg, #c8842d, #e0a34d); }}
        .level-2 {{ background: linear-gradient(135deg, #b66427, #d57a31); }}
        .level-3 {{ background: linear-gradient(135deg, #9e4326, #cf6a39); }}
        .level-4, .level-5 {{ background: linear-gradient(135deg, #7e281d, #c2482b); }}
        @keyframes rise {{
            from {{ opacity: 0; transform: translateY(-8px) scale(0.96); }}
            to {{ opacity: 1; transform: translateY(0) scale(1); }}
        }}
        @media (max-width: 980px) {{
            body {{ overflow: auto; }}
            .layout {{ grid-template-columns: 1fr; height: auto; }}
            .side, .main {{ overflow: visible; }}
        }}
    </style>
</head>
<body>
    <div class="notifications" id="notifications"></div>
    <div class="layout">
        <aside class="side">
            <div>
                <div class="title">2048 比赛看板</div>
                <div class="subtitle">模式：{variant}<br>数据源：2048verse</div>
            </div>
            <div class="cards">
                <div class="card"><div class="label">当前时间</div><div class="value small" id="current-time">{current_time}</div></div>
                <div class="card"><div class="label">比赛时间</div><div class="value small">{match_start}<br>{match_end}</div></div>
                <div class="card"><div class="label">比赛总时长</div><div class="value">{duration}</div></div>
                <div class="card"><div class="label">比赛状态</div><div class="value small" id="status-text">{status_text}</div></div>
                <div class="card"><div class="label">剩余时间</div><div class="value" id="remaining-text">{remaining_text}</div></div>
                <div class="card"><div class="label">封榜状态</div><div class="value small" id="seal-text">{seal_status}</div></div>
                <div class="card"><div class="label">参赛人数</div><div class="value">{player_count}</div></div>
                <div class="card"><div class="label">总比赛分</div><div class="value">{total_points}</div></div>
                <div class="card"><div class="label">总计分局数</div><div class="value">{total_games}</div></div>
                <div class="card"><div class="label">总满盘数</div><div class="value">{total_full_boards}</div></div>
            </div>
        </aside>
        <main class="main">
            <h2>玩家排名</h2>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>排名</th>
                            <th>玩家</th>
                            <th>总分</th>
                            <th>计分局数</th>
                            <th>总满盘数</th>
                        </tr>
                    </thead>
                    <tbody>{ranking_rows}</tbody>
                </table>
            </div>
        </main>
    </div>
    <script>
        const startMs = {start_ms};
        const endMs = {end_ms};
        const durationSeconds = {duration_seconds};
        const sealMs = {seal_ms};
        const notificationSeconds = {notification_seconds};
        const initialNotifications = {notifications};
        const toastSeenStorageKey = "fullBoardToastSeen_{event_id}";

        function pad(num) {{
            return String(num).padStart(2, "0");
        }}

        function formatClockFromSeconds(totalSeconds) {{
            const safe = Math.max(0, Math.floor(totalSeconds));
            const hours = Math.floor(safe / 3600);
            const minutes = Math.floor((safe % 3600) / 60);
            const seconds = safe % 60;
            return `${{pad(hours)}}:${{pad(minutes)}}:${{pad(seconds)}}`;
        }}

        function formatDate(ms) {{
            const date = new Date(ms);
            return `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}} ${{pad(date.getHours())}}:${{pad(date.getMinutes())}}:${{pad(date.getSeconds())}}`;
        }}

        function renderDynamicClock() {{
            const now = Date.now();
            document.getElementById("current-time").textContent = formatDate(now);

            let statusText = "已结束";
            let remainingText = "00:00:00";
            if (now < startMs) {{
                statusText = `未开始 ${{formatClockFromSeconds((startMs - now) / 1000)}}`;
                remainingText = "--";
            }} else if (now <= endMs) {{
                statusText = `进行中 ${{formatClockFromSeconds((now - startMs) / 1000)}} / ${{formatClockFromSeconds(durationSeconds)}}`;
                remainingText = formatClockFromSeconds((endMs - now) / 1000);
            }}

            let sealText = "不封榜";
            if (sealMs !== null) {{
                sealText = now < sealMs ? `将在 ${{formatDate(sealMs).slice(11)}} 封榜` : "已封榜";
            }}

            document.getElementById("status-text").textContent = statusText;
            document.getElementById("remaining-text").textContent = remainingText;
            document.getElementById("seal-text").textContent = sealText;
        }}

        function renderNotifications(items) {{
            const holder = document.getElementById("notifications");
            const shownIds = new Set((() => {{
                try {{
                    const raw = localStorage.getItem(toastSeenStorageKey);
                    if (!raw) return [];
                    const parsed = JSON.parse(raw);
                    return Array.isArray(parsed) ? parsed : [];
                }} catch (_) {{
                    return [];
                }}
            }})());

            const freshItems = items.filter((item) => {{
                const itemId = String(item.id || "");
                if (!itemId || shownIds.has(itemId)) {{
                    return false;
                }}
                shownIds.add(itemId);
                return true;
            }});

            try {{
                localStorage.setItem(toastSeenStorageKey, JSON.stringify(Array.from(shownIds)));
            }} catch (_) {{
                // Ignore storage write failures and keep current-page behavior.
            }}

            freshItems.forEach((item, index) => {{
                const node = document.createElement("div");
                node.className = `toast level-${{Math.min(item.level || 1, 5)}}`;
                node.textContent = item.message;
                holder.appendChild(node);
                setTimeout(() => node.remove(), notificationSeconds * 1000 + index * 160);
            }});
        }}

        renderDynamicClock();
        setInterval(renderDynamicClock, 1000);
        renderNotifications(initialNotifications);
    </script>
</body>
</html>
""".format(
        refresh=HTML_REFRESH_SECONDS,
        variant=event_info.variant,
        current_time=format_datetime(now),
        match_start=format_datetime(event_info.start_time),
        match_end=format_datetime(event_info.end_time),
        duration=format_clock(event_info.duration),
        status_text=status_text,
        remaining_text=remaining_text,
        seal_status=seal_status,
        player_count=len(players),
        total_points=total_points,
        total_games=total_games,
        total_full_boards=total_full_boards,
        ranking_rows=build_ranking_rows(display_results),
        start_ms=int(event_info.start_time.timestamp() * 1000),
        end_ms=int(event_info.end_time.timestamp() * 1000),
        duration_seconds=int(event_info.duration.total_seconds()),
        seal_ms=seal_timestamp,
        notification_seconds=NOTIFICATION_SECONDS,
        notifications=notifications_json,
        event_id=event_info.event_id,
    )
    html_path.write_text(html, encoding="utf-8")


def perform_sync(players, event_info, frozen_results, seen_full_board_ids):
    live_results = collect_results(players, event_info.variant, event_info.start_time, event_info.end_time)

    if event_info.seal_time is not None and now_local() >= event_info.seal_time:
        if frozen_results is None:
            frozen_results = freeze_results_at(live_results, event_info.seal_time)
    display_results = frozen_results if frozen_results is not None else live_results

    notifications, seen_full_board_ids = build_full_board_notifications(live_results, seen_full_board_ids)
    return live_results, display_results, frozen_results, notifications, seen_full_board_ids


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


def load_timed_event_from_hub(connection, event_code):
    row = connection.execute(
        """
        SELECT
            e.event_code,
            e.event_name,
            e.start_time,
            e.end_time,
            e.seal_time,
            e.status,
            e.competition_type,
            v.code AS variant_code
        FROM events e
        LEFT JOIN variants v ON v.id = e.variant_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if row is None:
        raise ValueError("赛事不存在: {}".format(event_code))
    if row["competition_type"] != "timed_scoring":
        raise ValueError("该赛事不是限时赛: {}".format(event_code))
    if row["variant_code"] not in {"2x4", "3x3"}:
        raise ValueError("限时赛仅支持 2x4/3x3，当前为 {}".format(row["variant_code"] or "-"))
    if not row["start_time"] or not row["end_time"]:
        raise ValueError("该赛事缺少开始/结束时间")
    start_time = datetime.fromisoformat(str(row["start_time"]).replace(" ", "T"))
    end_time = datetime.fromisoformat(str(row["end_time"]).replace(" ", "T"))
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=LOCAL_TIMEZONE)
    else:
        start_time = start_time.astimezone(LOCAL_TIMEZONE)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=LOCAL_TIMEZONE)
    else:
        end_time = end_time.astimezone(LOCAL_TIMEZONE)
    seal_time = None
    if row["seal_time"]:
        seal_time = datetime.fromisoformat(str(row["seal_time"]).replace(" ", "T"))
        seal_time = seal_time.replace(tzinfo=LOCAL_TIMEZONE) if seal_time.tzinfo is None else seal_time.astimezone(LOCAL_TIMEZONE)
    event_info = build_event_info(row["variant_code"], start_time, end_time - start_time, seal_time)
    event_info.event_id = row["event_code"]
    event_info.event_name = row["event_name"]
    return row, event_info


def load_registered_players_from_hub(connection, event_code):
    rows = connection.execute(
        """
        SELECT
            pa.account_key AS username,
            p.display_name
        FROM registrations r
        JOIN events e ON e.id = r.event_id
        JOIN players p ON p.id = r.player_id
        JOIN player_accounts pa ON pa.player_id = r.player_id AND pa.platform_id = e.platform_id AND pa.is_primary = 1
        WHERE e.event_code = ? AND r.status = 'active'
        ORDER BY LOWER(pa.account_key) ASC
        """,
        (event_code,),
    ).fetchall()
    players = [{"username": row["username"], "display_name": row["display_name"]} for row in rows if row["username"]]
    return players


def create_timed_event_in_hub(connection, *, event_name, variant, start_time, duration, seal_time, is_official, is_rated):
    end_time = start_time + duration
    with transaction(connection):
        result = create_or_update_event(
            connection,
            event_code=None,
            event_name=event_name,
            platform_code="2048verse",
            variant_code=variant,
            event_type="timed_scoring",
            competition_type="timed_scoring",
            rating_bucket_code=infer_rating_bucket_code(variant, "timed_scoring", None),
            status="ready",
            is_official=is_official,
            is_rated=is_rated,
            registration_close_time=format_datetime(start_time),
            start_time=format_datetime(start_time),
            end_time=format_datetime(end_time),
            seal_time=format_datetime(seal_time) if seal_time else None,
            source="organizer_timed_scoring",
            tags=["timed_scoring"],
            target_value=None,
            metadata={"notes": "created by organizer_timed_scoring.py"},
            rule_overrides={},
        )
    return result["event_code"]


def import_players_to_event(connection, event_code, players):
    added = 0
    for player in players:
        username = player.get("username")
        display_name = player.get("display_name") or username
        if not username:
            continue
        register_player(connection, event_code, username, display_name=display_name, registered_via="timed_scoring_file")
        added += 1
    return added


def main():
    print("=== 2048 限时赛主办方执行程序 ===")
    print("1. 运行已有限时赛（按 event_code）")
    print("2. 直接创建并运行限时赛")
    mode_choice = input("请选择 [1]: ").strip() or "1"

    try:
        hub = ensure_hub_ready()
    except Exception as error:
        print("中台初始化失败：{}".format(error))
        return

    try:
        if mode_choice == "1":
            event_code = input("赛事ID（event_code）: ").strip()
            event_row, event_info = load_timed_event_from_hub(hub, event_code)
            players = load_registered_players_from_hub(hub, event_code)
            if not players:
                print("该赛事当前没有报名选手，请先在统一程序里报名。")
                return
            print("已加载赛事: {} | {} | 选手 {} 人".format(event_row["event_code"], event_row["event_name"], len(players)))
        else:
            variant = input("比赛模式（2x4 / 3x3）: ").strip()
            if variant not in {"2x4", "3x3"}:
                print("模式无效，程序退出。")
                return
            event_name = input("比赛名称（默认：{}限时赛）: ".format(variant)).strip() or "{}限时赛".format(variant)
            start_time = parse_start_time(input("比赛开始时间（HH:MM 或 YYYY-MM-DD HH:MM[:SS]）: "))
            duration_text = input("比赛总时长（小时，默认 1）: ").strip() or "1"
            duration = parse_duration_hours(duration_text)
            seal_text = input("封榜时间（比赛开始后多少分钟，回车表示不封榜）: ")
            seal_time = parse_optional_seal_minutes(seal_text, start_time)
            is_official, is_rated = prompt_official_and_rated_defaults()
            event_code = create_timed_event_in_hub(
                hub,
                event_name=event_name,
                variant=variant,
                start_time=start_time,
                duration=duration,
                seal_time=seal_time,
                is_official=is_official,
                is_rated=is_rated,
            )
            print("已创建赛事: {}".format(event_code))
            player_file = input("名单文件路径（.xlsx 或 .csv）: ").strip()
            players = load_players(player_file)
            with transaction(hub):
                added = import_players_to_event(hub, event_code, players)
            event_row, event_info = load_timed_event_from_hub(hub, event_code)
            print("已导入报名 {} 人。".format(added))
    except Exception as error:
        print("初始化失败：{}".format(error))
        return
    finally:
        try:
            hub.close()
        except Exception:
            pass

    root_dir = event_output_dir(BASE_OUTPUT_DIR, event_info)

    existing_state = load_runtime_state()
    if runtime_state_is_protected(existing_state):
        choice = prompt_existing_match_action(existing_state)
        if choice == "1":
            print("已取消本次新开。请继续使用当前正在运行的比赛实例。")
            return
        if choice == "2":
            print("正在请求取消当前比赛...")
            if request_cancel_running_match(existing_state):
                print("已取消当前比赛，可以继续新开。")
            else:
                print("未能在预期时间内取消当前比赛。请先确认旧实例是否还在运行。")
                return
        else:
            print("已选择强制新开。请注意：如果旧实例没有真正停止，可能仍会发生导出冲突。")

    print("\n已载入 {} 名玩家。".format(len(players)))
    print("比赛模式：{}".format(variant))
    print("比赛时间：{} - {}".format(format_datetime(event_info.start_time), format_datetime(event_info.end_time)))
    print("比赛总时长：{}".format(format_clock(event_info.duration)))
    print("封榜时间：{}".format(format_datetime(seal_time) if seal_time else "不封榜"))
    print("HTML 看板：{}".format(HTML_FILE.resolve()))
    print("导出目录：{}".format(root_dir.resolve()))
    print("按 Ctrl+C 停止程序，退出前会自动再同步并导出一次。\n")

    frozen_results = None
    seen_full_board_ids = set()
    final_export_done = False
    write_runtime_state(event_info, root_dir)

    try:
        while True:
            try:
                if cancel_requested_for_current_process():
                    append_runtime_log("Cancellation requested by another launcher.")
                    print("检测到新的启动器请求取消当前比赛，当前实例即将停止。")
                    break

                now = now_local()
                print("[{}] 正在同步比赛数据...".format(format_datetime(now)))
                live_results, display_results, frozen_results, notifications, seen_full_board_ids = perform_sync(
                    players, event_info, frozen_results, seen_full_board_ids
                )
                write_html(event_info, players, live_results, display_results, notifications, HTML_FILE)
                export_all(live_results, event_info, root_dir)
                write_runtime_state(event_info, root_dir)
                print("已更新 HTML 看板、总排名和玩家明细。")

                if now >= event_info.end_time and not final_export_done:
                    final_export_done = True
                    print("比赛已结束，最终结果已导出。")
                sleep(CHECK_INTERVAL)
            except KeyboardInterrupt:
                print("")
                if not confirm_exit():
                    print("Exit cancelled. Continuing...")
                    continue
                print("\nExit requested. Running one final sync and export...")
                live_results, display_results, frozen_results, notifications, seen_full_board_ids = perform_sync(
                    players, event_info, frozen_results, seen_full_board_ids
                )
                write_html(event_info, players, live_results, display_results, notifications, HTML_FILE)
                export_all(live_results, event_info, root_dir)
                print("Final export complete. Program exited.")
                break
            except Exception as error:
                append_runtime_log(traceback.format_exc())
                print("Sync failed: {}. Logged and retrying in {} seconds.".format(error, CHECK_INTERVAL))
                sleep(CHECK_INTERVAL)
    finally:
        unregister_close_minimize_handler()
        cleanup_runtime_state()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        append_runtime_log(traceback.format_exc())
        print("Fatal error: {}. Details logged to {}".format(error, RUNTIME_LOG_FILE.resolve()))
        input("Press Enter to exit...")
