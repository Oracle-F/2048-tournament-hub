import json
from datetime import datetime

from settings import BOT_ADMINS_PATH


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _load_admin_config():
    if not BOT_ADMINS_PATH.exists():
        return {}
    try:
        payload = json.loads(BOT_ADMINS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def list_bot_admins(bot_platform):
    payload = _load_admin_config()
    values = payload.get(bot_platform) or []
    return {str(item) for item in values if str(item).strip()}


def is_bot_admin(bot_platform, bot_user_id):
    return str(bot_user_id) in list_bot_admins(bot_platform)


def write_audit_log(
    connection,
    *,
    actor_type,
    actor_id,
    action_type,
    target_table,
    target_id,
    reason=None,
    before=None,
    after=None,
):
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_type, actor_id, action_type, target_table, target_id,
            reason, before_json, after_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_type,
            actor_id,
            action_type,
            target_table,
            int(target_id),
            reason,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            now_iso(),
        ),
    )
