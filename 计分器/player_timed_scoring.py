from pathlib import Path
from time import sleep
import traceback

from tournament_common import (
    CHECK_INTERVAL,
    HTML_REFRESH_SECONDS,
    build_event_info,
    event_output_dir,
    export_player_text,
    fetch_games_for_window,
    format_clock,
    format_datetime,
    format_offset,
    get_display_name,
    get_remaining_time_text,
    get_status_text,
    now_local,
    parse_duration_hours,
    parse_start_time,
    score_games,
)


BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "选手比赛看板.html"
BASE_OUTPUT_DIR = BASE_DIR.parent / "比赛导出" / "选手自用"

RUNTIME_LOG_FILE = BASE_DIR / "player_timed_scoring_runtime.log"


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


def write_html(event_info, result, html_path):
    now = now_local()
    status_text = get_status_text(now, event_info.start_time, event_info.end_time, event_info.duration)
    remaining_text = get_remaining_time_text(now, event_info.start_time, event_info.end_time)

    rows = []
    if result.games:
        for game in result.games:
            rows.append(
                "<tr>"
                "<td>{}</td>"
                "<td>{}</td>"
                "<td>{}</td>"
                "</tr>".format(
                    format_offset(game.offset),
                    game.label,
                    game.points,
                )
            )
        history_rows = "".join(rows)
    else:
        history_rows = "<tr><td colspan='3'>暂无计分局</td></tr>"

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="{refresh}">
    <title>2048 选手比赛看板</title>
    <style>
        :root {{
            --bg: #f7f1e7;
            --panel: #fff9f2;
            --line: #ddceb5;
            --text: #2e2317;
            --muted: #756550;
            --accent: #aa5e2d;
            --soft: #efddc1;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            padding: 14px;
            font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
            background:
                radial-gradient(circle at top, #fde7c8, transparent 32%),
                linear-gradient(180deg, #f8f2ea, #f0e2ca);
            color: var(--text);
        }}
        .shell {{
            max-width: 430px;
            margin: 0 auto;
        }}
        .panel {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 22px;
            padding: 18px;
            box-shadow: 0 12px 28px rgba(69, 48, 18, 0.08);
        }}
        .headline {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 12px;
            margin-bottom: 14px;
        }}
        h1 {{
            margin: 0;
            font-size: 24px;
        }}
        .sub {{
            color: var(--muted);
            font-size: 13px;
            line-height: 1.5;
        }}
        .score-band {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 12px;
        }}
        .hero {{
            background: linear-gradient(135deg, #fff8ee, #f8e5c5);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 14px;
        }}
        .hero .label {{
            color: var(--muted);
            font-size: 12px;
            margin-bottom: 4px;
        }}
        .hero .value {{
            font-size: 30px;
            font-weight: 800;
        }}
        .cards {{
            display: grid;
            gap: 10px;
        }}
        .card {{
            background: #fff;
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 12px 14px;
        }}
        .label {{
            color: var(--muted);
            font-size: 12px;
            margin-bottom: 4px;
        }}
        .value {{
            font-size: 21px;
            font-weight: 700;
        }}
        .value.small {{
            font-size: 14px;
            line-height: 1.45;
            font-weight: 600;
        }}
        .table {{
            margin-top: 14px;
            border: 1px solid var(--line);
            border-radius: 18px;
            overflow: hidden;
            background: #fff;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            padding: 10px 12px;
            border-bottom: 1px solid #efe2cf;
            text-align: left;
            font-size: 13px;
        }}
        th {{
            background: var(--soft);
        }}
        tr:last-child td {{
            border-bottom: none;
        }}
        .foot {{
            margin-top: 10px;
            color: var(--muted);
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="shell">
        <section class="panel">
            <div class="headline">
                <div>
                    <h1>{display_name}</h1>
                    <div class="sub">模式：{variant}<br>比赛时间：{match_start} - {match_end}</div>
                </div>
                <div class="sub">文件夹：{event_id}</div>
            </div>

            <div class="score-band">
                <div class="hero">
                    <div class="label">总比赛分</div>
                    <div class="value">{total_points}</div>
                </div>
                <div class="hero">
                    <div class="label">总满盘数</div>
                    <div class="value">{total_full_boards}</div>
                </div>
            </div>

            <div class="cards">
                <div class="card"><div class="label">当前时间</div><div class="value small" id="current-time">{current_time}</div></div>
                <div class="card"><div class="label">比赛总时长</div><div class="value">{duration}</div></div>
                <div class="card"><div class="label">比赛状态</div><div class="value small" id="status-text">{status_text}</div></div>
                <div class="card"><div class="label">剩余时间</div><div class="value" id="remaining-text">{remaining_text}</div></div>
                <div class="card"><div class="label">计分局数</div><div class="value">{scoring_game_count}</div></div>
            </div>

            <div class="table">
                <table>
                    <thead>
                        <tr>
                            <th>时间</th>
                            <th>盘面</th>
                            <th>分数</th>
                        </tr>
                    </thead>
                    <tbody>{history_rows}</tbody>
                </table>
            </div>
            <div class="foot">导出文件：{export_name}</div>
        </section>
    </div>
    <script>
        const startMs = {start_ms};
        const endMs = {end_ms};
        const durationSeconds = {duration_seconds};

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

        function renderClock() {{
            const now = Date.now();
            document.getElementById("current-time").textContent = formatDate(now);
            if (now < startMs) {{
                document.getElementById("status-text").textContent = `未开始 ${{formatClockFromSeconds((startMs - now) / 1000)}}`;
                document.getElementById("remaining-text").textContent = "--";
            }} else if (now <= endMs) {{
                document.getElementById("status-text").textContent = `进行中 ${{formatClockFromSeconds((now - startMs) / 1000)}} / ${{formatClockFromSeconds(durationSeconds)}}`;
                document.getElementById("remaining-text").textContent = formatClockFromSeconds((endMs - now) / 1000);
            }} else {{
                document.getElementById("status-text").textContent = "已结束";
                document.getElementById("remaining-text").textContent = "00:00:00";
            }}
        }}

        renderClock();
        setInterval(renderClock, 1000);
    </script>
</body>
</html>
""".format(
        refresh=HTML_REFRESH_SECONDS,
        display_name=result.display_name,
        variant=result.variant,
        match_start=format_datetime(event_info.start_time),
        match_end=format_datetime(event_info.end_time),
        event_id=event_info.event_id,
        total_points=result.total_points,
        total_full_boards=result.total_full_boards,
        current_time=format_datetime(now),
        duration=format_clock(event_info.duration),
        status_text=status_text,
        remaining_text=remaining_text,
        scoring_game_count=result.scoring_game_count,
        history_rows=history_rows,
        export_name="{}.txt".format(result.username),
        start_ms=int(event_info.start_time.timestamp() * 1000),
        end_ms=int(event_info.end_time.timestamp() * 1000),
        duration_seconds=int(event_info.duration.total_seconds()),
    )
    html_path.write_text(html, encoding="utf-8")


def main():
    print("=== 2048 选手自查程序 ===")
    username = input("2048verse 用户名: ").strip()
    if not username:
        print("用户名不能为空。")
        return

    variant = input("比赛模式（2x4 / 3x3）: ").strip()
    if variant not in {"2x4", "3x3"}:
        print("模式无效，程序退出。")
        return

    try:
        start_time = parse_start_time(input("比赛开始时间（HH:MM 或 YYYY-MM-DD HH:MM[:SS]）: "))
        duration_text = input("比赛总时长（小时，默认 1）: ").strip() or "1"
        duration = parse_duration_hours(duration_text)
    except Exception as error:
        print("时间输入无效：{}".format(error))
        return

    event_info = build_event_info(variant, start_time, duration, None)
    root_dir = event_output_dir(BASE_OUTPUT_DIR, event_info)

    print("\n比赛时间：{} - {}".format(format_datetime(event_info.start_time), format_datetime(event_info.end_time)))
    print("HTML 看板：{}".format(HTML_FILE.resolve()))
    print("导出目录：{}".format(root_dir.resolve()))
    print("比赛开始后再启动程序也可以补抓当前时间窗内的数据。")
    print("按 Ctrl+C 停止程序。\n")

    display_name = get_display_name(username)

    while True:
        try:
            now = now_local()
            print("[{}] 正在刷新个人成绩...".format(format_datetime(now)))
            games = fetch_games_for_window(username, variant, event_info.start_time, event_info.end_time)
            result = score_games(username, display_name, variant, games, event_info.start_time, event_info.end_time)
            write_html(event_info, result, HTML_FILE)
            export_player_text(result, event_info, root_dir)
            print("已更新个人 HTML 和导出文件。")
            sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("")
            if not confirm_exit():
                print("Exit cancelled. Continuing...")
                continue
            print("\nExit requested. Running one final sync and export...")
            games = fetch_games_for_window(username, variant, event_info.start_time, event_info.end_time)
            result = score_games(username, display_name, variant, games, event_info.start_time, event_info.end_time)
            write_html(event_info, result, HTML_FILE)
            export_player_text(result, event_info, root_dir)
            print("Final export complete. Program exited.")
            break
        except Exception as error:
            append_runtime_log(traceback.format_exc())
            print("Sync failed: {}. Logged and retrying in {} seconds.".format(error, CHECK_INTERVAL))
            sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        append_runtime_log(traceback.format_exc())
        print("Fatal error: {}. Details logged to {}".format(error, RUNTIME_LOG_FILE.resolve()))
        input("Press Enter to exit...")
