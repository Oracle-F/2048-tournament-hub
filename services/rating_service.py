import json
import math
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from settings import EXPORTS_DIR


INITIAL_RATING = 500.0
INITIAL_DEVIATION = 300.0
INITIAL_VOLATILITY = 0.06
MIN_RATING_DEVIATION = 60.0
MAX_RATING_DEVIATION = 350.0
GLICKO2_TAU = 0.5
RD_ACTIVE_CAP = 220.0
RD_INACTIVITY_GRACE_DAYS = 14
RD_INACTIVITY_PER_DAY = 4.0
GLICKO2_SCALE = 173.7178


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def fit_text(draw, text, font, max_width):
    text = str(text or "-")
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    output = text
    while output and draw.textlength(output + ellipsis, font=font) > max_width:
        output = output[:-1]
    return (output + ellipsis) if output else ellipsis


def load_rating_bucket(connection, bucket_code):
    row = connection.execute(
        """
        SELECT
            rb.id,
            rb.code,
            rb.name,
            rb.competition_type,
            rb.target_value,
            rf.code AS family_code,
            rf.name AS family_name
        FROM rating_buckets rb
        JOIN rating_families rf ON rf.id = rb.family_id
        WHERE rb.code = ?
        """,
        (bucket_code,),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown rating bucket: {}".format(bucket_code))
    return row


def list_rating_buckets(connection):
    rows = connection.execute(
        """
        SELECT
            rb.code,
            rb.name,
            rb.competition_type,
            rb.target_value,
            rb.is_active,
            rb.metadata_json,
            rf.code AS family_code,
            rf.name AS family_name,
            COUNT(DISTINCT e.id) AS rated_event_count,
            COUNT(DISTINCT pr.player_id) AS rated_player_count
        FROM rating_buckets rb
        JOIN rating_families rf ON rf.id = rb.family_id
        LEFT JOIN events e
            ON e.rating_bucket_id = rb.id
           AND e.is_rated = 1
           AND e.is_official = 1
        LEFT JOIN player_ratings pr ON pr.rating_bucket_id = rb.id
        GROUP BY rb.id
        ORDER BY rf.sort_order ASC, rb.code ASC
        """
    ).fetchall()
    output = []
    for row in rows:
        metadata = {}
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, ValueError):
                metadata = {}
        # Trial bucket is for staged migration; hide from normal hub list.
        if metadata.get("trial_bucket"):
            continue
        output.append(row)
    return output


def load_rated_events(connection, bucket_id=None):
    params = []
    bucket_filter = ""
    if bucket_id is not None:
        bucket_filter = "AND e.rating_bucket_id = ?"
        params.append(bucket_id)
    return connection.execute(
        """
        SELECT
            e.id,
            e.event_code,
            e.event_name,
            e.rating_bucket_id,
            e.start_time,
            e.end_time,
            e.created_at,
            e.status
        FROM events e
        WHERE e.is_rated = 1
          AND e.is_official = 1
          AND e.rating_bucket_id IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM event_results er
              WHERE er.event_id = e.id
                AND er.rank_value IS NOT NULL
                AND er.primary_metric_value IS NOT NULL
          )
          {bucket_filter}
        ORDER BY COALESCE(e.end_time, e.start_time, e.created_at, '') ASC, e.id ASC
        """.format(bucket_filter=bucket_filter),
        params,
    ).fetchall()


def load_event_rating_rows(connection, event_id):
    return connection.execute(
        """
        SELECT
            er.player_id,
            er.rank_value,
            er.primary_metric_value,
            er.secondary_metric_value,
            er.best_single_score,
            p.display_name
        FROM event_results er
        JOIN players p ON p.id = er.player_id
        WHERE er.event_id = ?
          AND er.rank_value IS NOT NULL
          AND er.primary_metric_value IS NOT NULL
        ORDER BY er.rank_value ASC, p.display_name ASC
        """,
        (event_id,),
    ).fetchall()


def actual_score(rank_a, rank_b):
    if rank_a < rank_b:
        return 1.0
    if rank_a > rank_b:
        return 0.0
    return 0.5


def _to_mu(rating):
    return (rating - INITIAL_RATING) / GLICKO2_SCALE


def _to_phi(deviation):
    return deviation / GLICKO2_SCALE


def _to_rating(mu):
    return mu * GLICKO2_SCALE + INITIAL_RATING


def _to_deviation(phi):
    return phi * GLICKO2_SCALE


def _g(phi):
    return 1.0 / math.sqrt(1.0 + (3.0 * phi * phi) / (math.pi * math.pi))


def _e(mu, mu_j, phi_j):
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _parse_time(value):
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _event_time(event):
    end_time = event["end_time"] if "end_time" in event.keys() else None
    start_time = event["start_time"] if "start_time" in event.keys() else None
    created_at = event["created_at"] if "created_at" in event.keys() else None
    return _parse_time(end_time) or _parse_time(start_time) or _parse_time(created_at) or datetime.now()


def _inactivity_days(last_active_at, event_at):
    if last_active_at is None:
        return 0
    delta = event_at - last_active_at
    days = max(0, int(delta.total_seconds() // 86400))
    return max(0, days - RD_INACTIVITY_GRACE_DAYS)


def _inflate_rd_for_inactivity(deviation, days):
    if days <= 0:
        return deviation
    inflated = math.sqrt(deviation * deviation + (RD_INACTIVITY_PER_DAY * days) ** 2)
    return min(RD_ACTIVE_CAP, max(MIN_RATING_DEVIATION, inflated))


def _update_volatility(phi, sigma, delta, v, tau):
    a = math.log(sigma * sigma)

    def f(x):
        ex = math.exp(x)
        num = ex * (delta * delta - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return num / den - (x - a) / (tau * tau)

    if delta * delta > phi * phi + v:
        b = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        b = a - k * tau

    fa = f(a)
    fb = f(b)
    eps = 1e-6
    while abs(b - a) > eps:
        c = a + (a - b) * fa / (fb - fa)
        fc = f(c)
        if fc * fb < 0:
            a = b
            fa = fb
        else:
            fa = fa / 2.0
        b = c
        fb = fc
    return math.exp(a / 2.0)


def reset_bucket_ratings(connection, bucket_id):
    connection.execute("DELETE FROM rating_history WHERE rating_bucket_id = ?", (bucket_id,))
    connection.execute("DELETE FROM player_ratings WHERE rating_bucket_id = ?", (bucket_id,))


def reset_all_ratings(connection):
    connection.execute("DELETE FROM rating_history")
    connection.execute("DELETE FROM player_ratings")


def apply_event_to_memory(connection, event, ratings):
    rows = load_event_rating_rows(connection, event["id"])
    if len(rows) < 2:
        return {
            "event_code": event["event_code"],
            "player_count": len(rows),
            "history_count": 0,
            "skipped": True,
        }

    for row in rows:
        ratings.setdefault(
            row["player_id"],
            {
                "rating": INITIAL_RATING,
                "deviation": INITIAL_DEVIATION,
                "volatility": INITIAL_VOLATILITY,
                "event_count": 0,
                "best_rating": INITIAL_RATING,
                "last_active_at": None,
            },
        )

    event_at = _event_time(event)
    created_at = now_iso()
    field_size = len(rows)
    history_count = 0
    rows_by_player = {row["player_id"]: row for row in rows}
    pre_states = {}
    for row in rows:
        player_id = row["player_id"]
        state = ratings[player_id]
        idle_days = _inactivity_days(state.get("last_active_at"), event_at)
        inflated_rd = _inflate_rd_for_inactivity(state["deviation"], idle_days)
        pre_states[player_id] = {
            "rating": state["rating"],
            "deviation": inflated_rd,
            "volatility": state.get("volatility", INITIAL_VOLATILITY),
            "idle_days": idle_days,
        }

    for row in rows:
        player_id = row["player_id"]
        state = ratings[player_id]
        old_rating = pre_states[player_id]["rating"]
        old_deviation = pre_states[player_id]["deviation"]
        old_volatility = pre_states[player_id]["volatility"]
        idle_days = pre_states[player_id]["idle_days"]

        mu = _to_mu(old_rating)
        phi = _to_phi(old_deviation)
        sigma = old_volatility
        opponents = [rows_by_player[item["player_id"]] for item in rows if item["player_id"] != player_id]
        if not opponents:
            continue

        v_inv = 0.0
        delta_sum = 0.0
        ties = 0
        for opp in opponents:
            opp_state = pre_states[opp["player_id"]]
            mu_j = _to_mu(opp_state["rating"])
            phi_j = _to_phi(opp_state["deviation"])
            score = actual_score(row["rank_value"], opp["rank_value"])
            if score == 0.5:
                ties += 1
            g_val = _g(phi_j)
            e_val = _e(mu, mu_j, phi_j)
            v_inv += (g_val * g_val) * e_val * (1.0 - e_val)
            delta_sum += g_val * (score - e_val)

        if v_inv <= 0:
            continue
        v = 1.0 / v_inv
        delta = v * delta_sum
        new_sigma = _update_volatility(phi, sigma, delta, v, GLICKO2_TAU)
        phi_star = math.sqrt(phi * phi + new_sigma * new_sigma)
        new_phi = 1.0 / math.sqrt((1.0 / (phi_star * phi_star)) + (1.0 / v))
        new_mu = mu + (new_phi * new_phi) * delta_sum

        new_rating = _to_rating(new_mu)
        new_deviation = max(MIN_RATING_DEVIATION, min(MAX_RATING_DEVIATION, _to_deviation(new_phi)))
        state["rating"] = new_rating
        state["deviation"] = new_deviation
        state["volatility"] = new_sigma
        state["event_count"] += 1
        state["best_rating"] = max(state["best_rating"], new_rating)
        state["last_active_at"] = event_at
        delta_rating = new_rating - old_rating

        details = {
            "algorithm": "glicko2_v1",
            "initial_rating": INITIAL_RATING,
            "initial_rd": INITIAL_DEVIATION,
            "initial_volatility": INITIAL_VOLATILITY,
            "tau": GLICKO2_TAU,
            "rd_min": MIN_RATING_DEVIATION,
            "rd_max": MAX_RATING_DEVIATION,
            "rd_active_cap": RD_ACTIVE_CAP,
            "rd_inactivity_grace_days": RD_INACTIVITY_GRACE_DAYS,
            "rd_inactivity_per_day": RD_INACTIVITY_PER_DAY,
            "inactivity_days": idle_days,
            "pre_rating": old_rating,
            "pre_rd": old_deviation,
            "pre_volatility": old_volatility,
            "post_rating": new_rating,
            "post_rd": new_deviation,
            "post_volatility": new_sigma,
            "rank": row["rank_value"],
            "field_size": field_size,
            "opponent_count": len(opponents),
            "tie_count": ties,
            "primary_metric_value": row["primary_metric_value"],
            "secondary_metric_value": row["secondary_metric_value"],
            "best_single_score": row["best_single_score"],
        }
        connection.execute(
            """
            INSERT INTO rating_history (
                player_id, rating_bucket_id, event_id,
                old_rating, new_rating, delta_rating,
                old_deviation, new_deviation,
                placement, field_size, weight, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                player_id,
                event["rating_bucket_id"],
                event["id"],
                old_rating,
                new_rating,
                delta_rating,
                old_deviation,
                new_deviation,
                row["rank_value"],
                field_size,
                1.0,
                json.dumps(details, ensure_ascii=False),
                created_at,
            ),
        )
        history_count += 1
    return {
        "event_code": event["event_code"],
        "player_count": field_size,
        "history_count": history_count,
        "skipped": False,
    }


def save_rating_states(connection, bucket_id, ratings, last_event_id_by_player):
    updated_at = now_iso()
    for player_id, state in ratings.items():
        connection.execute(
            """
            INSERT INTO player_ratings (
                player_id, rating_bucket_id, rating_value, rating_deviation,
                rating_volatility, event_count, best_rating, last_event_id, last_updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id, rating_bucket_id) DO UPDATE SET
                rating_value = excluded.rating_value,
                rating_deviation = excluded.rating_deviation,
                rating_volatility = excluded.rating_volatility,
                event_count = excluded.event_count,
                best_rating = excluded.best_rating,
                last_event_id = excluded.last_event_id,
                last_updated_at = excluded.last_updated_at
            """,
            (
                player_id,
                bucket_id,
                state["rating"],
                state["deviation"],
                state.get("volatility", INITIAL_VOLATILITY),
                state["event_count"],
                state["best_rating"],
                last_event_id_by_player.get(player_id),
                updated_at,
            ),
        )


def recalculate_ratings(connection, bucket_code=None):
    if bucket_code:
        bucket = load_rating_bucket(connection, bucket_code)
        events = load_rated_events(connection, bucket["id"])
        reset_bucket_ratings(connection, bucket["id"])
        bucket_ids = [bucket["id"]]
    else:
        bucket = None
        events = load_rated_events(connection)
        reset_all_ratings(connection)
        bucket_ids = sorted({event["rating_bucket_id"] for event in events})

    ratings_by_bucket = {bucket_id: {} for bucket_id in bucket_ids}
    last_event_id_by_bucket_player = {bucket_id: {} for bucket_id in bucket_ids}
    applied = []
    skipped = []
    history_count = 0

    for event in events:
        bucket_id = event["rating_bucket_id"]
        ratings = ratings_by_bucket.setdefault(bucket_id, {})
        result = apply_event_to_memory(connection, event, ratings)
        if result["skipped"]:
            skipped.append(result)
            continue
        applied.append(result)
        history_count += result["history_count"]
        for row in load_event_rating_rows(connection, event["id"]):
            last_event_id_by_bucket_player.setdefault(bucket_id, {})[row["player_id"]] = event["id"]

    for bucket_id, ratings in ratings_by_bucket.items():
        save_rating_states(
            connection,
            bucket_id,
            ratings,
            last_event_id_by_bucket_player.get(bucket_id, {}),
        )

    return {
        "bucket_code": bucket_code,
        "event_count": len(applied),
        "skipped_event_count": len(skipped),
        "history_count": history_count,
        "player_count": sum(len(ratings) for ratings in ratings_by_bucket.values()),
        "applied_events": applied,
        "skipped_events": skipped,
    }


def recalculate_event_bucket_ratings(connection, event_code):
    row = connection.execute(
        """
        SELECT rb.code AS bucket_code
        FROM events e
        LEFT JOIN rating_buckets rb ON rb.id = e.rating_bucket_id
        WHERE e.event_code = ?
        """,
        (event_code,),
    ).fetchone()
    if row is None:
        raise ValueError("Event not found: {}".format(event_code))
    if not row["bucket_code"]:
        raise ValueError("Event has no rating bucket: {}".format(event_code))
    return recalculate_ratings(connection, row["bucket_code"])


def list_rating_leaderboard(connection, bucket_code, limit=50):
    bucket = load_rating_bucket(connection, bucket_code)
    rows = connection.execute(
        """
        SELECT
            pr.rating_value,
            pr.rating_deviation,
            pr.event_count,
            pr.best_rating,
            pr.last_updated_at,
            p.display_name,
            pa.account_name,
            e.event_code AS last_event_code
        FROM player_ratings pr
        JOIN players p ON p.id = pr.player_id
        LEFT JOIN rating_buckets rb ON rb.id = pr.rating_bucket_id
        LEFT JOIN player_accounts pa
            ON pa.player_id = pr.player_id
           AND (rb.platform_id IS NULL OR pa.platform_id = rb.platform_id)
           AND pa.is_primary = 1
        LEFT JOIN events e ON e.id = pr.last_event_id
        WHERE pr.rating_bucket_id = ?
        ORDER BY pr.rating_value DESC, pr.rating_deviation ASC, p.display_name ASC
        LIMIT ?
        """,
        (bucket["id"], limit),
    ).fetchall()
    return {"bucket": bucket, "rows": rows}


def list_performance_leaderboard(connection, bucket_code, limit=50):
    bucket = load_rating_bucket(connection, bucket_code)
    order_direction = "ASC" if bucket["competition_type"] == "speedrun" else "DESC"
    missing_value = "999999999999" if order_direction == "ASC" else "-999999999999"
    rows = connection.execute(
        """
        SELECT *
        FROM (
            SELECT
                er.rank_value AS event_rank,
                er.primary_metric_type,
                er.primary_metric_value,
                er.secondary_metric_type,
                er.secondary_metric_value,
                er.best_single_score,
                p.display_name,
                pa.account_name,
                e.event_code,
                e.event_name,
                e.start_time,
                e.end_time,
                ROW_NUMBER() OVER (
                    PARTITION BY er.player_id
                    ORDER BY COALESCE(er.primary_metric_value, {missing_value}) {order_direction},
                             COALESCE(er.best_single_score, {missing_value}) {order_direction},
                             COALESCE(e.end_time, e.start_time, '') DESC
                ) AS player_best_order
            FROM event_results er
            JOIN events e ON e.id = er.event_id
            JOIN players p ON p.id = er.player_id
            LEFT JOIN rating_buckets rb ON rb.id = e.rating_bucket_id
            LEFT JOIN player_accounts pa
                ON pa.player_id = er.player_id
               AND (rb.platform_id IS NULL OR pa.platform_id = rb.platform_id)
               AND pa.is_primary = 1
            WHERE e.rating_bucket_id = ?
              AND e.is_official = 1
              AND er.primary_metric_value IS NOT NULL
        )
        WHERE player_best_order = 1
        ORDER BY COALESCE(primary_metric_value, {missing_value}) {order_direction},
                 COALESCE(best_single_score, {missing_value}) {order_direction},
                 display_name ASC
        LIMIT ?
        """.format(order_direction=order_direction, missing_value=missing_value),
        (bucket["id"], limit),
    ).fetchall()
    return {"bucket": bucket, "rows": rows}


def write_rating_leaderboard_image(bucket, rows, output_dir, exported_at):
    display_rows = rows[:30]
    width = 1220
    header_height = 190
    row_height = 46
    footer_height = 64
    height = header_height + row_height * (len(display_rows) + 1) + footer_height
    if not display_rows:
        height = header_height + row_height * 3 + footer_height

    image = Image.new("RGB", (width, height), "#f6efe3")
    draw = ImageDraw.Draw(image)
    title_font = load_font(34, bold=True)
    body_font = load_font(22)
    strong_font = load_font(22, bold=True)
    small_font = load_font(16)

    draw.rounded_rectangle((24, 20, width - 24, height - 20), radius=24, fill="#fffaf1", outline="#d8c8ad", width=2)
    draw.text((48, 36), "{} Rating榜".format(bucket["name"]), fill="#2b2115", font=title_font)
    subtitle = "{} | {} | {}".format(bucket["family_name"], bucket["competition_type"], bucket["code"])
    draw.text((48, 84), subtitle, fill="#6b5d49", font=body_font)
    summary = "统计选手: {} 人 | 导出时间: {}".format(len(rows), exported_at)
    draw.text((48, 120), summary, fill="#6b5d49", font=small_font)
    draw.text((48, 146), "按 rating 从高到低排序；RD 越低代表当前 rating 越稳定", fill="#6b5d49", font=small_font)

    table_top = header_height
    draw.rounded_rectangle((40, table_top, width - 40, table_top + row_height), radius=12, fill="#ead9bf")
    columns = [
        ("排名", 60),
        ("选手", 140),
        ("Rating", 560),
        ("RD", 720),
        ("参赛", 850),
        ("最高", 970),
        ("最近赛事", 1080),
    ]
    for label, x in columns:
        draw.text((x, table_top + 10), label, fill="#3a2c1c", font=strong_font)

    if not display_rows:
        top = table_top + row_height
        draw.rectangle((40, top, width - 40, top + row_height * 2), fill="#fff4e1")
        draw.text((60, top + 34), "暂无 rating 数据。请先结算正式计 rating 赛事并重算 rating。", fill="#6b5d49", font=body_font)
    else:
        medal_colors = {1: "#c58a13", 2: "#8d96a0", 3: "#b36d3f"}
        for index, row in enumerate(display_rows, start=1):
            top = table_top + row_height * index
            fill = "#fff4e1" if index % 2 else "#f8e9d3"
            if index == 1:
                fill = "#f8ddb1"
            elif index == 2:
                fill = "#e6e9ec"
            elif index == 3:
                fill = "#edd0b8"
            draw.rectangle((40, top, width - 40, top + row_height), fill=fill)
            player_text = row["display_name"]
            if row["account_name"] and row["account_name"] != row["display_name"]:
                player_text = "{} ({})".format(row["display_name"], row["account_name"])
            draw.text((60, top + 10), str(index), fill=medal_colors.get(index, "#3a2c1c"), font=strong_font)
            draw.text((140, top + 10), fit_text(draw, player_text, body_font, 380), fill="#2f261b", font=body_font)
            draw.text((560, top + 10), "{:.1f}".format(row["rating_value"]), fill="#2f261b", font=body_font)
            draw.text((720, top + 10), "{:.1f}".format(row["rating_deviation"]), fill="#2f261b", font=body_font)
            draw.text((850, top + 10), str(row["event_count"]), fill="#2f261b", font=body_font)
            best_rating = row["best_rating"] if row["best_rating"] is not None else row["rating_value"]
            draw.text((970, top + 10), "{:.1f}".format(best_rating), fill="#2f261b", font=body_font)
            draw.text((1080, top + 10), fit_text(draw, row["last_event_code"] or "-", small_font, 90), fill="#2f261b", font=small_font)

    footer_text = "长期统榜图片保存在“比赛导出/长期统榜/BUCKET_CODE”目录中"
    draw.text((48, height - 46), footer_text, fill="#746754", font=small_font)
    path = output_dir / "Rating榜.png"
    image.save(path)
    return path


def export_leaderboards(connection, bucket_code, output_root=None, limit=100):
    rating = list_rating_leaderboard(connection, bucket_code, limit=limit)
    performance = list_performance_leaderboard(connection, bucket_code, limit=limit)
    bucket = rating["bucket"]
    output_dir = Path(output_root) if output_root else EXPORTS_DIR / "长期统榜" / bucket["code"]
    output_dir.mkdir(parents=True, exist_ok=True)

    exported_at = now_iso()
    payload = {
        "snapshot_type": "long_term_leaderboard",
        "bucket": {
            "code": bucket["code"],
            "name": bucket["name"],
            "family_code": bucket["family_code"],
            "family_name": bucket["family_name"],
            "competition_type": bucket["competition_type"],
            "target_value": bucket["target_value"],
        },
        "exported_at": exported_at,
        "rating": [
            {
                "rank": index,
                "display_name": row["display_name"],
                "account_name": row["account_name"],
                "rating_value": round(row["rating_value"], 2),
                "rating_deviation": round(row["rating_deviation"], 2),
                "event_count": row["event_count"],
                "best_rating": round(row["best_rating"], 2) if row["best_rating"] is not None else None,
                "last_event_code": row["last_event_code"],
            }
            for index, row in enumerate(rating["rows"], start=1)
        ],
        "performance": [
            {
                "rank": index,
                "display_name": row["display_name"],
                "account_name": row["account_name"],
                "primary_metric_type": row["primary_metric_type"],
                "primary_metric_value": row["primary_metric_value"],
                "best_single_score": row["best_single_score"],
                "event_code": row["event_code"],
                "event_name": row["event_name"],
            }
            for index, row in enumerate(performance["rows"], start=1)
        ],
    }

    json_path = output_dir / "长期统榜.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "{} 长期统榜".format(bucket["name"]),
        "bucket: {}".format(bucket["code"]),
        "exported_at: {}".format(exported_at),
        "",
        "Rating 榜",
    ]
    if rating["rows"]:
        for index, row in enumerate(rating["rows"], start=1):
            lines.append(
                "{}. {} | rating {:.1f} | RD {:.1f} | events {}".format(
                    index,
                    row["display_name"],
                    row["rating_value"],
                    row["rating_deviation"],
                    row["event_count"],
                )
            )
    else:
        lines.append("- 暂无 rating")

    lines.extend(["", "历史成绩榜"])
    if performance["rows"]:
        for index, row in enumerate(performance["rows"], start=1):
            lines.append(
                "{}. {} | {} {} | {}".format(
                    index,
                    row["display_name"],
                    row["primary_metric_type"],
                    row["primary_metric_value"],
                    row["event_code"],
                )
            )
    else:
        lines.append("- 暂无成绩")

    txt_path = output_dir / "长期统榜.txt"
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    image_path = write_rating_leaderboard_image(bucket, rating["rows"], output_dir, exported_at)
    return {
        "output_dir": output_dir,
        "json_path": json_path,
        "txt_path": txt_path,
        "image_path": image_path,
        "rating_count": len(rating["rows"]),
        "performance_count": len(performance["rows"]),
    }
