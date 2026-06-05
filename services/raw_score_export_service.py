import json
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from settings import LOCAL_TIMEZONE, ORGANIZER_EXPORTS_DIR


def now_local():
    return datetime.now(LOCAL_TIMEZONE)


def parse_time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def format_time(value):
    if not value:
        return "-"
    parsed = parse_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def safe_name(value):
    invalid = '\\/:*?"<>|'
    output = []
    for char in value:
        output.append("_" if char in invalid else char)
    return "".join(output)


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


def format_metric(value):
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return "{:.2f}".format(value)
    return str(value)


def fit_text(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    output = text
    while output and draw.textlength(output + ellipsis, font=font) > max_width:
        output = output[:-1]
    return (output + ellipsis) if output else ellipsis


def row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def load_event_export_bundle(connection, event_code):
    event = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            e.event_type,
            e.competition_type,
            e.start_time,
            e.end_time,
            e.status,
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
        raise ValueError("Event not found: {}".format(event_code))

    rule_set = connection.execute(
        """
        SELECT aggregation_method, ranking_metric, ranking_order, rule_config_json
        FROM event_rule_sets
        WHERE event_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (event["id"],),
    ).fetchone()
    if rule_set is None:
        raise ValueError("Rule set not found for event: {}".format(event_code))

    rows = connection.execute(
        """
        SELECT
            er.rank_value,
            er.primary_metric_type,
            er.primary_metric_value,
            er.secondary_metric_type,
            er.secondary_metric_value,
            er.best_single_score,
            er.result_payload_json,
            p.display_name,
            pa.account_key AS username
        FROM event_results er
        JOIN players p ON p.id = er.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = er.player_id
           AND pa.is_primary = 1
        WHERE er.event_id = ?
        ORDER BY er.rank_value ASC, LOWER(COALESCE(pa.account_key, p.display_name)) ASC
        """,
        (event["id"],),
    ).fetchall()
    return event, rule_set, rows


def build_ranking_payload(event, rule_set, rows):
    players_with_scores = sum(1 for row in rows if row["primary_metric_value"] is not None)
    total_scoring_games = sum(int(row["secondary_metric_value"] or 0) for row in rows)
    rule_config = json.loads(rule_set["rule_config_json"] or "{}")
    payload = {
        "snapshot_type": "final",
        "event": {
            "event_code": event["event_code"],
            "event_name": event["event_name"],
            "platform": event["platform_code"],
            "variant": event["variant_code"],
            "event_type": event["event_type"],
            "competition_type": event["competition_type"],
            "start_time": format_time(event["start_time"]),
            "end_time": format_time(event["end_time"]),
            "status": event["status"],
            "aggregation_method": rule_set["aggregation_method"],
            "ranking_metric": rule_set["ranking_metric"],
        },
        "rules": {
            "aggregation_method": rule_set["aggregation_method"],
            "aggregation_count": rule_config.get("aggregation_count"),
            "top_n": rule_config.get("top_n"),
            "weight_base": rule_config.get("weight_base"),
            "missing_attempt_policy": rule_config.get("missing_attempt_policy"),
            "ranking_metric": rule_set["ranking_metric"],
            "ranking_order": rule_set["ranking_order"],
            "window_rule": "只统计比赛开始后开始、并在比赛结束前结束的局",
        },
        "export": {
            "exported_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "time_zone": "Asia/Shanghai",
        },
        "summary": {
            "ranked_player_count": len(rows),
            "players_with_scores": players_with_scores,
            "total_scoring_games": total_scoring_games,
        },
        "ranking": [],
    }
    for row in rows:
        result_payload = json.loads(row["result_payload_json"] or "{}")
        payload["ranking"].append(
            {
                "rank": row["rank_value"],
                "username": row["username"],
                "display_name": row["display_name"],
                "primary_metric_type": row["primary_metric_type"],
                "primary_metric_value": row["primary_metric_value"],
                "secondary_metric_type": row["secondary_metric_type"],
                "secondary_metric_value": row["secondary_metric_value"],
                "best_single_score": row["best_single_score"],
                "scoring_game_count": int(row["secondary_metric_value"] or 0),
                "result_payload": result_payload,
            }
        )
    return payload


def write_ranking_text(event, rows, output_dir):
    is_timed = event["competition_type"] == "timed_scoring"
    lines = [
        "比赛名称: {}".format(event["event_name"]),
        "赛事ID: {}".format(event["event_code"]),
        "平台: {}".format(event["platform_code"]),
        "玩法: {}".format(event["variant_code"] or "-"),
        "赛制: {}".format(event["competition_type"]),
        "比赛时间: {} - {}".format(format_time(event["start_time"]), format_time(event["end_time"])),
        "",
        "排名 | 玩家 | 比赛分 | 计分局数 | 总满盘数" if is_timed else "排名 | 玩家 | 成绩 | 已计局数 | 最佳单局",
    ]
    for row in rows:
        payload = json.loads(row["result_payload_json"] or "{}")
        full_boards = payload.get("total_full_boards")
        lines.append(
            "{} | {} | {} | {} | {}".format(
                row["rank_value"],
                row["display_name"],
                row["primary_metric_value"] if row["primary_metric_value"] is not None else "-",
                int(row["secondary_metric_value"] or 0),
                (full_boards if full_boards is not None else "-")
                if is_timed
                else (row["best_single_score"] if row["best_single_score"] is not None else "-"),
            )
        )
    path = output_dir / "总排名.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_ranking_json(payload, output_dir):
    path = output_dir / "总排名.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_ranking_image(event, rows, output_dir):
    is_timed = event["competition_type"] == "timed_scoring"
    width = 1220
    header_height = 192
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
    draw.text((48, 36), "{} 总排名".format(event["event_name"]), fill="#2b2115", font=title_font)
    subtitle = "{} | {} - {}".format(
        event["competition_type"],
        format_time(event["start_time"]),
        format_time(event["end_time"]),
    )
    draw.text((48, 84), subtitle, fill="#6b5d49", font=body_font)
    summary = "赛事ID: {} | 平台: {} | 玩法: {} | 选手: {} 人".format(
        event["event_code"],
        event["platform_code"],
        event["variant_code"] or "-",
        len(rows),
    )
    draw.text((48, 120), summary, fill="#6b5d49", font=small_font)
    draw.text(
        (48, 146),
        "导出时间: {}".format(now_local().strftime("%Y-%m-%d %H:%M:%S")),
        fill="#6b5d49",
        font=small_font,
    )

    table_top = header_height
    draw.rounded_rectangle((40, table_top, width - 40, table_top + row_height), radius=12, fill="#ead9bf")
    columns = [
        ("排名", 60),
        ("玩家", 140),
        ("比赛分", 620) if is_timed else ("成绩", 620),
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
        if row_get(row, "username") and row["username"] != row["display_name"]:
            player_text = "{} ({})".format(row["display_name"], row["username"])
        player_text = fit_text(draw, player_text, body_font, 440)
        draw.text((140, top + 10), player_text, fill="#2f261b", font=body_font)
        draw.text((620, top + 10), format_metric(row["primary_metric_value"]), fill="#2f261b", font=body_font)
        draw.text((820, top + 10), str(int(row["secondary_metric_value"] or 0)), fill="#2f261b", font=body_font)
        payload = json.loads(row["result_payload_json"] or "{}")
        third_metric = payload.get("total_full_boards") if is_timed else row["best_single_score"]
        draw.text((1000, top + 10), format_metric(third_metric), fill="#2f261b", font=body_font)

    footer_text = "正式结果导出，详情见同目录下的总排名文本、JSON 和玩家明细"
    draw.text((48, height - 46), footer_text, fill="#746754", font=small_font)
    path = output_dir / "总排名.png"
    image.save(path)
    return path


def write_player_details(event, rows, output_dir):
    details_dir = output_dir / "玩家明细"
    details_dir.mkdir(parents=True, exist_ok=True)
    detail_paths = []
    for row in rows:
        payload = json.loads(row["result_payload_json"] or "{}")
        source_records = payload.get("source_records", [])
        lines = [
            "比赛名称: {}".format(event["event_name"]),
            "赛事ID: {}".format(event["event_code"]),
            "平台: {}".format(event["platform_code"]),
            "玩法: {}".format(event["variant_code"] or "-"),
            "赛制: {}".format(event["competition_type"]),
            "比赛时间: {} - {}".format(format_time(event["start_time"]), format_time(event["end_time"])),
            "",
            "玩家: {}".format(row["display_name"]),
            "用户名: {}".format(row["username"] or "-"),
            "排名: {}".format(row["rank_value"]),
            "最终成绩: {}".format(row["primary_metric_value"] if row["primary_metric_value"] is not None else "-"),
            "已计局数: {}".format(int(row["secondary_metric_value"] or 0)),
            "最佳单局: {}".format(row["best_single_score"] if row["best_single_score"] is not None else "-"),
            "",
            "计入记录",
        ]
        weighted_terms = payload.get("weighted_terms") or []
        if weighted_terms:
            lines.append("")
            lines.append("加权贡献")
            for term in weighted_terms:
                lines.append(
                    "- 第{}高局: 分数 {} × 权重 {:.4f} = {:.4f}".format(
                        term.get("index"),
                        term.get("metric"),
                        term.get("weight"),
                        term.get("contribution"),
                    )
                )
            lines.append("加权总分: {}".format(row["primary_metric_value"] if row["primary_metric_value"] is not None else "-"))
        if not source_records:
            lines.append("无")
        else:
            for index, record in enumerate(source_records, start=1):
                time_text = format_time(record.get("ended_at"))
                if record.get("started_at"):
                    time_text = "{} -> {}".format(
                        format_time(record.get("started_at")),
                        format_time(record.get("ended_at")),
                    )
                lines.append(
                    "{}. 时间 {} | 分数 {} | record_id {}".format(
                        index,
                        time_text,
                        record.get("competition_score")
                        if record.get("competition_score") is not None
                        else record.get("final_score")
                        if record.get("final_score") is not None
                        else record.get("raw_score"),
                        record.get("performance_record_id"),
                    )
                )
        path = details_dir / "{}.txt".format(safe_name(row["username"] or row["display_name"]))
        path.write_text("\n".join(lines), encoding="utf-8")
        detail_paths.append(str(path))
    return detail_paths


def export_final_ranking(connection, event_code):
    event, rule_set, rows = load_event_export_bundle(connection, event_code)
    output_dir = ORGANIZER_EXPORTS_DIR / safe_name(event["event_code"])
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_ranking_payload(event, rule_set, rows)
    txt_path = write_ranking_text(event, rows, output_dir)
    json_path = write_ranking_json(payload, output_dir)
    png_path = write_ranking_image(event, rows, output_dir)
    detail_paths = write_player_details(event, rows, output_dir)
    return {
        "event_code": event["event_code"],
        "output_dir": str(output_dir),
        "txt_path": str(txt_path),
        "json_path": str(json_path),
        "png_path": str(png_path),
        "detail_count": len(detail_paths),
        "row_count": len(rows),
    }
