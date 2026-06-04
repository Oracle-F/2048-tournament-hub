import csv
import json
from pathlib import Path


def parse_int(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(float(text))


def parse_json_text(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text}


def normalize_row(row, index, default_source_prefix):
    username = (row.get("username") or row.get("account_name") or "").strip()
    if not username:
        raise ValueError("Missing username in row {}".format(index))

    display_name = (row.get("display_name") or username).strip()
    raw_score = parse_int(row.get("raw_score"))
    final_score = parse_int(row.get("final_score"))
    generic_score = parse_int(row.get("score"))
    competition_score = parse_int(row.get("competition_score"))

    if raw_score is None and generic_score is not None:
        raw_score = generic_score
    if final_score is None:
        final_score = raw_score if raw_score is not None else generic_score
    if competition_score is None:
        competition_score = final_score if final_score is not None else raw_score

    source_record_id = (row.get("source_record_id") or "").strip()
    if not source_record_id:
        source_record_id = "{}_{}".format(default_source_prefix, index)

    return {
        "username": username,
        "display_name": display_name,
        "source_record_id": source_record_id,
        "record_type": (row.get("record_type") or "manual_import").strip(),
        "started_at": (row.get("started_at") or "").strip() or None,
        "ended_at": (row.get("ended_at") or "").strip() or None,
        "raw_score": raw_score,
        "final_score": final_score,
        "competition_score": competition_score,
        "primary_time_ms": parse_int(row.get("primary_time_ms")),
        "target_tile_value": parse_int(row.get("target_tile_value")),
        "score_before_target": parse_int(row.get("score_before_target")),
        "result_state": (row.get("result_state") or "").strip() or None,
        "evidence": parse_json_text(row.get("evidence_json")),
        "raw_payload": parse_json_text(row.get("raw_payload_json")) or row,
    }


def parse_csv_file(file_path: Path, default_source_prefix: str):
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for index, row in enumerate(reader, start=1):
            rows.append(normalize_row(row, index, default_source_prefix))
        return rows


def parse_json_file(file_path: Path, default_source_prefix: str):
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        items = payload.get("records") or payload.get("scores") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError("Unsupported JSON structure in {}".format(file_path))

    rows = []
    for index, row in enumerate(items, start=1):
        if not isinstance(row, dict):
            raise ValueError("JSON row {} is not an object".format(index))
        rows.append(normalize_row(row, index, default_source_prefix))
    return rows


def parse_score_file(file_path: Path, default_source_prefix: str):
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        return parse_csv_file(file_path, default_source_prefix)
    if suffix == ".json":
        return parse_json_file(file_path, default_source_prefix)
    raise ValueError("Unsupported score file type: {}".format(file_path))

