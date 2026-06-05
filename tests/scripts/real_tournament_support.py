from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from db import transaction
from services.event_admin_service import create_or_update_event
from services.raw_score_export_service import load_event_export_bundle
from services.rating_service import recalculate_event_bucket_ratings
from services.raw_score_import_service import import_score_file
from services.settlement_service import build_ranked_results, settle_event
from settings import PRODUCTION_DATABASE_PATH
from .testing_support import test_temporary_directory


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_TOURNAMENTS_DIR = PROJECT_ROOT / "tests" / "real_tournaments"
SNAPSHOT_SCHEMA_VERSION = 1


def open_readonly_connection(database_path: Path = PRODUCTION_DATABASE_PATH) -> sqlite3.Connection:
    uri = "file:{}?mode=ro".format(Path(database_path).resolve().as_posix())
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _load_json_text(value: str | None):
    if not value:
        return None
    return json.loads(value)


def _compare_values(expected, actual, path: str, diffs: list[str], *, tolerance: float = 1e-6):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            diffs.append("{}: expected object, got {}".format(path, type(actual).__name__))
            return
        expected_keys = set(expected.keys())
        actual_keys = set(actual.keys())
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        if missing:
            diffs.append("{}: missing keys {}".format(path, missing))
        if extra:
            diffs.append("{}: unexpected keys {}".format(path, extra))
        for key in sorted(expected_keys & actual_keys):
            _compare_values(expected[key], actual[key], "{}.{}".format(path, key), diffs, tolerance=tolerance)
        return

    if isinstance(expected, list):
        if not isinstance(actual, list):
            diffs.append("{}: expected list, got {}".format(path, type(actual).__name__))
            return
        if len(expected) != len(actual):
            diffs.append("{}: length {} != {}".format(path, len(expected), len(actual)))
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            _compare_values(expected_item, actual_item, "{}[{}]".format(path, index), diffs, tolerance=tolerance)
        return

    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if abs(float(expected) - float(actual)) > tolerance:
            diffs.append("{}: expected {} != actual {}".format(path, expected, actual))
        return

    if expected != actual:
        diffs.append("{}: expected {!r} != actual {!r}".format(path, expected, actual))


def normalize_event_row(row, target_value=None):
    if target_value is None:
        try:
            target_value = row["target_value"]
        except Exception:
            target_value = None
    return {
        "event_code": row["event_code"],
        "event_name": row["event_name"],
        "platform_code": row["platform_code"],
        "variant_code": row["variant_code"],
        "rating_bucket_code": row["rating_bucket_code"],
        "event_type": row["event_type"],
        "competition_type": row["competition_type"],
        "status": row["status"],
        "is_official": bool(row["is_official"]),
        "is_rated": bool(row["is_rated"]),
        "registration_close_time": row["registration_close_time"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "seal_time": row["seal_time"],
        "source": row["source"],
        "tags": _load_json_text(row["tags_json"]) or [],
        "metadata": _load_json_text(row["metadata_json"]) or {},
        "target_value": target_value,
    }


def normalize_rule_set_row(row):
    return {
        "version": row["version"],
        "rule_type": row["rule_type"],
        "ranking_metric": row["ranking_metric"],
        "ranking_order": row["ranking_order"],
        "aggregation_method": row["aggregation_method"],
        "validation_rule": row["validation_rule"],
        "tiebreakers": _load_json_text(row["tiebreakers_json"]) or [],
        "rule_config": _load_json_text(row["rule_config_json"]) or {},
    }


def normalize_raw_score_row(row):
    return {
        "username": row["username"],
        "display_name": row["display_name"],
        "source_record_id": row["source_record_id"],
        "record_type": row["record_type"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "raw_score": row["raw_score"],
        "final_score": row["final_score"],
        "competition_score": row["competition_score"],
        "primary_time_ms": row["primary_time_ms"],
        "target_tile_value": row["target_tile_value"],
        "score_before_target": row["score_before_target"],
        "result_state": row["result_state"],
        "evidence_json": row["evidence_json"],
        "raw_payload_json": row["raw_payload_json"],
    }


def normalize_result_payload(payload):
    payload = payload or {}
    source_records = []
    for record in payload.get("source_records") or []:
        source_records.append(
            {
                "ended_at": record.get("ended_at"),
                "raw_score": record.get("raw_score"),
                "final_score": record.get("final_score"),
                "primary_time_ms": record.get("primary_time_ms"),
                "target_tile_value": record.get("target_tile_value"),
            }
        )
    return {
        "aggregation_method": payload.get("aggregation_method"),
        "aggregation_count": payload.get("aggregation_count"),
        "missing_attempt_policy": payload.get("missing_attempt_policy"),
        "competition_type": payload.get("competition_type"),
        "total_full_boards": payload.get("total_full_boards"),
        "weighted_terms": payload.get("weighted_terms") or [],
        "source_records": source_records,
    }


def normalize_ranking_result(row):
    return {
        "rank": row["rank_value"],
        "username": row["username"],
        "display_name": row["display_name"],
        "primary_metric_type": row["primary_metric_type"],
        "primary_metric_value": row["primary_metric_value"],
        "secondary_metric_type": row["secondary_metric_type"],
        "secondary_metric_value": row["secondary_metric_value"],
        "best_single_score": row["best_single_score"],
        "result_payload": normalize_result_payload(_load_json_text(row["result_payload_json"]) or {}),
    }


def normalize_exported_ranking_result(item):
    result = dict(item or {})
    payload = dict(result.get("result_payload") or {})
    payload.pop("total_full_boards", None)
    result["result_payload"] = payload
    return result


def normalize_rating_result(row):
    return {
        "username": row["username"],
        "display_name": row["display_name"],
        "old_rating": row["old_rating"],
        "new_rating": row["new_rating"],
        "delta_rating": row["delta_rating"],
        "old_deviation": row["old_deviation"],
        "new_deviation": row["new_deviation"],
        "placement": row["placement"],
        "field_size": row["field_size"],
        "details": _load_json_text(row["details_json"]) or {},
    }


def _compare_event_rows(expected, actual, diffs: list[str]):
    _compare_values(expected, actual, "event", diffs)


def _compare_rule_set_rows(expected, actual, diffs: list[str]):
    _compare_values(expected, actual, "rule_set", diffs)


def _compare_named_rows(kind: str, expected_rows: list[dict[str, Any]], actual_rows: list[dict[str, Any]], key_name: str, diffs: list[str]):
    expected_by_key = {row[key_name]: row for row in expected_rows}
    actual_by_key = {row[key_name]: row for row in actual_rows}
    if set(expected_by_key) != set(actual_by_key):
        missing = sorted(set(expected_by_key) - set(actual_by_key))
        extra = sorted(set(actual_by_key) - set(expected_by_key))
        if missing:
            diffs.append("{}: missing {}".format(kind, missing))
        if extra:
            diffs.append("{}: unexpected {}".format(kind, extra))
    for key in sorted(set(expected_by_key) & set(actual_by_key)):
        _compare_values(expected_by_key[key], actual_by_key[key], "{}[{}]".format(kind, key), diffs)


def load_real_tournament_snapshot(path: Path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = [
        "schema_version",
        "snapshot_type",
        "source",
        "event",
        "rule_set",
        "players",
        "raw_scores",
        "ranking_results",
        "rating_results",
    ]
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError("Missing fields {} in {}".format(", ".join(missing), path))
    return payload


def export_real_tournament_snapshot(connection, event_id: int):
    event_row = connection.execute(
        """
        SELECT
            e.*,
            p.code AS platform_code,
            v.code AS variant_code,
            rb.code AS rating_bucket_code
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        LEFT JOIN rating_buckets rb ON rb.id = e.rating_bucket_id
        WHERE e.id = ?
        """,
        (event_id,),
    ).fetchone()
    if event_row is None:
        raise ValueError("Event not found: {}".format(event_id))

    rule_row = connection.execute(
        """
        SELECT *
        FROM event_rule_sets
        WHERE event_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if rule_row is None:
        raise ValueError("Rule set not found for event id: {}".format(event_id))
    rule_config = _load_json_text(rule_row["rule_config_json"]) or {}

    event_code = event_row["event_code"]
    _event, _rule_set, ranking_rows = load_event_export_bundle(connection, event_code)

    raw_rows = connection.execute(
        """
        SELECT
            pr.source_record_id,
            pr.record_type,
            pr.started_at,
            pr.ended_at,
            pr.raw_score,
            pr.final_score,
            ear.derived_metric_value AS competition_score,
            pr.primary_time_ms,
            pr.target_tile_value,
            pr.score_before_target,
            pr.result_state,
            pr.evidence_json,
            pr.raw_payload_json,
            p.display_name,
            pa.account_key AS username
        FROM event_attempt_records ear
        JOIN performance_records pr ON pr.id = ear.performance_record_id
        JOIN players p ON p.id = ear.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = ear.player_id
           AND pa.is_primary = 1
        WHERE ear.event_id = ?
        ORDER BY COALESCE(pr.ended_at, pr.started_at, '') ASC, pr.id ASC
        """,
        (event_id,),
    ).fetchall()

    rating_rows = connection.execute(
        """
        SELECT
            rh.old_rating,
            rh.new_rating,
            rh.delta_rating,
            rh.old_deviation,
            rh.new_deviation,
            rh.placement,
            rh.field_size,
            rh.details_json,
            p.display_name,
            pa.account_key AS username
        FROM rating_history rh
        JOIN players p ON p.id = rh.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = rh.player_id
           AND pa.is_primary = 1
        WHERE rh.event_id = ?
        ORDER BY rh.placement ASC, LOWER(COALESCE(pa.account_key, p.display_name)) ASC
        """,
        (event_id,),
    ).fetchall()

    players = []
    seen_players = set()
    for row in raw_rows:
        key = (row["username"], row["display_name"])
        if key in seen_players:
            continue
        seen_players.add(key)
        players.append(
            {
                "username": row["username"],
                "display_name": row["display_name"],
            }
        )

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_type": "real_tournament",
        "source": {
            "database": str(PRODUCTION_DATABASE_PATH.name),
            "event_id": event_row["id"],
            "event_code": event_code,
        },
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "event": normalize_event_row(event_row, target_value=rule_config.get("target_value")),
        "rule_set": normalize_rule_set_row(rule_row),
        "players": players,
        "raw_scores": [normalize_raw_score_row(row) for row in raw_rows],
        "ranking_results": [normalize_ranking_result(row) for row in ranking_rows],
        "rating_results": [normalize_rating_result(row) for row in rating_rows],
        "summary": {
            "player_count": len(players),
            "raw_score_count": len(raw_rows),
            "ranking_count": len(ranking_rows),
            "rating_count": len(rating_rows),
        },
    }
    return snapshot


def write_real_tournament_snapshot(snapshot, output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def replay_real_tournament_snapshot(connection, snapshot):
    event = snapshot["event"]
    rule_set = snapshot["rule_set"]
    target_value = (rule_set.get("rule_config") or {}).get("target_value")
    diffs: list[str] = []

    rule_overrides = {
        "ranking_metric": rule_set.get("ranking_metric"),
        "ranking_order": rule_set.get("ranking_order"),
        "aggregation_method": rule_set.get("aggregation_method"),
        "validation_rule": rule_set.get("validation_rule"),
        "tiebreakers": rule_set.get("tiebreakers") or [],
        "aggregation_count": (rule_set.get("rule_config") or {}).get("aggregation_count"),
        "missing_attempt_policy": (rule_set.get("rule_config") or {}).get("missing_attempt_policy"),
        "top_n": (rule_set.get("rule_config") or {}).get("top_n"),
        "weight_base": (rule_set.get("rule_config") or {}).get("weight_base"),
        "single_metric": (rule_set.get("rule_config") or {}).get("single_metric"),
    }

    with transaction(connection):
        created_event = create_or_update_event(
            connection,
            event_code=event["event_code"],
            event_name=event["event_name"],
            platform_code=event["platform_code"],
            variant_code=event["variant_code"],
            event_type=event["event_type"],
            competition_type=event["competition_type"],
            rating_bucket_code=event["rating_bucket_code"],
            status=event["status"],
            is_official=bool(event["is_official"]),
            is_rated=bool(event["is_rated"]),
            registration_close_time=event["registration_close_time"],
            start_time=event["start_time"],
            end_time=event["end_time"],
            seal_time=event["seal_time"],
            source=event["source"],
            tags=event["tags"],
            target_value=target_value,
            metadata=event["metadata"],
            rule_overrides=rule_overrides,
        )
    replay_event_code = created_event["event_code"]

    actual_event_row = connection.execute(
        """
        SELECT
            e.*,
            p.code AS platform_code,
            v.code AS variant_code,
            rb.code AS rating_bucket_code
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        LEFT JOIN rating_buckets rb ON rb.id = e.rating_bucket_id
        WHERE e.event_code = ?
        """,
        (replay_event_code,),
    ).fetchone()
    actual_rule_row = connection.execute(
        """
        SELECT *
        FROM event_rule_sets
        WHERE event_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (actual_event_row["id"],),
    ).fetchone()
    expected_event = dict(event)
    if str(expected_event.get("event_code") or "").isdigit() is False and expected_event.get("event_code") != replay_event_code:
        expected_event["event_code"] = replay_event_code
    _compare_event_rows(expected_event, normalize_event_row(actual_event_row, target_value=target_value), diffs)
    _compare_rule_set_rows(rule_set, normalize_rule_set_row(actual_rule_row), diffs)

    import_summary = None
    with test_temporary_directory(prefix="real_tournament_import_") as temp_dir:
        score_file = temp_dir / "{}.json".format(event["event_code"])
        score_file.write_text(json.dumps(snapshot["raw_scores"], ensure_ascii=False, indent=2), encoding="utf-8")
        with transaction(connection):
            import_summary = import_score_file(connection, replay_event_code, score_file)

    build_summary = build_ranked_results(connection, replay_event_code)
    settle_summary = None
    with transaction(connection):
        settle_summary = settle_event(connection, replay_event_code)
    rating_summary = None
    with transaction(connection):
        rating_summary = recalculate_event_bucket_ratings(connection, replay_event_code)

    actual_ranking_rows = connection.execute(
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
        (actual_event_row["id"],),
    ).fetchall()
    actual_rating_rows = connection.execute(
        """
        SELECT
            rh.old_rating,
            rh.new_rating,
            rh.delta_rating,
            rh.old_deviation,
            rh.new_deviation,
            rh.placement,
            rh.field_size,
            rh.details_json,
            p.display_name,
            pa.account_key AS username
        FROM rating_history rh
        JOIN players p ON p.id = rh.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = rh.player_id
           AND pa.is_primary = 1
        WHERE rh.event_id = ?
        ORDER BY rh.placement ASC, LOWER(COALESCE(pa.account_key, p.display_name)) ASC
        """,
        (actual_event_row["id"],),
    ).fetchall()

    if import_summary["record_count"] != snapshot["summary"]["raw_score_count"]:
        diffs.append(
            "import.record_count: expected {} != actual {}".format(
                snapshot["summary"]["raw_score_count"], import_summary["record_count"]
            )
        )
    if import_summary["new_players"] != snapshot["summary"]["player_count"]:
        diffs.append(
            "import.new_players: expected {} != actual {}".format(
                snapshot["summary"]["player_count"], import_summary["new_players"]
            )
        )
    if build_summary["record_count"] != snapshot["summary"]["raw_score_count"]:
        diffs.append(
            "build.record_count: expected {} != actual {}".format(
                snapshot["summary"]["raw_score_count"], build_summary["record_count"]
            )
        )
    if settle_summary["player_count"] != snapshot["summary"]["ranking_count"]:
        diffs.append(
            "settle.player_count: expected {} != actual {}".format(
                snapshot["summary"]["ranking_count"], settle_summary["player_count"]
            )
        )
    if rating_summary["history_count"] != snapshot["summary"]["rating_count"]:
        diffs.append(
            "rating.history_count: expected {} != actual {}".format(
                snapshot["summary"]["rating_count"], rating_summary["history_count"]
            )
        )

    _compare_named_rows(
        "ranking",
        [normalize_exported_ranking_result(item) for item in snapshot["ranking_results"]],
        [normalize_exported_ranking_result(normalize_ranking_result(row)) for row in actual_ranking_rows],
        "username",
        diffs,
    )
    _compare_named_rows(
        "rating",
        snapshot["rating_results"],
        [normalize_rating_result(row) for row in actual_rating_rows],
        "username",
        diffs,
    )

    return {
        "ok": not diffs,
        "diffs": diffs,
        "import_summary": import_summary,
        "build_summary": build_summary,
        "settle_summary": settle_summary,
        "rating_summary": rating_summary,
    }
