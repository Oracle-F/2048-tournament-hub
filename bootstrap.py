import json
from datetime import datetime

from settings import CONFIG_DIR


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_json_file(file_name):
    path = CONFIG_DIR / file_name
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def migrate_legacy_stone_names(connection):
    connection.execute("UPDATE rating_buckets SET code = 'stone_1k_4x4' WHERE code = 'cap_1k_4x4'")
    connection.execute("UPDATE rating_buckets SET code = 'stone_2k_4x4' WHERE code = 'cap_2k_4x4'")
    connection.execute("UPDATE rating_buckets SET name = '1k Stone 4x4' WHERE code = 'stone_1k_4x4'")
    connection.execute("UPDATE rating_buckets SET name = '2k Stone 4x4' WHERE code = 'stone_2k_4x4'")
    connection.execute("UPDATE rating_buckets SET competition_type = 'stone_x' WHERE competition_type = 'cap_x'")
    connection.execute("UPDATE events SET competition_type = 'stone_x' WHERE competition_type = 'cap_x'")
    connection.execute("UPDATE event_rule_sets SET rule_type = 'stone_x' WHERE rule_type = 'cap_x'")
    connection.execute("UPDATE event_rule_sets SET validation_rule = 'stone_target' WHERE validation_rule = 'cap_target'")


def bootstrap_platforms(connection):
    items = load_json_file("platforms.json")
    for item in items:
        connection.execute(
            """
            INSERT INTO platforms (code, name, base_url, is_active, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                base_url = excluded.base_url,
                is_active = excluded.is_active,
                metadata_json = excluded.metadata_json
            """,
            (
                item["code"],
                item["name"],
                item.get("base_url"),
                1 if item.get("is_active", True) else 0,
                json.dumps(item.get("metadata", {}), ensure_ascii=False),
                now_iso(),
            ),
        )


def bootstrap_variants(connection):
    items = load_json_file("variants.json")
    for item in items:
        connection.execute(
            """
            INSERT INTO variants (
                code, name, game_family, shape_type, board_width, board_height,
                ruleset_key, category, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                game_family = excluded.game_family,
                shape_type = excluded.shape_type,
                board_width = excluded.board_width,
                board_height = excluded.board_height,
                ruleset_key = excluded.ruleset_key,
                category = excluded.category,
                metadata_json = excluded.metadata_json
            """,
            (
                item["code"],
                item["name"],
                item.get("game_family"),
                item.get("shape_type"),
                item.get("board_width"),
                item.get("board_height"),
                item.get("ruleset_key"),
                item.get("category"),
                json.dumps(item.get("metadata", {}), ensure_ascii=False),
                now_iso(),
            ),
        )


def bootstrap_rating_families(connection):
    items = load_json_file("rating_families.json")
    for item in items:
        connection.execute(
            """
            INSERT INTO rating_families (code, name, description, is_active, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                is_active = excluded.is_active,
                sort_order = excluded.sort_order
            """,
            (
                item["code"],
                item["name"],
                item.get("description"),
                1 if item.get("is_active", True) else 0,
                item.get("sort_order", 0),
                now_iso(),
            ),
        )


def lookup_id(connection, table_name, code):
    row = connection.execute(
        "SELECT id FROM {} WHERE code = ?".format(table_name),
        (code,),
    ).fetchone()
    return None if row is None else row["id"]


def bootstrap_rating_buckets(connection):
    items = load_json_file("rating_buckets.json")
    expanded_items = []
    for item in items:
        expanded_items.append(item)
        trial = dict(item)
        trial["code"] = "{}_g2_trial".format(item["code"])
        trial["name"] = "{}(G2试运行)".format(item["name"])
        trial_meta = dict(item.get("metadata", {}))
        trial_meta["rating_algorithm"] = "glicko2_v1"
        trial_meta["trial_bucket"] = True
        trial["metadata"] = trial_meta
        expanded_items.append(trial)

    for item in expanded_items:
        family_id = lookup_id(connection, "rating_families", item["family_code"])
        platform_id = lookup_id(connection, "platforms", item["platform_code"]) if item.get("platform_code") else None
        variant_id = lookup_id(connection, "variants", item["variant_code"]) if item.get("variant_code") else None
        if family_id is None:
            raise ValueError("Missing rating family for bucket {}".format(item["code"]))

        connection.execute(
            """
            INSERT INTO rating_buckets (
                code, family_id, name, platform_id, variant_id, competition_type,
                target_value, is_rated_by_default, is_active, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                family_id = excluded.family_id,
                name = excluded.name,
                platform_id = excluded.platform_id,
                variant_id = excluded.variant_id,
                competition_type = excluded.competition_type,
                target_value = excluded.target_value,
                is_rated_by_default = excluded.is_rated_by_default,
                is_active = excluded.is_active,
                metadata_json = excluded.metadata_json
            """,
            (
                item["code"],
                family_id,
                item["name"],
                platform_id,
                variant_id,
                item["competition_type"],
                item.get("target_value"),
                1 if item.get("is_rated_by_default", True) else 0,
                1 if item.get("is_active", True) else 0,
                json.dumps(item.get("metadata", {}), ensure_ascii=False),
                now_iso(),
            ),
        )


def bootstrap_all(connection):
    migrate_legacy_stone_names(connection)
    bootstrap_platforms(connection)
    bootstrap_variants(connection)
    bootstrap_rating_families(connection)
    bootstrap_rating_buckets(connection)
    connection.commit()
