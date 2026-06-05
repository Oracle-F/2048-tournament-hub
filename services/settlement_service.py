import json
from collections import defaultdict
from datetime import datetime

from services.competition_mode_service import event_uses_locking
from settings import LOCAL_TIMEZONE


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_event_bundle(connection, event_code):
    event = connection.execute(
        """
        SELECT e.*, p.code AS platform_code, v.code AS variant_code, rb.code AS rating_bucket_code
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        LEFT JOIN variants v ON v.id = e.variant_id
        LEFT JOIN rating_buckets rb ON rb.id = e.rating_bucket_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if event is None:
        raise ValueError("Event not found: {}".format(event_code))

    rule_set = connection.execute(
        """
        SELECT * FROM event_rule_sets
        WHERE event_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (event["id"],),
    ).fetchone()
    if rule_set is None:
        raise ValueError("Rule set not found for event: {}".format(event_code))
    return event, rule_set


def load_candidate_records(connection, event_id):
    return connection.execute(
        """
        SELECT
            ear.id AS event_attempt_record_id,
            ear.player_id,
            ear.performance_record_id,
            ear.derived_metric_value,
            ear.derived_metric_json,
            pr.started_at,
            pr.ended_at,
            pr.raw_score,
            pr.final_score,
            pr.primary_time_ms,
            pr.target_tile_value,
            pr.record_type,
            pr.source_record_id,
            pr.raw_payload_json,
            p.display_name,
            pa.account_name
        FROM event_attempt_records ear
        JOIN performance_records pr ON pr.id = ear.performance_record_id
        JOIN players p ON p.id = ear.player_id
        LEFT JOIN player_accounts pa ON pa.player_id = p.id AND pa.is_primary = 1
        WHERE ear.event_id = ?
          AND pr.is_valid_source = 1
        ORDER BY ear.player_id ASC, COALESCE(pr.ended_at, '') ASC, ear.performance_record_id ASC
        """,
        (event_id,),
    ).fetchall()


LOCKING_ALLOWED_RECORD_TYPES = {"locked_first_game", "tracked_next_record", "manual_score_entry"}


def is_locking_eligible_record(record):
    return str(record["record_type"] or "") in LOCKING_ALLOWED_RECORD_TYPES


def list_suspicious_locking_records(connection, event_code):
    event, _rule_set = load_event_bundle(connection, event_code)
    if not event_uses_locking(event["competition_type"], event["platform_code"], event["variant_code"]):
        return {
            "event": event,
            "rows": [],
            "reason": "event_not_locking",
        }
    start_dt = parse_local_event_time(event["start_time"])
    end_dt = parse_local_event_time(event["end_time"])
    rows = []
    for record in load_candidate_records(connection, event["id"]):
        if not is_record_within_event_window(record, start_dt, end_dt):
            continue
        if is_locking_eligible_record(record):
            continue
        rows.append(record)
    return {
        "event": event,
        "rows": rows,
        "reason": "ok",
    }


def parse_local_event_time(value):
    if not value:
        return None
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ]
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=LOCAL_TIMEZONE)
        return parsed.astimezone(LOCAL_TIMEZONE)
    except ValueError:
        pass
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=LOCAL_TIMEZONE)
        except ValueError:
            continue
    raise ValueError("Unsupported local event time: {}".format(value))


def is_record_within_event_window(record, start_dt, end_dt):
    if start_dt is None and end_dt is None:
        return True

    record_end = parse_local_event_time(record["ended_at"]) if record["ended_at"] else None
    record_start = parse_local_event_time(record["started_at"]) if record["started_at"] else record_end

    if start_dt is not None:
        if record_start is None or record_start < start_dt:
            return False
    if end_dt is not None:
        if record_end is None or record_end > end_dt:
            return False
    return True


def metric_from_record(competition_type, record, ranking_metric=None):
    if competition_type == "speedrun":
        return record["primary_time_ms"]
    if ranking_metric in {"competition_score", "weighted_board_sum"} and record["derived_metric_value"] is not None:
        return record["derived_metric_value"]

    if record["derived_metric_value"] is not None:
        return record["derived_metric_value"]
    if competition_type in {"classic_raw_score", "fibonacci_raw_score", "stone_x", "no_x", "timed_scoring"}:
        if record["final_score"] is not None:
            return record["final_score"]
        return record["raw_score"]
    return record["final_score"] if record["final_score"] is not None else record["raw_score"]


def select_records(records, aggregation_method, aggregation_count, ranking_order, competition_type, ranking_metric=None, top_n=None):
    reverse = ranking_order == "desc"
    missing_value = float("-inf") if reverse else float("inf")
    if aggregation_method == "latest":
        return records[-1:] if records else []

    ordered = sorted(
        records,
        key=lambda item: (
            metric_from_record(competition_type, item)
            if metric_from_record(competition_type, item, ranking_metric=ranking_metric) is not None
            else missing_value,
            item["ended_at"] or "",
            item["performance_record_id"],
        ),
        reverse=reverse,
    )

    if aggregation_method in {"best_single", "best_of_n", "average_of_n", "sum_of_best_n"}:
        count = aggregation_count or 1
        return ordered[:count]
    if aggregation_method == "weighted_top_n":
        return ordered[: int(top_n or 5)]
    if aggregation_method == "sum":
        return ordered
    return ordered[:1]


def _weighted_terms(metrics, weight_base):
    terms = []
    for index, metric in enumerate(metrics):
        weight = float(weight_base) ** index
        terms.append({"index": index + 1, "metric": metric, "weight": weight, "contribution": metric * weight})
    return terms


def compute_primary_value(
    selected_records,
    aggregation_method,
    competition_type,
    aggregation_count,
    missing_attempt_policy,
    ranking_metric=None,
    weight_base=0.6,
):
    metrics = [metric_from_record(competition_type, record, ranking_metric=ranking_metric) for record in selected_records]
    metrics = [value for value in metrics if value is not None]
    if not metrics:
        if aggregation_method == "average_of_n" and aggregation_count and missing_attempt_policy == "zero_fill":
            return 0
        return None

    if aggregation_method == "average_of_n":
        denominator = len(metrics)
        total = sum(metrics)
        if aggregation_count and missing_attempt_policy == "zero_fill":
            denominator = aggregation_count
        return total / denominator if denominator else None
    if aggregation_method == "sum_of_best_n":
        return sum(metrics)
    if aggregation_method == "weighted_top_n":
        return sum(item["contribution"] for item in _weighted_terms(metrics, weight_base))
    if aggregation_method == "sum":
        return sum(metrics)
    return metrics[0]


def compute_best_single(selected_records, competition_type, ranking_order, ranking_metric=None):
    metrics = [metric_from_record(competition_type, record, ranking_metric=ranking_metric) for record in selected_records]
    metrics = [value for value in metrics if value is not None]
    if not metrics:
        return None
    if ranking_order == "asc":
        return min(metrics)
    return max(metrics)


def rank_results(results, ranking_order):
    reverse = ranking_order == "desc"
    missing_value = float("-inf") if reverse else float("inf")

    def _to_cmp_values(item):
        values = [item["primary_metric_value"], item["best_single_score"]]
        values.extend(item.get("tie_break_values") or [])
        output = []
        for value in values:
            output.append(value if value is not None else missing_value)
        return output

    def _sort_key(item):
        values = _to_cmp_values(item)
        if reverse:
            values = [-value for value in values]
        return tuple(values + [item["display_name"].lower()])

    ordered = sorted(results, key=_sort_key)

    ranked = []
    previous_key = None
    current_rank = 0
    for index, item in enumerate(ordered, start=1):
        key = (
            item["primary_metric_value"],
            item["best_single_score"],
            tuple(item.get("tie_break_values") or []),
        )
        if key != previous_key:
            current_rank = index
            previous_key = key
        item["rank_value"] = current_rank
        ranked.append(item)
    return ranked


def replace_event_results(connection, event_id, ranked_results):
    connection.execute("DELETE FROM event_results WHERE event_id = ?", (event_id,))
    for result in ranked_results:
        connection.execute(
            """
            INSERT INTO event_results (
                event_id, player_id, result_status, rank_value,
                primary_metric_type, primary_metric_value,
                secondary_metric_type, secondary_metric_value,
                best_single_score, is_published, result_payload_json,
                calculated_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                event_id,
                result["player_id"],
                "calculated",
                result["rank_value"],
                result["primary_metric_type"],
                result["primary_metric_value"],
                result.get("secondary_metric_type"),
                result.get("secondary_metric_value"),
                result.get("best_single_score"),
                json.dumps(result["result_payload"], ensure_ascii=False),
                now_iso(),
                now_iso(),
            ),
        )


def build_ranked_results(connection, event_code, include_registered_without_records=False):
    event, rule_set = load_event_bundle(connection, event_code)
    rule_config = json.loads(rule_set["rule_config_json"] or "{}")
    aggregation_method = rule_set["aggregation_method"] or "best_single"
    ranking_order = rule_set["ranking_order"] or "desc"
    aggregation_count = rule_config.get("aggregation_count")
    top_n = int(rule_config.get("top_n") or 5)
    weight_base = float(rule_config.get("weight_base") or 0.6)
    missing_attempt_policy = rule_config.get("missing_attempt_policy")
    competition_type = event["competition_type"]
    ranking_metric = rule_set["ranking_metric"]
    start_dt = parse_local_event_time(event["start_time"])
    end_dt = parse_local_event_time(event["end_time"])

    records = load_candidate_records(connection, event["id"])
    event_metadata = json.loads(event["metadata_json"] or "{}")
    reservation_mode = competition_type == "timed_scoring" and event_metadata.get("timed_mode") == "reservation"
    locking_mode = event_uses_locking(event["competition_type"], event["platform_code"], event["variant_code"])
    grouped = defaultdict(list)
    for record in records:
        if reservation_mode:
            if record["record_type"] != "manual_score_entry":
                continue
            payload = json.loads(record["raw_payload_json"] or "{}")
            if payload.get("evidence_note") != "reservation_auto_settlement":
                continue
            source_record_id = (record["source_record_id"] or "")
            # V2 reservation settlement stores one record per counted game with `_game_` suffix.
            # This excludes legacy V1 single-record settlements that wrote raw-score as competition_score.
            if "_reservation_" not in source_record_id or "_game_" not in source_record_id:
                continue
        if locking_mode and not is_locking_eligible_record(record):
            continue
        if not is_record_within_event_window(record, start_dt, end_dt):
            continue
        grouped[record["player_id"]].append(record)

    registered_players = {}
    if include_registered_without_records:
        rows = connection.execute(
            """
            SELECT
                r.player_id,
                p.display_name,
                pa.account_key
            FROM registrations r
            JOIN players p ON p.id = r.player_id
            LEFT JOIN player_accounts pa
                ON pa.player_id = r.player_id
               AND pa.platform_id = ?
               AND pa.is_primary = 1
            WHERE r.event_id = ? AND r.status = 'active'
            """,
            (event["platform_id"], event["id"]),
        ).fetchall()
        registered_players = {
            row["player_id"]: {
                "display_name": row["display_name"],
                "account_name": row["account_key"],
            }
            for row in rows
        }

    ranked_inputs = []
    selected_attempt_record_ids = []
    all_player_ids = set(grouped)
    if include_registered_without_records:
        all_player_ids.update(registered_players)

    for player_id in sorted(all_player_ids):
        player_records = grouped.get(player_id, [])
        selected = select_records(
            player_records,
            aggregation_method,
            aggregation_count,
            ranking_order,
            competition_type,
            ranking_metric=ranking_metric,
            top_n=top_n,
        )
        selected_ids = [record["event_attempt_record_id"] for record in selected]
        selected_attempt_record_ids.extend(selected_ids)
        primary_value = compute_primary_value(
            selected,
            aggregation_method,
            competition_type,
            aggregation_count,
            missing_attempt_policy,
            ranking_metric=ranking_metric,
            weight_base=weight_base,
        )
        best_single = compute_best_single(selected, competition_type, ranking_order, ranking_metric=ranking_metric)
        selected_metrics = [
            metric_from_record(competition_type, record, ranking_metric=ranking_metric)
            for record in selected
            if metric_from_record(competition_type, record, ranking_metric=ranking_metric) is not None
        ]
        total_full_boards = None
        if competition_type == "timed_scoring":
            full_board_values = []
            for record in selected:
                derived_json = json.loads(record["derived_metric_json"] or "{}")
                level = derived_json.get("full_board_level")
                if isinstance(level, int):
                    full_board_values.append(level)
            total_full_boards = sum(full_board_values) if full_board_values else 0
        tie_break_values = selected_metrics[1:] if selected_metrics else []
        if competition_type == "timed_scoring":
            tie_break_values.insert(0, total_full_boards if total_full_boards is not None else 0)
            tie_break_values.append(-len(selected))
            raw_score_ties = []
            for record in selected:
                raw_like = record["final_score"] if record["final_score"] is not None else record["raw_score"]
                if raw_like is not None:
                    raw_score_ties.append(raw_like)
            raw_score_ties.sort(reverse=True)
            tie_break_values.extend(raw_score_ties)
        weighted_terms = _weighted_terms(selected_metrics, weight_base) if aggregation_method == "weighted_top_n" else []
        if player_records:
            first_record = player_records[0]
            display_name = first_record["display_name"]
            account_name = first_record["account_name"]
        else:
            player_info = registered_players.get(player_id, {})
            display_name = player_info.get("display_name") or "Unknown"
            account_name = player_info.get("account_name")

        ranked_inputs.append(
            {
                "player_id": player_id,
                "display_name": display_name,
                "account_name": account_name,
                "primary_metric_type": rule_set["ranking_metric"],
                "primary_metric_value": primary_value,
                "secondary_metric_type": "attempt_count",
                "secondary_metric_value": len(selected),
                "best_single_score": best_single,
                "tie_break_values": tie_break_values,
                "result_payload": {
                    "selected_record_ids": selected_ids,
                    "aggregation_method": aggregation_method,
                    "aggregation_count": aggregation_count,
                    "top_n": top_n,
                    "weight_base": weight_base,
                    "missing_attempt_policy": missing_attempt_policy,
                    "competition_type": competition_type,
                    "total_full_boards": total_full_boards,
                    "weighted_terms": weighted_terms,
                    "source_records": [
                        {
                            "performance_record_id": record["performance_record_id"],
                            "ended_at": record["ended_at"],
                            "raw_score": record["raw_score"],
                            "final_score": record["final_score"],
                            "competition_score": record["derived_metric_value"],
                            "primary_time_ms": record["primary_time_ms"],
                            "target_tile_value": record["target_tile_value"],
                        }
                        for record in selected
                    ],
                },
            }
        )

    ranked = rank_results(ranked_inputs, ranking_order)
    if competition_type == "timed_scoring":
        for index, item in enumerate(ranked, start=1):
            item["rank_value"] = index
    return {
        "event": event,
        "rule_set": rule_set,
        "rule_config": rule_config,
        "ranked_results": ranked,
        "record_count": len(records),
        "selected_attempt_record_ids": selected_attempt_record_ids,
        "aggregation_method": aggregation_method,
    }


def update_attempt_record_statuses(connection, selected_record_ids):
    if not selected_record_ids:
        return
    placeholders = ",".join("?" for _ in selected_record_ids)
    connection.execute(
        """
        UPDATE event_attempt_records
        SET evaluation_status = 'used', updated_at = ?
        WHERE id IN ({})
        """.format(placeholders),
        (now_iso(), *selected_record_ids),
    )


def reset_attempt_record_statuses(connection, event_id):
    connection.execute(
        """
        UPDATE event_attempt_records
        SET evaluation_status = 'pending', updated_at = ?
        WHERE event_id = ?
        """,
        (now_iso(), event_id),
    )


def settle_event(connection, event_code):
    bundle = build_ranked_results(connection, event_code, include_registered_without_records=False)
    event = bundle["event"]
    ranked = bundle["ranked_results"]
    reset_attempt_record_statuses(connection, event["id"])
    update_attempt_record_statuses(connection, bundle["selected_attempt_record_ids"])
    replace_event_results(connection, event["id"], ranked)

    return {
        "event_id": event["id"],
        "event_code": event["event_code"],
        "player_count": len(ranked),
        "record_count": bundle["record_count"],
        "aggregation_method": bundle["aggregation_method"],
    }
