from __future__ import annotations

import re
from datetime import datetime

from services.bot_binding_service import lookup_player_account


DASHBOARD_MAX_LINES = 10
DASHBOARD_MAX_NONSPACE_CHARS = 200
DASHBOARD_MAX_LINE_NONSPACE_CHARS = 50


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def normalize_dashboard_text(text):
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized


def count_nonspace_chars(text):
    return len(re.sub(r"\s+", "", str(text or "")))


def validate_dashboard_text(text):
    normalized = normalize_dashboard_text(text)
    if not normalized:
        raise ValueError("个人看板内容不能为空。")

    lines = normalized.split("\n")
    if len(lines) > DASHBOARD_MAX_LINES:
        raise ValueError("个人看板最多 {} 行。".format(DASHBOARD_MAX_LINES))

    total_nonspace_chars = count_nonspace_chars(normalized)
    if total_nonspace_chars > DASHBOARD_MAX_NONSPACE_CHARS:
        raise ValueError("个人看板总非空格字符数不能超过 {}。".format(DASHBOARD_MAX_NONSPACE_CHARS))

    for index, line in enumerate(lines, start=1):
        line_nonspace_chars = count_nonspace_chars(line)
        if line_nonspace_chars > DASHBOARD_MAX_LINE_NONSPACE_CHARS:
            raise ValueError("第 {} 行非空格字符数不能超过 {}。".format(index, DASHBOARD_MAX_LINE_NONSPACE_CHARS))

    return {
        "text": normalized,
        "line_count": len(lines),
        "nonspace_char_count": total_nonspace_chars,
    }


def sanitize_dashboard_text_for_display(text):
    normalized = normalize_dashboard_text(text)
    if not normalized:
        return ""
    return normalized.replace("@", "＠")


def set_player_dashboard(connection, *, game_platform, player_id, dashboard_text):
    payload = validate_dashboard_text(dashboard_text)
    updated_at = now_iso()
    connection.execute(
        """
        INSERT INTO bot_player_dashboards (
            game_platform,
            player_id,
            dashboard_text,
            line_count,
            nonspace_char_count,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_platform, player_id) DO UPDATE SET
            dashboard_text = excluded.dashboard_text,
            line_count = excluded.line_count,
            nonspace_char_count = excluded.nonspace_char_count,
            updated_at = excluded.updated_at
        """,
        (
            game_platform,
            player_id,
            payload["text"],
            payload["line_count"],
            payload["nonspace_char_count"],
            updated_at,
            updated_at,
        ),
    )
    return payload


def clear_player_dashboard(connection, *, game_platform, player_id):
    connection.execute(
        """
        DELETE FROM bot_player_dashboards
        WHERE game_platform = ?
          AND player_id = ?
        """,
        (game_platform, player_id),
    )


def get_player_dashboard(connection, *, game_platform, player_id):
    row = connection.execute(
        """
        SELECT
            game_platform,
            player_id,
            dashboard_text,
            line_count,
            nonspace_char_count,
            created_at,
            updated_at
        FROM bot_player_dashboards
        WHERE game_platform = ?
          AND player_id = ?
        LIMIT 1
        """,
        (game_platform, player_id),
    ).fetchone()
    return dict(row) if row is not None else None


def resolve_dashboard_target(connection, *, game_platform, username):
    normalized = (username or "").strip()
    if not normalized:
        return None

    row = connection.execute(
        """
        SELECT
            bab.player_id,
            bab.account_key,
            COALESCE(bab.display_name, p.display_name, bab.account_key) AS display_name
        FROM bot_account_bindings bab
        JOIN players p ON p.id = bab.player_id
        WHERE bab.game_platform = ?
          AND bab.is_active = 1
          AND (
            bab.account_key = ?
            OR bab.display_name = ?
            OR LOWER(bab.account_key) = LOWER(?)
            OR LOWER(COALESCE(bab.display_name, '')) = LOWER(?)
          )
        ORDER BY bab.updated_at DESC, bab.id DESC
        LIMIT 1
        """,
        (
            game_platform,
            normalized,
            normalized,
            normalized,
            normalized,
        ),
    ).fetchone()
    if row is not None:
        return dict(row)

    account = lookup_player_account(connection, game_platform, normalized)
    if account is None:
        return None
    return {
        "player_id": account["player_id"],
        "account_key": account["account_key"],
        "display_name": account["display_name"],
    }


def format_dashboard_reply(target_label, dashboard_row):
    label = str(target_label or "-").strip() or "-"
    if dashboard_row is None:
        return "玩家: {}\n暂未设置个人看板。".format(label)

    content = sanitize_dashboard_text_for_display(dashboard_row.get("dashboard_text"))
    if not content:
        return "玩家: {}\n暂未设置个人看板。".format(label)
    return "玩家: {}\n个人看板\n{}".format(label, content)
