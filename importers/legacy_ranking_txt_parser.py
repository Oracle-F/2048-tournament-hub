import json
import re
from datetime import datetime
from pathlib import Path


HEADER_ALIASES = {
    "排名": "rank",
    "玩家": "username",
    "总分": "total_points",
    "计分局数": "scoring_game_count",
    "总满盘数": "total_full_boards",
}


def split_columns(line: str):
    return [part.strip() for part in line.split("|")]


def normalize_header(header: str):
    return HEADER_ALIASES.get(header, header)


def build_event_id(variant: str, start_time: str):
    parsed = parse_datetime(start_time)
    return "{}_{}_{}".format(parsed.strftime("%Y%m%d"), variant, parsed.strftime("%H%M"))


def parse_datetime(value: str):
    for time_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, time_format)
        except ValueError:
            continue
    raise ValueError("Unsupported datetime format: {}".format(value))


def parse_match_time(line: str):
    prefix = "比赛时间:"
    if not line.startswith(prefix):
        raise ValueError("Invalid 比赛时间 line: {}".format(line))
    value = line[len(prefix):].strip()
    parts = value.split(" - ")
    if len(parts) != 2:
        raise ValueError("Invalid 比赛时间 line: {}".format(line))
    return parts[0].strip(), parts[1].strip()


def parse_metadata(lines):
    metadata = {
        "mode": None,
        "start_time": None,
        "end_time": None,
        "duration": None,
        "signature": None,
    }
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("模式:"):
            metadata["mode"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("比赛时间:"):
            start_time, end_time = parse_match_time(stripped)
            metadata["start_time"] = start_time
            metadata["end_time"] = end_time
        elif stripped.startswith("比赛总时长:"):
            metadata["duration"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("校验码:"):
            metadata["signature"] = stripped.split(":", 1)[1].strip()
    return metadata


def parse_ranking_table(lines):
    table_lines = [line.strip() for line in lines if "|" in line.strip()]
    if not table_lines:
        raise ValueError("No ranking table found")

    header_line = table_lines[0]
    headers = [normalize_header(value) for value in split_columns(header_line)]
    rows = []
    for line in table_lines[1:]:
        columns = split_columns(line)
        if len(columns) != len(headers):
            continue
        row = dict(zip(headers, columns))
        row["rank"] = int(row["rank"])
        if "total_points" in row:
            row["total_points"] = int(row["total_points"])
        if "scoring_game_count" in row:
            row["scoring_game_count"] = int(row["scoring_game_count"])
        if "total_full_boards" in row:
            row["total_full_boards"] = int(row["total_full_boards"])
        rows.append(row)
    return rows


def convert_legacy_ranking_txt(file_path: Path):
    text = file_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    metadata = parse_metadata(lines)
    ranking_rows = parse_ranking_table(lines)

    variant = metadata["mode"]
    if not variant:
        raise ValueError("Missing 模式 in {}".format(file_path))
    if not metadata["start_time"] or not metadata["end_time"]:
        raise ValueError("Missing 比赛时间 in {}".format(file_path))

    event_id = build_event_id(variant, metadata["start_time"])
    ranking = []
    for row in ranking_rows:
        ranking.append(
            {
                "rank": row["rank"],
                "username": row["username"],
                "display_name": row["username"],
                "primary_label": "总分",
                "primary_value": row.get("total_points"),
                "secondary_label": "总满盘数",
                "secondary_value": row.get("total_full_boards"),
                "extra": {
                    "计分局数": row.get("scoring_game_count"),
                },
            }
        )

    payload = {
        "event": {
            "event_id": event_id,
            "event_name": "2048 {} 比赛".format(variant),
            "source": "2048verse",
            "variant": variant,
            "format": "timed_scoring",
            "start_time": metadata["start_time"],
            "end_time": metadata["end_time"],
            "duration": metadata["duration"],
            "seal_time": None,
        },
        "ranking": ranking,
        "signature": metadata["signature"],
        "conversion": {
            "source_type": "legacy_ranking_txt",
            "source_path": str(file_path),
            "notes": [
                "Converted from legacy ranking txt",
                "Display name is inferred from username",
                "Missing total full boards remain null when not present in txt",
            ],
        },
    }
    return payload


def dumps_payload(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2)
