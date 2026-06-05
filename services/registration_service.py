import json
from datetime import datetime

from services.ingest_service import ensure_player
from settings import LOCAL_TIMEZONE


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def lookup_event(connection, event_code):
    row = connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            e.competition_type,
            e.registration_close_time,
            e.start_time,
            e.end_time,
            e.metadata_json,
            e.platform_id,
            p.code AS platform_code
        FROM events e
        JOIN platforms p ON p.id = e.platform_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if row is None:
        raise ValueError("Event not found: {}".format(event_code))
    return row


def parse_event_time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace(" ", "T"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def ensure_registration_open(event, action_label):
    close_time = parse_event_time(event["registration_close_time"])
    start_time = parse_event_time(event["start_time"])
    deadline = close_time or start_time
    if deadline is None:
        return
    now = datetime.now(LOCAL_TIMEZONE)
    if now >= deadline:
        raise ValueError("{}已截止：已超过报名截止时间".format(action_label))


def list_registrations(connection, event_code):
    event = lookup_event(connection, event_code)
    rows = connection.execute(
        """
        SELECT
            r.id,
            r.status,
            r.registered_via,
            r.registered_at,
            p.id AS player_id,
            p.display_name,
            pa.account_key AS username
        FROM registrations r
        JOIN players p ON p.id = r.player_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = p.id
           AND pa.platform_id = ?
           AND pa.is_primary = 1
        WHERE r.event_id = ?
        ORDER BY LOWER(COALESCE(pa.account_key, p.display_name)) ASC, r.id ASC
        """,
        (event["platform_id"], event["id"]),
    ).fetchall()
    return {"event": event, "rows": rows}


def list_player_registrations(connection, player_id, *, platform_code=None, active_only=True):
    query = """
        SELECT
            e.event_code,
            e.event_name,
            e.competition_type,
            e.start_time,
            e.end_time,
            e.metadata_json,
            r.status,
            r.registered_at,
            r.registered_via,
            p.code AS platform_code
        FROM registrations r
        JOIN events e ON e.id = r.event_id
        JOIN platforms p ON p.id = e.platform_id
        WHERE r.player_id = ?
    """
    params = [player_id]
    if active_only:
        query += " AND r.status = 'active'"
    if platform_code:
        query += " AND p.code = ?"
        params.append(platform_code)
    query += " ORDER BY COALESCE(e.start_time, '') DESC, e.id DESC"
    return connection.execute(query, tuple(params)).fetchall()


def register_player(
    connection,
    event_code,
    username,
    display_name=None,
    registered_via="manual",
    metadata=None,
    enforce_deadline=True,
):
    event = lookup_event(connection, event_code)
    if enforce_deadline:
        ensure_registration_open(event, "报名")
    player_id, inserted = ensure_player(
        connection,
        display_name=display_name or username,
        username=username,
        platform_id=event["platform_id"],
    )
    metadata = metadata or {}
    connection.execute(
        """
        INSERT INTO registrations (
            event_id, player_id, registered_via, status, metadata_json, registered_at
        )
        VALUES (?, ?, ?, 'active', ?, ?)
        ON CONFLICT(event_id, player_id) DO UPDATE SET
            registered_via = excluded.registered_via,
            status = 'active',
            metadata_json = excluded.metadata_json
        """,
        (
            event["id"],
            player_id,
            registered_via,
            json.dumps(metadata, ensure_ascii=False),
            now_iso(),
        ),
    )
    return {
        "event_code": event["event_code"],
        "player_id": player_id,
        "username": username,
        "display_name": display_name or username,
        "new_player": inserted,
    }


def cancel_registration(connection, event_code, username, enforce_deadline=True):
    event = lookup_event(connection, event_code)
    if enforce_deadline:
        ensure_registration_open(event, "取消报名")
    row = connection.execute(
        """
        SELECT r.id
        FROM registrations r
        JOIN player_accounts pa ON pa.player_id = r.player_id AND pa.platform_id = ?
        WHERE r.event_id = ? AND pa.account_key = ?
        ORDER BY pa.is_primary DESC, pa.id ASC
        LIMIT 1
        """,
        (event["platform_id"], event["id"], username),
    ).fetchone()
    if row is None:
        raise ValueError("Registration not found: {}".format(username))
    connection.execute(
        """
        UPDATE registrations
        SET status = 'cancelled', metadata_json = ?, registered_at = registered_at
        WHERE id = ?
        """,
        (json.dumps({"cancelled_at": now_iso()}, ensure_ascii=False), row["id"]),
    )
    return {"event_code": event["event_code"], "username": username}


def is_registered(connection, event_code, username):
    event = lookup_event(connection, event_code)
    row = connection.execute(
        """
        SELECT 1
        FROM registrations r
        JOIN player_accounts pa ON pa.player_id = r.player_id AND pa.platform_id = ?
        WHERE r.event_id = ? AND pa.account_key = ? AND r.status = 'active'
        LIMIT 1
        """,
        (event["platform_id"], event["id"], username),
    ).fetchone()
    return row is not None


def update_registered_player_username(connection, event_code, old_username, new_username):
    event = lookup_event(connection, event_code)
    old_key = str(old_username or "").strip()
    new_key = str(new_username or "").strip()
    if not old_key:
        raise ValueError("原用户名不能为空。")
    if not new_key:
        raise ValueError("新用户名不能为空。")
    if old_key.lower() == new_key.lower():
        raise ValueError("新旧用户名相同，无需修改。")

    current = connection.execute(
        """
        SELECT
            r.player_id,
            p.display_name,
            pa.id AS account_id,
            pa.account_key,
            pa.account_name,
            pa.account_display_name,
            pa.is_primary
        FROM registrations r
        JOIN players p ON p.id = r.player_id
        JOIN player_accounts pa
          ON pa.player_id = r.player_id
         AND pa.platform_id = ?
        WHERE r.event_id = ?
          AND r.status = 'active'
          AND pa.account_key = ?
        ORDER BY pa.is_primary DESC, pa.id ASC
        LIMIT 1
        """,
        (event["platform_id"], event["id"], old_key),
    ).fetchone()
    if current is None:
        raise ValueError("未找到该赛事中用户名为 {} 的已报名选手。".format(old_key))

    target = connection.execute(
        """
        SELECT id, player_id, account_key, account_name, account_display_name, is_primary
        FROM player_accounts
        WHERE platform_id = ? AND lower(account_key) = lower(?)
        ORDER BY id ASC
        LIMIT 1
        """,
        (event["platform_id"], new_key),
    ).fetchone()

    if target is not None and target["player_id"] != current["player_id"]:
        raise ValueError("新用户名 {} 已属于另一名选手，不能直接修改。".format(new_key))

    updated_at = now_iso()
    if target is None:
        display_name = current["account_display_name"]
        account_name = current["account_name"]
        if not display_name or str(display_name).strip().lower() == old_key.lower():
            display_name = new_key
        if not account_name or str(account_name).strip().lower() == old_key.lower():
            account_name = new_key
        connection.execute(
            """
            UPDATE player_accounts
            SET account_key = ?, account_name = ?, account_display_name = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_key, account_name, display_name, updated_at, current["account_id"]),
        )
    else:
        connection.execute(
            """
            UPDATE player_accounts
            SET is_primary = CASE WHEN id = ? THEN 1 ELSE 0 END,
                updated_at = ?
            WHERE player_id = ? AND platform_id = ?
            """,
            (target["id"], updated_at, current["player_id"], event["platform_id"]),
        )
        display_name = target["account_display_name"]
        account_name = target["account_name"]
        if not display_name or str(display_name).strip().lower() == old_key.lower():
            display_name = new_key
        if not account_name or str(account_name).strip().lower() == old_key.lower():
            account_name = new_key
        connection.execute(
            """
            UPDATE player_accounts
            SET account_name = ?, account_display_name = ?, updated_at = ?
            WHERE id = ?
            """,
            (account_name, display_name, updated_at, target["id"]),
        )

    connection.execute(
        """
        UPDATE bot_account_bindings
        SET account_key = ?,
            display_name = CASE
                WHEN display_name IS NULL OR trim(display_name) = '' OR lower(display_name) = lower(?)
                THEN ?
                ELSE display_name
            END,
            updated_at = ?
        WHERE player_id = ? AND game_platform = ?
        """,
        (new_key, old_key, new_key, updated_at, current["player_id"], event["platform_code"]),
    )
    connection.execute(
        """
        UPDATE players
        SET display_name = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_key, updated_at, current["player_id"]),
    )
    return {
        "event_code": event["event_code"],
        "player_id": current["player_id"],
        "display_name": new_key,
        "old_username": old_key,
        "new_username": new_key,
    }
