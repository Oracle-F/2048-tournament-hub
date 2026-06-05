from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from services.competition_mode_service import event_uses_locking
from services.discord_lock_refresh_service import refresh_locking_event_scores_from_discord
from services.event_progress_service import build_event_progress_snapshot
from services.settlement_service import build_ranked_results
from settings import INTERIM_EXPORTS_DIR, LOCAL_TIMEZONE


def now_local():
    return datetime.now(LOCAL_TIMEZONE)


def format_time(value):
    if not value:
        return "-"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def timestamp_name(value):
    return value.astimezone(LOCAL_TIMEZONE).strftime("%Y%m%d_%H%M%S")


def format_metric(value):
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return "{:.2f}".format(value)
    return str(value)


def load_font(size, bold=False):
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


def fit_text(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    output = text
    while output and draw.textlength(output + ellipsis, font=font) > max_width:
        output = output[:-1]
    return (output + ellipsis) if output else ellipsis


def preview_ranking(connection, event_code):
    bundle = build_ranked_results(connection, event_code, include_registered_without_records=True)
    event = bundle["event"]
    if event_uses_locking(event["competition_type"], event["platform_code"], event["variant_code"]):
        try:
            refresh_locking_event_scores_from_discord(connection, event_code)
            bundle = build_ranked_results(connection, event_code, include_registered_without_records=True)
        except Exception:
            pass
    event = bundle["event"]
    rows = bundle["ranked_results"]
    return {
        "event": event,
        "rows": rows,
        "record_count": bundle["record_count"],
        "aggregation_method": bundle["aggregation_method"],
    }


def export_interim_ranking_image(connection, event_code):
    preview = preview_ranking(connection, event_code)
    progress = build_event_progress_snapshot(connection, event_code)
    event = preview["event"]
    rows = preview["rows"]
    is_timed = event["competition_type"] == "timed_scoring"

    export_dir = INTERIM_EXPORTS_DIR / event["event_code"]
    export_dir.mkdir(parents=True, exist_ok=True)
    created_at = now_local()
    image_path = export_dir / "{}_当前排名.png".format(timestamp_name(created_at))

    width = 1220
    header_height = 196
    row_height = 46
    footer_height = 64
    body_rows = max(1, len(rows))
    height = header_height + row_height * (body_rows + 1) + footer_height

    image = Image.new("RGB", (width, height), "#f6efe3")
    draw = ImageDraw.Draw(image)
    title_font = load_font(34, bold=True)
    body_font = load_font(22)
    strong_font = load_font(22, bold=True)
    small_font = load_font(16)

    draw.rounded_rectangle((24, 20, width - 24, height - 20), radius=24, fill="#fffaf1", outline="#d8c8ad", width=2)
    draw.text((48, 36), "{} 当前排名".format(event["event_name"]), fill="#2b2115", font=title_font)
    subtitle = "{} | {} - {}".format(
        event["competition_type"],
        format_time(event["start_time"]),
        format_time(event["end_time"]),
    )
    draw.text((48, 84), subtitle, fill="#6b5d49", font=body_font)
    summary = "赛事ID: {} | 统计记录: {} | 导出时间: {}".format(
        event["event_code"],
        preview["record_count"],
        created_at.strftime("%Y-%m-%d %H:%M:%S"),
    )
    draw.text((48, 120), summary, fill="#6b5d49", font=small_font)
    draw.text(
        (48, 146),
        "报名 {} | 待开始 {} | 进行中 {} | 已完赛 {}".format(
            len(progress["participants"]),
            progress["counts"].get("待开始", 0),
            progress["counts"].get("进行中", 0),
            progress["counts"].get("已完赛", 0),
        ),
        fill="#6b5d49",
        font=small_font,
    )

    table_top = header_height
    draw.rounded_rectangle((40, table_top, width - 40, table_top + row_height), radius=12, fill="#ead9bf")
    columns = [
        ("排名", 60),
        ("玩家", 140),
        ("当前比赛分", 620) if is_timed else ("当前成绩", 620),
        ("计分局数", 800) if is_timed else ("已计局数", 800),
        ("总满盘数", 980) if is_timed else ("最佳单局", 980),
    ]
    for label, x in columns:
        draw.text((x, table_top + 10), label, fill="#3a2c1c", font=strong_font)

    medal_colors = {1: "#c58a13", 2: "#8d96a0", 3: "#b36d3f"}
    for index, row in enumerate(rows, start=1):
        top = table_top + row_height * index
        fill = "#fffdf9" if index % 2 else "#f7efe3"
        if index == 1:
            fill = "#f7e6a8"
        elif index == 2:
            fill = "#e3e8ef"
        elif index == 3:
            fill = "#edd0b8"
        draw.rectangle((40, top, width - 40, top + row_height), fill=fill)
        draw.text((60, top + 10), str(row["rank_value"]), fill=medal_colors.get(index, "#3a2c1c"), font=strong_font)
        player_text = row["display_name"]
        if row.get("account_name") and row["account_name"] != row["display_name"]:
            player_text = "{} ({})".format(row["display_name"], row["account_name"])
        player_text = fit_text(draw, player_text, body_font, 440)
        draw.text((140, top + 10), player_text, fill="#2f261b", font=body_font)
        draw.text((620, top + 10), format_metric(row["primary_metric_value"]), fill="#2f261b", font=body_font)
        draw.text((830, top + 10), format_metric(row.get("secondary_metric_value")), fill="#2f261b", font=body_font)
        payload = row.get("result_payload") or {}
        third_metric = payload.get("total_full_boards") if is_timed else row.get("best_single_score")
        draw.text((1000, top + 10), format_metric(third_metric), fill="#2f261b", font=body_font)

    footer_text = "中途快照图片保存在“比赛导出/中途排名/赛事ID”目录中"
    draw.text((48, height - 46), footer_text, fill="#746754", font=small_font)
    image.save(image_path)

    return {
        "event_code": event["event_code"],
        "event_name": event["event_name"],
        "row_count": len(rows),
        "image_path": str(image_path),
    }
