import json
from pathlib import Path

from settings import (
    DEFAULT_IMPORT_IS_OFFICIAL,
    DEFAULT_IMPORT_IS_RATED,
    DEFAULT_IMPORT_SOURCE,
    DEFAULT_IMPORT_STATUS,
    DEFAULT_IMPORT_TAGS,
)


def infer_rating_bucket(variant: str):
    mapping = {
        "2x4": "timed_2x4",
        "3x3": "timed_3x3",
    }
    return mapping.get(variant)


def parse_organizer_ranking_json(file_path: Path):
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    event = payload.get("event") or {}
    ranking = payload.get("ranking") or []
    signature = payload.get("signature")

    event_id = event.get("event_id")
    if not event_id:
        raise ValueError("Missing event.event_id in {}".format(file_path))

    variant = event.get("variant")
    rating_bucket_code = infer_rating_bucket(variant)

    results = []
    for item in ranking:
        results.append(
            {
                "rank": item.get("rank"),
                "username": item.get("username"),
                "display_name": item.get("display_name") or item.get("username"),
                "primary_metric_type": "custom_points",
                "primary_metric_value": item.get("primary_value"),
                "secondary_metric_type": "full_board_total",
                "secondary_metric_value": item.get("secondary_value"),
                "extra": item.get("extra") or {},
                "raw_payload": item,
            }
        )

    return {
        "event": {
            "event_code": event_id,
            "event_name": event.get("event_name") or event_id,
            "platform_code": event.get("source") or "2048verse",
            "variant_code": variant,
            "rating_bucket_code": rating_bucket_code,
            "event_type": "timed_scoring",
            "competition_type": "timed_scoring",
            "status": DEFAULT_IMPORT_STATUS,
            "is_official": DEFAULT_IMPORT_IS_OFFICIAL,
            "is_rated": DEFAULT_IMPORT_IS_RATED,
            "start_time": event.get("start_time"),
            "end_time": event.get("end_time"),
            "seal_time": event.get("seal_time"),
            "source": DEFAULT_IMPORT_SOURCE,
            "tags": list(DEFAULT_IMPORT_TAGS),
            "metadata": {
                "legacy_event": event,
                "import_file": str(file_path),
            },
        },
        "rule_set": {
            "version": 1,
            "rule_type": event.get("format") or event.get("format_name") or "timed_scoring",
            "ranking_metric": "custom_points",
            "ranking_order": "desc",
            "aggregation_method": "sum",
            "validation_rule": "window_closed_games",
            "tiebreakers": ["full_board_total"],
            "rule_config": {
                "duration": event.get("duration"),
                "seal_time": event.get("seal_time"),
                "source_payload": event,
            },
        },
        "results": results,
        "snapshot": {
            "snapshot_type": "legacy_import",
            "snapshot_label": file_path.parent.name,
            "payload": payload,
            "signature": signature,
        },
    }

