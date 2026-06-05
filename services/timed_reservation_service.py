import json
from datetime import datetime, timedelta

from services.raw_score_import_service import add_manual_score
from services.registration_service import parse_event_time
from services.settlement_service import settle_event
from services.verse_adapter import fetch_recent_games, get_game_end_time, get_game_start_time
from settings import LOCAL_TIMEZONE

TIMED_SCORING_RULES = {
    "2x4": [
        (18, (512, 256, 128, 64), 4),
        (17, (512, 256, 128, 32, 16, 8, 4, 2), 4),
        (15, (512, 256, 128), 3),
        (14, (512, 256, 64, 32, 16, 8, 4, 2), 3),
        (12, (512, 256, 64), 2),
        (11, (512, 256), 2),
        (10, (512, 128, 64, 32, 16, 8, 4, 2), 2),
        (8, (512, 128, 64), 1),
        (7, (512, 128), 1),
        (6, (512,), 1),
        (5, (256, 128, 64, 32, 16, 8, 4, 2), 1),
        (3, (256, 128, 64), 0),
        (2, (256, 128), 0),
        (1, (256,), 0),
    ],
    "3x3": [
        (25, (1024, 512, 256, 128, 64), 5),
        (24, (1024, 512, 256, 128, 32, 16, 8, 4, 2), 5),
        (22, (1024, 512, 256, 128), 4),
        (21, (1024, 512, 256, 64, 32, 16, 8, 4, 2), 4),
        (19, (1024, 512, 256, 64), 3),
        (18, (1024, 512, 256), 3),
        (17, (1024, 512, 128, 64, 32, 16, 8, 4, 2), 3),
        (15, (1024, 512, 128, 64), 2),
        (14, (1024, 512, 128), 2),
        (13, (1024, 512), 2),
        (12, (1024, 256, 128, 64, 32, 16, 8, 4, 2), 2),
        (10, (1024, 256, 128, 64), 1),
        (9, (1024, 256, 128), 1),
        (8, (1024, 256), 1),
        (7, (1024,), 1),
        (6, (512, 256, 128, 64, 32, 16, 8, 4, 2), 1),
        (4, (512, 256, 128, 64), 0),
        (3, (512, 256, 128), 0),
        (2, (512, 256), 0),
        (1, (512,), 0),
    ],
}


def now_local():
    return datetime.now(LOCAL_TIMEZONE)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _parse_json(value):
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_event_row(connection, event_code):
    row = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            e.start_time,
            e.end_time,
            e.competition_type,
            e.metadata_json
        FROM events e
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if row is None:
        raise ValueError("Event not found: {}".format(event_code))
    return row


def _ensure_reservation_mode(event):
    metadata = _parse_json(event["metadata_json"])
    if event["competition_type"] != "timed_scoring":
        raise ValueError("仅限时赛支持预约。")
    if metadata.get("timed_mode") != "reservation":
        raise ValueError("该限时赛不是预约型。")
    duration_minutes = int(metadata.get("reservation_duration_minutes") or 0)
    if duration_minutes <= 0:
        raise ValueError("赛事未配置有效的预约时长。")
    return metadata, duration_minutes


def _lookup_binding(connection, bot_platform, bot_user_id):
    row = connection.execute(
        """
        SELECT player_id
        FROM bot_account_bindings
        WHERE bot_platform = ? AND bot_user_id = ? AND is_active = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (bot_platform, bot_user_id),
    ).fetchone()
    if row is None:
        raise ValueError("你还没有绑定账号。")
    return row["player_id"]


def _lookup_username(connection, player_id):
    row = connection.execute(
        """
        SELECT pa.account_key
        FROM player_accounts pa
        JOIN platforms p ON p.id = pa.platform_id
        WHERE pa.player_id = ? AND pa.is_primary = 1 AND p.code = '2048verse'
        ORDER BY pa.id DESC
        LIMIT 1
        """,
        (player_id,),
    ).fetchone()
    if row is None:
        raise ValueError("未找到绑定的 2048verse 用户名。")
    return row["account_key"]


def _event_window(event, duration_minutes):
    start_dt = parse_event_time(event["start_time"])
    end_dt = parse_event_time(event["end_time"])
    if start_dt is None or end_dt is None:
        raise ValueError("赛事时间未配置完整。")
    latest_start = end_dt - timedelta(minutes=duration_minutes)
    return start_dt, end_dt, latest_start


def _load_registration(connection, event_id, player_id):
    row = connection.execute(
        """
        SELECT status
        FROM registrations
        WHERE event_id = ? AND player_id = ?
        """,
        (event_id, player_id),
    ).fetchone()
    if row is None or row["status"] != "active":
        raise ValueError("你还没有报名该比赛。")


def _load_reservation(connection, event_id, player_id):
    return connection.execute(
        """
        SELECT *
        FROM timed_event_reservations
        WHERE event_id = ? AND player_id = ?
        """,
        (event_id, player_id),
    ).fetchone()


def _lookup_registered_player_by_username(connection, event_id, username):
    normalized = str(username or "").strip()
    if not normalized:
        raise ValueError("选手用户名不能为空。")
    row = connection.execute(
        """
        SELECT
            reg.player_id,
            p.display_name,
            pa.account_key AS username
        FROM registrations reg
        JOIN players p ON p.id = reg.player_id
        LEFT JOIN player_accounts pa
          ON pa.player_id = reg.player_id
         AND pa.is_primary = 1
         AND pa.platform_id = (
            SELECT id FROM platforms WHERE code = '2048verse' LIMIT 1
         )
        WHERE reg.event_id = ?
          AND reg.status = 'active'
          AND lower(COALESCE(pa.account_key, '')) = lower(?)
        LIMIT 1
        """,
        (event_id, normalized),
    ).fetchone()
    if row is None:
        raise ValueError("未找到该赛事中已报名的选手：{}".format(normalized))
    if not row["username"]:
        raise ValueError("该选手还没有可用的 2048verse 用户名。")
    return row


def _validate_reservation_window(event, duration_minutes, reserved_start_dt):
    start_dt, end_dt, latest_start = _event_window(event, duration_minutes)
    if reserved_start_dt < start_dt:
        raise ValueError("预约开始时间不能早于比赛开始。")
    if reserved_start_dt > latest_start:
        raise ValueError("预约开始过晚，无法在比赛结束前完成。")
    reserved_end_dt = reserved_start_dt + timedelta(minutes=duration_minutes)
    if reserved_end_dt > end_dt:
        raise ValueError("预约结束时间超出比赛结束时间。")
    return reserved_end_dt


def _upsert_reservation(
    connection,
    *,
    event,
    metadata,
    player_id,
    reserved_start_dt,
    late_confirmed=False,
    enforce_existing_start_guard=True,
    metadata_extra=None,
):
    duration_minutes = int(metadata.get("reservation_duration_minutes") or 0)
    reserved_end_dt = _validate_reservation_window(event, duration_minutes, reserved_start_dt)
    existing = _load_reservation(connection, event["id"], player_id)
    now_dt = now_local()
    if existing is not None and existing["status"] == "settled":
        raise ValueError("该选手的预约已结算，不能再修改。")
    if enforce_existing_start_guard and existing is not None and existing["status"] in {"reserved", "confirmed_late"}:
        old_start = parse_event_time(existing["reserved_start_time"])
        if old_start is not None and now_dt >= old_start:
            raise ValueError("当前已过原预约开始时间，不能修改预约。")
    if reserved_start_dt < now_dt and not late_confirmed:
        return {
            "requires_confirm": True,
            "event_code": event["event_code"],
            "reserved_start_time": reserved_start_dt.isoformat(timespec="minutes"),
            "reserved_end_time": reserved_end_dt.isoformat(timespec="minutes"),
        }
    status = "confirmed_late" if reserved_start_dt < now_dt else "reserved"
    payload = {"timed_mode": metadata.get("timed_mode")}
    if isinstance(metadata_extra, dict):
        payload.update({key: value for key, value in metadata_extra.items() if value not in (None, "")})
    connection.execute(
        """
        INSERT INTO timed_event_reservations (
            event_id, player_id, reserved_start_time, reserved_end_time,
            status, is_late_confirmed, metadata_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id, player_id) DO UPDATE SET
            reserved_start_time = excluded.reserved_start_time,
            reserved_end_time = excluded.reserved_end_time,
            status = excluded.status,
            is_late_confirmed = excluded.is_late_confirmed,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            event["id"],
            player_id,
            reserved_start_dt.isoformat(timespec="seconds"),
            reserved_end_dt.isoformat(timespec="seconds"),
            status,
            1 if late_confirmed else 0,
            json.dumps(payload, ensure_ascii=False),
            now_iso(),
            now_iso(),
        ),
    )
    return {
        "requires_confirm": False,
        "event_code": event["event_code"],
        "reserved_start_time": reserved_start_dt.isoformat(timespec="minutes"),
        "reserved_end_time": reserved_end_dt.isoformat(timespec="minutes"),
        "status": status,
    }


def reserve_for_player(
    connection,
    *,
    event_code,
    bot_platform,
    bot_user_id,
    reserved_start_dt,
    late_confirmed=False,
):
    event = _load_event_row(connection, event_code)
    metadata, duration_minutes = _ensure_reservation_mode(event)
    player_id = _lookup_binding(connection, bot_platform, bot_user_id)
    _load_registration(connection, event["id"], player_id)
    return _upsert_reservation(
        connection,
        event=event,
        metadata=metadata,
        player_id=player_id,
        reserved_start_dt=reserved_start_dt,
        late_confirmed=late_confirmed,
        enforce_existing_start_guard=True,
    )


def reserve_for_registered_player(
    connection,
    *,
    event_code,
    username,
    reserved_start_dt,
    late_confirmed=False,
    operator=None,
    note=None,
):
    event = _load_event_row(connection, event_code)
    metadata, _duration_minutes = _ensure_reservation_mode(event)
    player = _lookup_registered_player_by_username(connection, event["id"], username)
    result = _upsert_reservation(
        connection,
        event=event,
        metadata=metadata,
        player_id=player["player_id"],
        reserved_start_dt=reserved_start_dt,
        late_confirmed=late_confirmed,
        enforce_existing_start_guard=False,
        metadata_extra={
            "source": "organizer_hub_manual",
            "operator": operator,
            "note": note,
        },
    )
    result["username"] = player["username"]
    result["display_name"] = player["display_name"]
    return result


def get_my_reservation(connection, *, event_code, bot_platform, bot_user_id):
    event = _load_event_row(connection, event_code)
    _ensure_reservation_mode(event)
    player_id = _lookup_binding(connection, bot_platform, bot_user_id)
    row = _load_reservation(connection, event["id"], player_id)
    return row


def list_event_reservation_status(connection, *, event_code):
    event = _load_event_row(connection, event_code)
    _, duration_minutes = _ensure_reservation_mode(event)
    rows = connection.execute(
        """
        SELECT
            p.id AS player_id,
            p.display_name,
            pa.account_key AS username,
            r.reserved_start_time,
            r.reserved_end_time,
            r.status,
            r.is_late_confirmed,
            r.settled_at,
            r.settlement_payload_json,
            r.updated_at
        FROM registrations reg
        JOIN players p ON p.id = reg.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = p.id
           AND pa.is_primary = 1
           AND pa.platform_id = (
                SELECT id FROM platforms WHERE code = '2048verse' LIMIT 1
           )
        LEFT JOIN timed_event_reservations r
            ON r.event_id = reg.event_id
           AND r.player_id = reg.player_id
        WHERE reg.event_id = ?
          AND reg.status = 'active'
        ORDER BY
            CASE COALESCE(r.status, 'unreserved')
                WHEN 'reserved' THEN 1
                WHEN 'confirmed_late' THEN 2
                WHEN 'settled' THEN 3
                WHEN 'cancelled' THEN 4
                ELSE 5
            END ASC,
            COALESCE(r.reserved_start_time, '9999-12-31 23:59:59') ASC,
            p.display_name COLLATE NOCASE ASC
        """,
        (event["id"],),
    ).fetchall()
    output = []
    for row in rows:
        payload = _parse_json(row["settlement_payload_json"])
        output.append(
            {
                "player_id": row["player_id"],
                "display_name": row["display_name"],
                "username": row["username"],
                "reserved_start_time": row["reserved_start_time"],
                "reserved_end_time": row["reserved_end_time"],
                "status": row["status"] or "unreserved",
                "is_late_confirmed": int(row["is_late_confirmed"] or 0),
                "settled_at": row["settled_at"],
                "best_score": payload.get("best_score"),
                "updated_at": row["updated_at"],
            }
        )
    return {
        "event_code": event["event_code"],
        "event_name": event["event_name"],
        "event_start_time": event["start_time"],
        "event_end_time": event["end_time"],
        "reservation_duration_minutes": duration_minutes,
        "rows": output,
    }


def cancel_my_reservation(connection, *, event_code, bot_platform, bot_user_id):
    event = _load_event_row(connection, event_code)
    _ensure_reservation_mode(event)
    player_id = _lookup_binding(connection, bot_platform, bot_user_id)
    row = _load_reservation(connection, event["id"], player_id)
    if row is None:
        raise ValueError("你还没有预约。")
    start_dt = parse_event_time(row["reserved_start_time"])
    if start_dt is not None and now_local() >= start_dt:
        raise ValueError("已过预约开始时间，不能取消。")
    connection.execute(
        """
        UPDATE timed_event_reservations
        SET status = 'cancelled', updated_at = ?
        WHERE id = ?
        """,
        (now_iso(), row["id"]),
    )
    return {"event_code": event_code}


def get_my_reservation_score(connection, *, event_code, bot_platform, bot_user_id):
    event = _load_event_row(connection, event_code)
    _ensure_reservation_mode(event)
    player_id = _lookup_binding(connection, bot_platform, bot_user_id)
    row = _load_reservation(connection, event["id"], player_id)
    if row is None:
        raise ValueError("你在这场赛事没有预约记录。")
    payload = _parse_json(row["settlement_payload_json"])
    return {
        "event_code": event_code,
        "status": row["status"],
        "reserved_start_time": row["reserved_start_time"],
        "reserved_end_time": row["reserved_end_time"],
        "settled_at": row["settled_at"],
        "best_score": payload.get("best_score"),
    }


def _extract_game_score(game):
    for key in ("final_score", "score", "raw_score"):
        value = game.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _collect_board_values(payload, output):
    if isinstance(payload, int):
        output.append(payload)
        return
    if isinstance(payload, str):
        text = payload.strip()
        if text.isdigit():
            output.append(int(text))
        return
    if isinstance(payload, dict):
        for value in payload.values():
            _collect_board_values(value, output)
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_board_values(item, output)


def _tiles_sum(values):
    return sum(int(value) for value in values)


def _normalize_tiles(values):
    return tuple(sorted((int(value) for value in values if value), reverse=True))


def _first_full_board_peak(rules):
    full_board_rules = [rule for rule in rules if rule[2] == 1]
    if not full_board_rules:
        return 0
    return min(max(int(value) for value in rule[1]) for rule in full_board_rules)


def _collapse_tiles(values):
    counts = {}
    for value in values:
        if not value:
            continue
        counts[int(value)] = counts.get(int(value), 0) + 1
    if not counts:
        return ()

    max_value = max(counts)
    value = 2
    while value <= max_value:
        count = counts.get(value, 0)
        pairs, remainder = divmod(count, 2)
        if remainder:
            counts[value] = remainder
        else:
            counts.pop(value, None)
        if pairs:
            counts[value * 2] = counts.get(value * 2, 0) + pairs
            if value * 2 > max_value:
                max_value = value * 2
        value *= 2

    collapsed = []
    for tile, count in counts.items():
        collapsed.extend([tile] * count)
    collapsed.sort(reverse=True)
    return tuple(collapsed)


def _iter_subset_tiles(values, target_sum):
    filtered = [int(value) for value in values if value]
    subset_count = 1 << len(filtered)
    for mask in range(1, subset_count):
        subset = []
        subset_sum = 0
        for index, value in enumerate(filtered):
            if mask & (1 << index):
                subset_sum += value
                if subset_sum > target_sum:
                    break
                subset.append(value)
        if subset_sum == target_sum:
            yield tuple(subset)


def _is_fake_hit(values, required_tiles):
    target_tiles = _normalize_tiles(required_tiles)
    target_sum = _tiles_sum(target_tiles)
    found_exact = False
    found_fake = False

    for subset in _iter_subset_tiles(values, target_sum):
        normalized_subset = _normalize_tiles(subset)
        if normalized_subset == target_tiles:
            found_exact = True
            continue
        if _collapse_tiles(subset) == target_tiles:
            found_fake = True

    return found_fake and not found_exact


def _score_timed_game(game, variant_code):
    rules = TIMED_SCORING_RULES.get((variant_code or "").lower())
    if not rules:
        return 0, 0
    board = game.get("board")
    if board is None:
        return 0, 0
    values = []
    _collect_board_values(board, values)
    if not values:
        return 0, 0
    values.sort(reverse=True)

    total_sum = _tiles_sum(values)
    actual_max = max((int(value) for value in values), default=0)
    full_board_peak = _first_full_board_peak(rules)
    candidate_rule = None
    for points, required_tiles, full_board_level in rules:
        rule_peak = max(int(value) for value in required_tiles)
        if rule_peak > full_board_peak and actual_max < rule_peak:
            continue
        if total_sum >= _tiles_sum(required_tiles):
            candidate_rule = (points, required_tiles, full_board_level)
            break

    if candidate_rule is None:
        return 0, 0

    _, candidate_tiles, _ = candidate_rule
    effective_sum = total_sum
    if _is_fake_hit(values, candidate_tiles):
        effective_sum = _tiles_sum(candidate_tiles) - 1

    for points, required_tiles, full_board_level in rules:
        rule_peak = max(int(value) for value in required_tiles)
        if rule_peak > full_board_peak and actual_max < rule_peak:
            continue
        if effective_sum >= _tiles_sum(required_tiles):
            return points, full_board_level

    return 0, 0


def settle_due_reservations(connection, *, event_code=None):
    now_dt = now_local()
    query = """
        SELECT
            r.id,
            r.event_id,
            r.player_id,
            r.status,
            r.settlement_payload_json,
            r.reserved_start_time,
            r.reserved_end_time,
            e.event_code,
            e.platform_id,
            v.code AS variant_code
        FROM timed_event_reservations r
        JOIN events e ON e.id = r.event_id
        LEFT JOIN variants v ON v.id = e.variant_id
        WHERE r.reserved_end_time <= ?
    """
    params = [now_dt.isoformat(timespec="seconds")]
    if event_code:
        query += " AND e.event_code = ?"
        params.append(event_code)
    rows = connection.execute(query, tuple(params)).fetchall()
    settled_count = 0
    touched_events = set()
    for row in rows:
        status = (row["status"] or "").lower()
        payload = _parse_json(row["settlement_payload_json"])
        should_rebuild_settled = status == "settled" and (
            payload.get("settlement_version") != 2 or "scoring_game_count" not in payload
        )
        if status not in {"reserved", "confirmed_late"} and not should_rebuild_settled:
            continue
        username = _lookup_username(connection, row["player_id"])
        start_dt = parse_event_time(row["reserved_start_time"])
        end_dt = parse_event_time(row["reserved_end_time"])
        if start_dt is None or end_dt is None:
            continue
        if should_rebuild_settled:
            source_prefix = "{}_{}_reservation_{}".format(row["event_code"], username, row["id"])
            perf_rows = connection.execute(
                """
                SELECT id
                FROM performance_records
                WHERE platform_id = ?
                  AND source_record_id LIKE ?
                """,
                (row["platform_id"], source_prefix + "%"),
            ).fetchall()
            perf_ids = [item["id"] for item in perf_rows]
            if perf_ids:
                placeholders = ",".join("?" for _ in perf_ids)
                connection.execute(
                    "DELETE FROM event_attempt_records WHERE event_id = ? AND performance_record_id IN ({})".format(placeholders),
                    tuple([row["event_id"]] + perf_ids),
                )
                connection.execute(
                    "DELETE FROM performance_records WHERE id IN ({})".format(placeholders),
                    tuple(perf_ids),
                )
        games = fetch_recent_games(username, row["variant_code"] or "4x4", start_dt, max_pages=15)
        total_points = 0
        scoring_game_count = 0
        total_full_boards = 0
        best_single_points = None
        best_raw_score = None
        for game in games:
            game_start = get_game_start_time(game) or get_game_end_time(game)
            game_end = get_game_end_time(game) or game_start
            if game_start is None or game_end is None:
                continue
            if game_start < start_dt or game_end > end_dt:
                continue
            raw_score = _extract_game_score(game)
            if raw_score is None:
                continue
            points, full_board_level = _score_timed_game(game, row["variant_code"] or "4x4")
            if points <= 0:
                continue
            scoring_game_count += 1
            total_points += int(points)
            total_full_boards += int(full_board_level)
            if best_single_points is None or points > best_single_points:
                best_single_points = int(points)
            if best_raw_score is None or raw_score > best_raw_score:
                best_raw_score = int(raw_score)
            saved = add_manual_score(
                connection,
                row["event_code"],
                username=username,
                display_name=username,
                source_record_id="{}_{}_reservation_{}_game_{}".format(
                    row["event_code"],
                    username,
                    row["id"],
                    str(game.get("id") or "unknown"),
                ),
                started_at=game_start.replace(tzinfo=None).isoformat(timespec="seconds"),
                ended_at=game_end.replace(tzinfo=None).isoformat(timespec="seconds"),
                raw_score=raw_score,
                final_score=raw_score,
                competition_score=int(points),
                evidence_note="reservation_auto_settlement",
            )
            connection.execute(
                """
                UPDATE event_attempt_records
                SET derived_metric_json = json_set(
                    COALESCE(derived_metric_json, '{}'),
                    '$.full_board_level',
                    ?
                ),
                updated_at = ?
                WHERE event_id = ? AND performance_record_id = ?
                """,
                (
                    int(full_board_level),
                    now_iso(),
                    row["event_id"],
                    saved["performance_record_id"],
                ),
            )
        connection.execute(
            """
            UPDATE timed_event_reservations
            SET status = 'settled',
                settled_at = ?,
                settlement_payload_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                now_iso(),
                json.dumps(
                    {
                        "best_score": total_points,
                        "total_points": total_points,
                        "scoring_game_count": scoring_game_count,
                        "best_single_points": best_single_points,
                        "best_raw_score": best_raw_score,
                        "total_full_boards": total_full_boards,
                        "settlement_version": 2,
                    },
                    ensure_ascii=False,
                ),
                now_iso(),
                row["id"],
            ),
        )
        settled_count += 1
        touched_events.add(row["event_code"])
    for code in sorted(touched_events):
        settle_event(connection, code)
    return {"settled_count": settled_count, "event_count": len(touched_events)}
