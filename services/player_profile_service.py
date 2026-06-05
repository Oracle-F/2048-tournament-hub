import json
from datetime import datetime
from pathlib import Path

from settings import EXPORTS_DIR


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def find_players(connection, query, limit=20):
    keyword = (query or "").strip()
    if not keyword:
        return []
    like = "%{}%".format(keyword)
    return connection.execute(
        """
        SELECT
            p.id,
            p.display_name,
            p.status,
            GROUP_CONCAT(DISTINCT pa.account_name) AS account_names,
            GROUP_CONCAT(DISTINCT pa.account_key) AS account_keys
        FROM players p
        LEFT JOIN player_accounts pa ON pa.player_id = p.id
        WHERE p.display_name = ?
           OR pa.account_name = ?
           OR pa.account_key = ?
           OR p.display_name LIKE ?
           OR pa.account_name LIKE ?
           OR pa.account_key LIKE ?
        GROUP BY p.id
        ORDER BY
            CASE
                WHEN p.display_name = ? THEN 0
                WHEN MAX(pa.account_name = ?) THEN 1
                WHEN MAX(pa.account_key = ?) THEN 2
                ELSE 3
            END,
            LOWER(p.display_name) ASC
        LIMIT ?
        """,
        (keyword, keyword, keyword, like, like, like, keyword, keyword, keyword, limit),
    ).fetchall()


def load_player(connection, player_id):
    row = connection.execute(
        """
        SELECT id, display_name, status, notes, created_at, updated_at
        FROM players
        WHERE id = ?
        """,
        (player_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Player not found: {}".format(player_id))
    return row


def load_accounts(connection, player_id):
    return connection.execute(
        """
        SELECT
            pa.account_key,
            pa.account_name,
            pa.account_display_name,
            pa.is_primary,
            p.code AS platform_code,
            p.name AS platform_name
        FROM player_accounts pa
        JOIN platforms p ON p.id = pa.platform_id
        WHERE pa.player_id = ?
        ORDER BY pa.is_primary DESC, p.code ASC, pa.id ASC
        """,
        (player_id,),
    ).fetchall()


def load_rating_summary(connection, player_id):
    return connection.execute(
        """
        SELECT
            rb.code AS bucket_code,
            rb.name AS bucket_name,
            rf.code AS family_code,
            rf.name AS family_name,
            pr.rating_value,
            pr.rating_deviation,
            pr.event_count,
            pr.best_rating,
            pr.last_updated_at,
            e.event_code AS last_event_code,
            (
                SELECT COUNT(*) + 1
                FROM player_ratings better
                WHERE better.rating_bucket_id = pr.rating_bucket_id
                  AND (
                      better.rating_value > pr.rating_value
                      OR (
                          better.rating_value = pr.rating_value
                          AND better.rating_deviation < pr.rating_deviation
                      )
                  )
            ) AS bucket_rank
        FROM player_ratings pr
        JOIN rating_buckets rb ON rb.id = pr.rating_bucket_id
        JOIN rating_families rf ON rf.id = rb.family_id
        LEFT JOIN events e ON e.id = pr.last_event_id
        WHERE pr.player_id = ?
        ORDER BY rf.sort_order ASC, rb.code ASC
        """,
        (player_id,),
    ).fetchall()


def load_best_performances(connection, player_id):
    return connection.execute(
        """
        SELECT *
        FROM (
            SELECT
                rb.code AS bucket_code,
                rb.name AS bucket_name,
                er.primary_metric_type,
                er.primary_metric_value,
                er.secondary_metric_type,
                er.secondary_metric_value,
                er.best_single_score,
                er.rank_value,
                e.event_code,
                e.event_name,
                e.start_time,
                e.end_time,
                ROW_NUMBER() OVER (
                    PARTITION BY rb.id
                    ORDER BY er.primary_metric_value DESC, er.best_single_score DESC, COALESCE(e.end_time, e.start_time, '') DESC
                ) AS row_order
            FROM event_results er
            JOIN events e ON e.id = er.event_id
            JOIN rating_buckets rb ON rb.id = e.rating_bucket_id
            WHERE er.player_id = ?
              AND e.is_official = 1
              AND er.primary_metric_value IS NOT NULL
        )
        WHERE row_order = 1
        ORDER BY bucket_code ASC
        """,
        (player_id,),
    ).fetchall()


def load_recent_results(connection, player_id, limit=10):
    return connection.execute(
        """
        SELECT
            er.rank_value,
            er.primary_metric_type,
            er.primary_metric_value,
            er.secondary_metric_type,
            er.secondary_metric_value,
            er.best_single_score,
            e.event_code,
            e.event_name,
            e.is_official,
            e.is_rated,
            e.start_time,
            e.end_time,
            rb.code AS bucket_code,
            rb.name AS bucket_name
        FROM event_results er
        JOIN events e ON e.id = er.event_id
        LEFT JOIN rating_buckets rb ON rb.id = e.rating_bucket_id
        WHERE er.player_id = ?
        ORDER BY COALESCE(e.end_time, e.start_time, er.calculated_at, '') DESC, e.id DESC
        LIMIT ?
        """,
        (player_id, limit),
    ).fetchall()


def load_rating_history(connection, player_id, limit=20):
    return connection.execute(
        """
        SELECT
            rb.code AS bucket_code,
            rb.name AS bucket_name,
            e.event_code,
            e.event_name,
            rh.old_rating,
            rh.new_rating,
            rh.delta_rating,
            rh.old_deviation,
            rh.new_deviation,
            rh.placement,
            rh.field_size,
            rh.created_at
        FROM rating_history rh
        JOIN rating_buckets rb ON rb.id = rh.rating_bucket_id
        JOIN events e ON e.id = rh.event_id
        WHERE rh.player_id = ?
        ORDER BY rh.created_at DESC, rh.id DESC
        LIMIT ?
        """,
        (player_id, limit),
    ).fetchall()


def build_player_profile(connection, player_id):
    player = load_player(connection, player_id)
    return {
        "player": player,
        "accounts": load_accounts(connection, player_id),
        "ratings": load_rating_summary(connection, player_id),
        "best_performances": load_best_performances(connection, player_id),
        "recent_results": load_recent_results(connection, player_id),
        "rating_history": load_rating_history(connection, player_id),
        "exported_at": now_iso(),
    }


def build_player_profile_by_query(connection, query):
    rows = find_players(connection, query, limit=2)
    if not rows:
        raise ValueError("Player not found: {}".format(query))
    if len(rows) > 1:
        exact_rows = [
            row
            for row in rows
            if row["display_name"] == query
            or query in (row["account_names"] or "").split(",")
            or query in (row["account_keys"] or "").split(",")
        ]
        if len(exact_rows) == 1:
            return build_player_profile(connection, exact_rows[0]["id"])
        raise ValueError("Multiple players matched: {}".format(query))
    return build_player_profile(connection, rows[0]["id"])


def row_to_dict(row):
    return dict(row) if row is not None else None


def profile_to_payload(profile):
    return {
        "snapshot_type": "player_profile",
        "exported_at": profile["exported_at"],
        "player": row_to_dict(profile["player"]),
        "accounts": [row_to_dict(row) for row in profile["accounts"]],
        "ratings": [row_to_dict(row) for row in profile["ratings"]],
        "best_performances": [row_to_dict(row) for row in profile["best_performances"]],
        "recent_results": [row_to_dict(row) for row in profile["recent_results"]],
        "rating_history": [row_to_dict(row) for row in profile["rating_history"]],
    }


def format_player_profile(profile):
    player = profile["player"]
    lines = [
        "选手档案",
        "选手: {} | id={} | 状态 {}".format(player["display_name"], player["id"], player["status"]),
        "导出时间: {}".format(profile["exported_at"]),
        "",
        "账号",
    ]
    if profile["accounts"]:
        for row in profile["accounts"]:
            marker = "主账号" if row["is_primary"] else "备用"
            lines.append(
                "- {} | {} | {} ({})".format(
                    row["platform_code"],
                    row["account_name"],
                    row["account_key"],
                    marker,
                )
            )
    else:
        lines.append("- 暂无账号")

    lines.extend(["", "分项 rating"])
    if profile["ratings"]:
        for row in profile["ratings"]:
            lines.append(
                "- {} | 第 {} | rating {:.1f} | RD {:.1f} | 参赛 {} | 最高 {:.1f}".format(
                    row["bucket_code"],
                    row["bucket_rank"],
                    row["rating_value"],
                    row["rating_deviation"],
                    row["event_count"],
                    row["best_rating"] if row["best_rating"] is not None else row["rating_value"],
                )
            )
    else:
        lines.append("- 暂无 rating")

    lines.extend(["", "分项最好成绩"])
    if profile["best_performances"]:
        for row in profile["best_performances"]:
            lines.append(
                "- {} | {} {} | 名次 {} | {}".format(
                    row["bucket_code"],
                    row["primary_metric_type"],
                    row["primary_metric_value"],
                    row["rank_value"],
                    row["event_code"],
                )
            )
    else:
        lines.append("- 暂无正式赛成绩")

    lines.extend(["", "最近比赛"])
    if profile["recent_results"]:
        for row in profile["recent_results"]:
            tags = []
            if row["is_official"]:
                tags.append("正式")
            if row["is_rated"]:
                tags.append("rating")
            lines.append(
                "- {} | {} | 第 {} | {} {} | {}".format(
                    row["event_code"],
                    row["bucket_code"] or "-",
                    row["rank_value"],
                    row["primary_metric_type"],
                    row["primary_metric_value"],
                    "/".join(tags) or "-",
                )
            )
    else:
        lines.append("- 暂无比赛结果")

    lines.extend(["", "最近 rating 变化"])
    if profile["rating_history"]:
        for row in profile["rating_history"]:
            lines.append(
                "- {} | {} | {:.1f} -> {:.1f} ({:+.1f}) | 第 {}/{}".format(
                    row["bucket_code"],
                    row["event_code"],
                    row["old_rating"],
                    row["new_rating"],
                    row["delta_rating"],
                    row["placement"],
                    row["field_size"],
                )
            )
    else:
        lines.append("- 暂无 rating 变化")

    return "\n".join(lines) + "\n"


def safe_file_name(value):
    cleaned = []
    for char in value:
        if char.isalnum() or char in {"-", "_"}:
            cleaned.append(char)
        elif char in {" ", "."}:
            cleaned.append("_")
    return "".join(cleaned).strip("_") or "player"


def export_player_profile(connection, player_id, output_root=None):
    profile = build_player_profile(connection, player_id)
    player = profile["player"]
    output_dir = Path(output_root) if output_root else EXPORTS_DIR / "选手档案" / safe_file_name(player["display_name"])
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_path = output_dir / "选手档案.txt"
    json_path = output_dir / "选手档案.json"
    txt_path.write_text(format_player_profile(profile), encoding="utf-8")
    json_path.write_text(json.dumps(profile_to_payload(profile), ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "output_dir": output_dir,
        "txt_path": txt_path,
        "json_path": json_path,
        "player_id": player["id"],
        "display_name": player["display_name"],
    }
