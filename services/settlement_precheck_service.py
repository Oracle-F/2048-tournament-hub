import json
from collections import defaultdict

from services.competition_mode_service import event_uses_locking
from services.event_progress_service import build_event_progress_snapshot
from services.replay_lock_service import list_replay_chain_issues
from services.settlement_service import (
    is_record_within_event_window,
    load_candidate_records,
    load_event_bundle,
    metric_from_record,
    parse_local_event_time,
)


def _player_label(player):
    username = player.get("username")
    display_name = player.get("display_name") or username or "Unknown"
    if username and username != display_name:
        return "{} ({})".format(display_name, username)
    return display_name


def _record_metric(event, record):
    return metric_from_record(event["competition_type"], record)


def _load_all_attempt_records(connection, event_id):
    rows = connection.execute(
        """
        SELECT
            ear.id AS event_attempt_record_id,
            ear.player_id,
            ear.performance_record_id,
            ear.derived_metric_value,
            pr.started_at,
            pr.ended_at,
            pr.record_type,
            pr.raw_score,
            pr.final_score,
            pr.primary_time_ms,
            pr.is_valid_source,
            pr.raw_payload_json,
            p.display_name,
            pa.account_key AS username
        FROM event_attempt_records ear
        JOIN performance_records pr ON pr.id = ear.performance_record_id
        JOIN players p ON p.id = ear.player_id
        LEFT JOIN player_accounts pa ON pa.player_id = p.id AND pa.is_primary = 1
        WHERE ear.event_id = ?
        ORDER BY ear.player_id ASC, COALESCE(pr.ended_at, '') ASC, ear.performance_record_id ASC
        """,
        (event_id,),
    ).fetchall()
    parsed = []
    for row in rows:
        payload = {}
        if row["raw_payload_json"]:
            try:
                payload = json.loads(row["raw_payload_json"])
            except json.JSONDecodeError:
                payload = {}
        parsed.append({**dict(row), "raw_payload": payload})
    return parsed


def _collect_duplicates(event, records):
    grouped = defaultdict(list)
    for record in records:
        grouped[
            (
                record["player_id"],
                record["ended_at"],
                _record_metric(event, record),
            )
        ].append(record)
    duplicates = []
    for items in grouped.values():
        if len(items) < 2:
            continue
        duplicates.append(
            {
                "player_id": items[0]["player_id"],
                "label": _player_label(items[0]),
                "ended_at": items[0]["ended_at"],
                "metric": _record_metric(event, items[0]),
                "record_ids": [item["event_attempt_record_id"] for item in items],
            }
        )
    duplicates.sort(key=lambda item: (item["label"].lower(), item["ended_at"] or "", item["metric"] or 0))
    return duplicates


def run_settlement_precheck(connection, event_code):
    event, rule_set = load_event_bundle(connection, event_code)
    rule_config = json.loads(rule_set["rule_config_json"] or "{}")
    aggregation_method = rule_set["aggregation_method"] or "best_single"
    aggregation_count = int(rule_config.get("aggregation_count") or 0)
    start_dt = parse_local_event_time(event["start_time"])
    end_dt = parse_local_event_time(event["end_time"])

    progress = build_event_progress_snapshot(connection, event_code)
    all_records = _load_all_attempt_records(connection, event["id"])
    voided_records = [record for record in all_records if not record["is_valid_source"]]
    valid_records = []
    outside_window_records = []
    manual_unknown_start_records = []
    for record in all_records:
        if not record["is_valid_source"]:
            continue
        if is_record_within_event_window(record, start_dt, end_dt):
            valid_records.append(record)
        else:
            outside_window_records.append(record)
        if record["raw_payload"].get("started_at_was_unknown"):
            manual_unknown_start_records.append(record)

    blockers = []
    warnings = []
    infos = []

    if start_dt is not None and end_dt is not None and start_dt > end_dt:
        blockers.append("比赛开始时间晚于结束时间。")
    if not valid_records:
        blockers.append("比赛时间窗内没有任何有效成绩记录。")
    if event["is_rated"] and not event["rating_bucket_code"]:
        blockers.append("该比赛设置为计 rating，但没有可用的 rating bucket。")

    if event_uses_locking(event["competition_type"], event["platform_code"], event["variant_code"]):
        replay_issues = list_replay_chain_issues(connection, event_code)
        blockers.extend(replay_issues)
        missing_session_players = [
            item
            for item in progress["participants"]
            if item["valid_record_count"] > 0 and item.get("latest_session_status") in {None, "expired", "cancelled"}
        ]
        for item in missing_session_players:
            blockers.append("选手 {} 有有效成绩，但缺少可用锁局会话（或会话已失效）。".format(_player_label(item)))

    pending_lock_count = sum(1 for item in progress["participants"] if item["latest_session_status"] == "pending_lock")
    locked_in_progress_count = sum(1 for item in progress["participants"] if item["latest_session_status"] == "locked_in_progress")
    if pending_lock_count or locked_in_progress_count:
        warnings.append(
            "仍有未收口锁局会话：待锁定 {} 人，锁定进行中 {} 人。".format(
                pending_lock_count,
                locked_in_progress_count,
            )
        )

    event_status = event["status"] or "-"
    if event_status != "finished":
        warnings.append("比赛状态当前为 {}，还没有标记为 finished。".format(event_status))

    missing_records = [item for item in progress["participants"] if item["valid_record_count"] <= 0]
    if missing_records:
        sample = ", ".join(_player_label(item) for item in missing_records[:5])
        extra = "" if len(missing_records) <= 5 else " 等{}人".format(len(missing_records))
        warnings.append("有报名选手还没有有效成绩：{}{}。".format(sample, extra))

    if aggregation_method in {"best_of_n", "average_of_n", "sum_of_best_n"} and aggregation_count > 0:
        insufficient = [
            item
            for item in progress["participants"]
            if 0 < item["valid_record_count"] < aggregation_count
        ]
        if insufficient:
            sample = ", ".join(
                "{}({}/{})".format(_player_label(item), item["valid_record_count"], aggregation_count)
                for item in insufficient[:5]
            )
            extra = "" if len(insufficient) <= 5 else " 等{}人".format(len(insufficient))
            warnings.append("部分选手有效局数不足要求：{}{}。".format(sample, extra))

    if outside_window_records:
        players = []
        seen = set()
        for record in outside_window_records:
            label = _player_label(record)
            if label in seen:
                continue
            seen.add(label)
            players.append(label)
        sample = ", ".join(players[:5])
        extra = "" if len(players) <= 5 else " 等{}人".format(len(players))
        warnings.append(
            "发现 {} 条时间窗外成绩记录，结算时会自动忽略，涉及：{}{}。".format(
                len(outside_window_records),
                sample,
                extra,
            )
        )

    if manual_unknown_start_records:
        infos.append(
            "有 {} 条人工补录成绩未提供 started_at，当前按 ended_at 参与时间窗判断。".format(
                len(manual_unknown_start_records)
            )
        )
    if voided_records:
        infos.append("当前已有 {} 条成绩记录被作废，结算时会自动忽略。".format(len(voided_records)))

    duplicates = _collect_duplicates(event, valid_records)
    if duplicates:
        sample = ", ".join(
            "{} @{} / {}".format(item["label"], item["ended_at"] or "-", item["metric"] if item["metric"] is not None else "-")
            for item in duplicates[:5]
        )
        extra = "" if len(duplicates) <= 5 else " 等{}组".format(len(duplicates))
        warnings.append("发现疑似重复成绩记录：{}{}。".format(sample, extra))

    if event["is_rated"] and not event["is_official"]:
        warnings.append("该比赛会计 rating，但当前未标记为正式赛，请确认这是否符合预期。")

    infos.append(
        "有效成绩 {} 条，报名 {} 人，已结算结果 {} 人。".format(
            len(valid_records),
            len([item for item in progress["participants"] if item.get("registration_status") == "active"]),
            connection.execute("SELECT COUNT(*) AS count FROM event_results WHERE event_id = ?", (event["id"],)).fetchone()["count"],
        )
    )

    return {
        "event": event,
        "rule_set": rule_set,
        "rule_config": rule_config,
        "aggregation_method": aggregation_method,
        "aggregation_count": aggregation_count,
        "progress": progress,
        "blockers": blockers,
        "warnings": warnings,
        "infos": infos,
        "outside_window_record_count": len(outside_window_records),
        "manual_unknown_start_count": len(manual_unknown_start_records),
        "duplicate_groups": duplicates,
        "valid_record_count": len(valid_records),
    }
