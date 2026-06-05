import json
from datetime import datetime


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def generate_next_event_code(connection):
    row = connection.execute(
        """
        SELECT MAX(CAST(event_code AS INTEGER)) AS max_code
        FROM events
        WHERE event_code GLOB '[0-9]*'
        """
    ).fetchone()
    max_code = row["max_code"] if row is not None else None
    next_code = int(max_code or 999) + 1
    return str(next_code)


def lookup_id(connection, table_name, code):
    row = connection.execute(
        "SELECT id FROM {} WHERE code = ?".format(table_name),
        (code,),
    ).fetchone()
    return None if row is None else row["id"]


def default_rule_values(event_type, competition_type):
    if competition_type == "timed_scoring":
        return {
            "rule_type": "timed_scoring",
            "ranking_metric": "custom_points",
            "ranking_order": "desc",
            "aggregation_method": "sum",
            "validation_rule": "window_closed_games",
        }
    if competition_type == "classic_raw_score":
        return {
            "rule_type": "classic_raw_score",
            "ranking_metric": "raw_score",
            "ranking_order": "desc",
            "aggregation_method": "best_single",
            "validation_rule": "event_attempt_selection",
        }
    if competition_type == "points_series_3x4":
        return {
            "rule_type": "points_series_3x4",
            "ranking_metric": "weighted_board_sum",
            "ranking_order": "desc",
            "aggregation_method": "weighted_top_n",
            "validation_rule": "event_attempt_selection",
        }
    if competition_type == "fibonacci_raw_score":
        return {
            "rule_type": "fibonacci_raw_score",
            "ranking_metric": "raw_score",
            "ranking_order": "desc",
            "aggregation_method": "best_single",
            "validation_rule": "fibonacci_variant",
        }
    if competition_type == "stone_x":
        return {
            "rule_type": "stone_x",
            "ranking_metric": "competition_score",
            "ranking_order": "desc",
            "aggregation_method": "best_single",
            "validation_rule": "stone_target",
        }
    if competition_type == "no_x":
        return {
            "rule_type": "no_x",
            "ranking_metric": "competition_score",
            "ranking_order": "desc",
            "aggregation_method": "best_single",
            "validation_rule": "no_x_target",
        }
    if competition_type == "speedrun":
        return {
            "rule_type": "speedrun",
            "ranking_metric": "completion_time_ms",
            "ranking_order": "asc",
            "aggregation_method": "best_single",
            "validation_rule": "speedrun_target",
        }
    return {
        "rule_type": competition_type or event_type,
        "ranking_metric": "competition_score",
        "ranking_order": "desc",
        "aggregation_method": None,
        "validation_rule": None,
    }


def infer_rating_bucket_code(variant_code, competition_type, target_value):
    if competition_type == "timed_scoring":
        mapping = {"2x4": "timed_2x4", "3x3": "timed_3x3"}
        return mapping.get(variant_code)
    if competition_type == "classic_raw_score" and variant_code == "4x4":
        return "classic_4x4_raw_score"
    if competition_type == "points_series_3x4" and variant_code == "3x4":
        return "points_series_3x4"
    if competition_type == "stone_x" and variant_code == "4x4":
        mapping = {1024: "stone_1k_4x4", 2048: "stone_2k_4x4"}
        return mapping.get(target_value)
    if competition_type == "no_x" and variant_code == "4x4":
        mapping = {
            1024: "no_1k_4x4",
            2048: "no_2k_4x4",
            8192: "no_8k_4x4",
            16384: "no_16k_4x4",
        }
        return mapping.get(target_value)
    if competition_type == "speedrun" and variant_code == "4x4":
        mapping = {1024: "speedrun_1k_4x4", 2048: "speedrun_2k_4x4"}
        return mapping.get(target_value)
    if competition_type == "fibonacci_raw_score":
        mapping = {
            "fibonacci_3x3": "fibonacci_3x3_raw_score",
            "fibonacci_4x4": "fibonacci_4x4_raw_score",
            "fibonacci_5x5": "fibonacci_5x5_raw_score",
        }
        return mapping.get(variant_code)
    return None


def create_or_update_event(
    connection,
    *,
    event_code,
    event_name,
    platform_code,
    variant_code,
    event_type,
    competition_type,
    rating_bucket_code,
    status,
    is_official,
    is_rated,
    registration_close_time,
    start_time,
    end_time,
    seal_time,
    source,
    tags,
    target_value,
    metadata,
    rule_overrides,
):
    event_code = str(event_code).strip() if event_code not in (None, "") else ""
    if not event_code or (source in {"organizer_raw_score", "organizer_event_hub"} and not event_code.isdigit()):
        event_code = generate_next_event_code(connection)
    platform_id = lookup_id(connection, "platforms", platform_code)
    if platform_id is None:
        raise ValueError("Unknown platform code: {}".format(platform_code))

    variant_id = lookup_id(connection, "variants", variant_code) if variant_code else None
    if variant_code and variant_id is None:
        raise ValueError("Unknown variant code: {}".format(variant_code))

    if rating_bucket_code:
        bucket_id = lookup_id(connection, "rating_buckets", rating_bucket_code)
        if bucket_id is None:
            raise ValueError("Unknown rating bucket code: {}".format(rating_bucket_code))
    else:
        bucket_id = None

    connection.execute(
        """
        INSERT INTO events (
            event_code, event_name, platform_id, variant_id, rating_bucket_id,
            event_type, competition_type, status, is_official, is_rated,
            registration_close_time, start_time, end_time, seal_time, source, tags_json, metadata_json,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_code) DO UPDATE SET
            event_name = excluded.event_name,
            platform_id = excluded.platform_id,
            variant_id = excluded.variant_id,
            rating_bucket_id = excluded.rating_bucket_id,
            event_type = excluded.event_type,
            competition_type = excluded.competition_type,
            status = excluded.status,
            is_official = excluded.is_official,
            is_rated = excluded.is_rated,
            registration_close_time = excluded.registration_close_time,
            start_time = excluded.start_time,
            end_time = excluded.end_time,
            seal_time = excluded.seal_time,
            source = excluded.source,
            tags_json = excluded.tags_json,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            event_code,
            event_name,
            platform_id,
            variant_id,
            bucket_id,
            event_type,
            competition_type,
            status,
            1 if is_official else 0,
            1 if is_rated else 0,
            registration_close_time,
            start_time,
            end_time,
            seal_time,
            source,
            json.dumps(tags, ensure_ascii=False),
            json.dumps(metadata, ensure_ascii=False),
            now_iso(),
            now_iso(),
        ),
    )
    event_id = connection.execute(
        "SELECT id FROM events WHERE event_code = ?",
        (event_code,),
    ).fetchone()["id"]

    default_rules = default_rule_values(event_type, competition_type)
    for key, value in rule_overrides.items():
        if value is not None:
            default_rules[key] = value

    rule_config = {
        "event_type": event_type,
        "competition_type": competition_type,
        "target_value": target_value,
        "source": source,
        "tags": tags,
    }
    if start_time or end_time:
        rule_config["time_window"] = {"start": start_time, "end": end_time}
    if seal_time:
        rule_config["seal_time"] = seal_time
    if metadata:
        rule_config["metadata"] = metadata
    if "aggregation_count" in rule_overrides and rule_overrides["aggregation_count"] is not None:
        rule_config["aggregation_count"] = rule_overrides["aggregation_count"]
    if "missing_attempt_policy" in rule_overrides and rule_overrides["missing_attempt_policy"] is not None:
        rule_config["missing_attempt_policy"] = rule_overrides["missing_attempt_policy"]
    if "top_n" in rule_overrides and rule_overrides["top_n"] is not None:
        rule_config["top_n"] = rule_overrides["top_n"]
    if "weight_base" in rule_overrides and rule_overrides["weight_base"] is not None:
        rule_config["weight_base"] = rule_overrides["weight_base"]
    if "single_metric" in rule_overrides and rule_overrides["single_metric"]:
        rule_config["single_metric"] = rule_overrides["single_metric"]

    connection.execute(
        """
        INSERT INTO event_rule_sets (
            event_id, version, rule_type, ranking_metric, ranking_order,
            aggregation_method, validation_rule, tiebreakers_json, rule_config_json, created_at
        )
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id, version) DO UPDATE SET
            rule_type = excluded.rule_type,
            ranking_metric = excluded.ranking_metric,
            ranking_order = excluded.ranking_order,
            aggregation_method = excluded.aggregation_method,
            validation_rule = excluded.validation_rule,
            tiebreakers_json = excluded.tiebreakers_json,
            rule_config_json = excluded.rule_config_json
        """,
        (
            event_id,
            default_rules["rule_type"],
            default_rules["ranking_metric"],
            default_rules["ranking_order"],
            default_rules.get("aggregation_method"),
            default_rules.get("validation_rule"),
            json.dumps(rule_overrides.get("tiebreakers", []), ensure_ascii=False),
            json.dumps(rule_config, ensure_ascii=False),
            now_iso(),
        ),
    )

    return {
        "event_id": event_id,
        "event_code": event_code,
        "rating_bucket_code": rating_bucket_code,
    }
