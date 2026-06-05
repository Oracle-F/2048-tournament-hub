import json
import hashlib
from datetime import datetime

from services.ingest_service import ensure_player


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def lookup_platform(connection, platform_code):
    row = connection.execute("SELECT id, code, name FROM platforms WHERE code = ?", (platform_code,)).fetchone()
    if row is None:
        raise ValueError("Unknown platform: {}".format(platform_code))
    return row


def lookup_player_account(connection, platform_code, account_key):
    normalized_key = (account_key or "").strip()
    if not normalized_key:
        raise ValueError("Game account is empty")
    row = connection.execute(
        """
        SELECT
            pa.player_id,
            pa.account_key,
            COALESCE(pa.account_display_name, pa.account_name, p.display_name, pa.account_key) AS display_name
        FROM player_accounts pa
        JOIN players p ON p.id = pa.player_id
        JOIN platforms pf ON pf.id = pa.platform_id
        WHERE pf.code = ?
          AND (
            pa.account_key = ?
            OR pa.account_name = ?
            OR pa.account_display_name = ?
            OR LOWER(pa.account_key) = LOWER(?)
            OR LOWER(COALESCE(pa.account_name, '')) = LOWER(?)
            OR LOWER(COALESCE(pa.account_display_name, '')) = LOWER(?)
          )
        ORDER BY
          CASE
            WHEN pa.account_key = ? THEN 1
            WHEN LOWER(pa.account_key) = LOWER(?) THEN 2
            ELSE 9
          END,
          pa.is_primary DESC,
          pa.id ASC
        LIMIT 1
        """,
        (
            platform_code,
            normalized_key,
            normalized_key,
            normalized_key,
            normalized_key,
            normalized_key,
            normalized_key,
            normalized_key,
            normalized_key,
        ),
    ).fetchone()
    return row


def _normalize_bind_pin(bind_pin):
    normalized = (bind_pin or "").strip()
    if not normalized:
        raise ValueError("请设置4-5位数字绑定密码。")
    if not normalized.isdigit() or len(normalized) not in (4, 5):
        raise ValueError("绑定密码必须是4-5位数字。")
    return normalized


def _hash_bind_pin(*, bot_platform, game_platform, account_key, bind_pin):
    normalized_key = (account_key or "").strip().lower()
    material = "{}|{}|{}|{}".format(bot_platform, game_platform, normalized_key, bind_pin)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def bind_bot_account(
    connection,
    *,
    bot_platform,
    bot_user_id,
    game_platform,
    account_key,
    bind_pin=None,
    metadata=None,
):
    platform = lookup_platform(connection, game_platform)
    normalized_key = (account_key or "").strip()
    normalized_pin = _normalize_bind_pin(bind_pin)
    account = lookup_player_account(connection, game_platform, normalized_key)
    if account is None:
        player_id, _inserted = ensure_player(
            connection,
            display_name=normalized_key,
            username=normalized_key,
            platform_id=platform["id"],
        )
        account = {
            "player_id": player_id,
            "account_key": normalized_key,
            "display_name": normalized_key,
        }
    existing = connection.execute(
        """
        SELECT
            bot_platform,
            bot_user_id,
            game_platform,
            player_id,
            account_key,
            display_name
        FROM bot_account_bindings
        WHERE game_platform = ?
          AND is_active = 1
          AND LOWER(account_key) = LOWER(?)
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (
            platform["code"],
            account["account_key"],
        ),
    ).fetchone()
    if existing is not None and str(existing["bot_user_id"]) != str(bot_user_id):
        raise ValueError("该 verse 账号已绑定到其他 QQ，请先解绑或联系管理员。")
    metadata_payload = dict(metadata or {})
    metadata_payload["bind_pin_hash"] = _hash_bind_pin(
        bot_platform=bot_platform,
        game_platform=platform["code"],
        account_key=account["account_key"],
        bind_pin=normalized_pin,
    )
    metadata_payload["bind_pin_length"] = len(normalized_pin)
    updated_at = now_iso()
    connection.execute(
        """
        INSERT INTO bot_account_bindings (
            bot_platform, bot_user_id, game_platform, player_id, account_key,
            display_name, is_active, metadata_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(bot_platform, bot_user_id, game_platform) DO UPDATE SET
            player_id = excluded.player_id,
            account_key = excluded.account_key,
            display_name = excluded.display_name,
            is_active = 1,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            bot_platform,
            bot_user_id,
            game_platform,
            account["player_id"],
            account["account_key"],
            account["display_name"],
            json.dumps(metadata_payload, ensure_ascii=False),
            updated_at,
            updated_at,
        ),
    )
    return {
        "bot_platform": bot_platform,
        "bot_user_id": bot_user_id,
        "game_platform": platform["code"],
        "game_platform_name": platform["name"],
        "player_id": account["player_id"],
        "account_key": account["account_key"],
        "display_name": account["display_name"],
    }


def get_bot_binding(connection, *, bot_platform, bot_user_id, game_platform=None):
    query = """
        SELECT
            bab.id,
            bab.bot_platform,
            bab.bot_user_id,
            bab.game_platform,
            bab.player_id,
            bab.account_key,
            bab.display_name,
            bab.is_active,
            bab.metadata_json
        FROM bot_account_bindings bab
        WHERE bab.bot_platform = ?
          AND bab.bot_user_id = ?
          AND bab.is_active = 1
    """
    params = [bot_platform, bot_user_id]
    if game_platform:
        query += " AND bab.game_platform = ?"
        params.append(game_platform)
    query += " ORDER BY bab.id DESC LIMIT 1"
    row = connection.execute(query, tuple(params)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result["metadata_json"] or "{}")
    return result


def find_active_bot_binding(
    connection,
    *,
    bot_platform=None,
    bot_user_id=None,
    game_platform=None,
    account_key=None,
):
    query = """
        SELECT
            bab.id,
            bab.bot_platform,
            bab.bot_user_id,
            bab.game_platform,
            bab.player_id,
            bab.account_key,
            bab.display_name,
            bab.is_active,
            bab.metadata_json
        FROM bot_account_bindings bab
        WHERE bab.is_active = 1
    """
    params = []
    if bot_platform is not None:
        query += " AND bab.bot_platform = ?"
        params.append(bot_platform)
    if bot_user_id is not None:
        query += " AND bab.bot_user_id = ?"
        params.append(bot_user_id)
    if game_platform is not None:
        query += " AND bab.game_platform = ?"
        params.append(game_platform)
    if account_key is not None:
        query += " AND LOWER(bab.account_key) = LOWER(?)"
        params.append(account_key)
    query += " ORDER BY bab.updated_at DESC, bab.id DESC LIMIT 1"
    row = connection.execute(query, tuple(params)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result["metadata_json"] or "{}")
    return result


def deactivate_bot_binding(
    connection,
    *,
    binding_id=None,
    bot_platform=None,
    bot_user_id=None,
    game_platform=None,
    account_key=None,
):
    query = """
        UPDATE bot_account_bindings
        SET is_active = 0,
            updated_at = ?
        WHERE is_active = 1
    """
    params = [now_iso()]
    if binding_id is not None:
        query += " AND id = ?"
        params.append(binding_id)
    else:
        if bot_platform is not None:
            query += " AND bot_platform = ?"
            params.append(bot_platform)
        if bot_user_id is not None:
            query += " AND bot_user_id = ?"
            params.append(bot_user_id)
        if game_platform is not None:
            query += " AND game_platform = ?"
            params.append(game_platform)
        if account_key is not None:
            query += " AND LOWER(account_key) = LOWER(?)"
            params.append(account_key)
    connection.execute(query, tuple(params))
