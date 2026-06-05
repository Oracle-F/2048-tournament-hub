from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .testing_support import PROJECT_ROOT, fresh_test_connection, set_test_database_environment, test_temporary_directory


set_test_database_environment()

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import transaction
from services.competition_mode_service import get_competition_mode
from services.event_admin_service import create_or_update_event, infer_rating_bucket_code
from services.rating_service import recalculate_event_bucket_ratings
from services.raw_score_import_service import import_score_file
from services.settlement_service import build_ranked_results, settle_event
from settings import LOCAL_TIMEZONE, TIME_FORMAT


SCENARIOS = [
    {
        "name": "classic_normal_10",
        "players": 10,
        "distribution": "normal",
        "mode_code": "classic_4x4_raw_score",
    },
    {
        "name": "stone_high_50",
        "players": 50,
        "distribution": "high",
        "mode_code": "stone_x_4x4",
    },
    {
        "name": "no_x_extreme_100",
        "players": 100,
        "distribution": "extreme",
        "mode_code": "no_x_4x4",
    },
    {
        "name": "speedrun_boundary_1000",
        "players": 1000,
        "distribution": "boundary",
        "mode_code": "speedrun_4x4",
    },
]

FAST_SCENARIOS = SCENARIOS[:3]


def _now_base():
    return datetime(2026, 5, 15, 10, 0, 0, tzinfo=LOCAL_TIMEZONE)


def _dt_text(value: datetime) -> str:
    return value.strftime(TIME_FORMAT)


def _scenario_title(scenario: dict[str, Any]) -> str:
    return "{} ({}, {} players)".format(
        scenario["name"],
        scenario["distribution"],
        scenario["players"],
    )


def _select_scenarios(players: int | None = None, distribution: str | None = None):
    if players is None and distribution is None:
        return list(SCENARIOS)
    if players is None or distribution is None:
        raise ValueError("players and distribution must be provided together")
    for scenario in SCENARIOS:
        if scenario["players"] == players and scenario["distribution"] == distribution:
            return [scenario]
    raise ValueError("Unsupported scenario combination: {} / {}".format(players, distribution))


def _build_record(scenario: dict[str, Any], index: int, start_time: datetime) -> dict[str, Any]:
    mode_code = scenario["mode_code"]
    distribution = scenario["distribution"]
    players = scenario["players"]
    username = "sim_{:04d}".format(index)
    display_name = "Player{:04d}".format(index)
    ended_at = start_time + timedelta(seconds=index % 47 + 1)
    started_at = ended_at - timedelta(seconds=1)

    if mode_code == "classic_4x4_raw_score":
        base_score = 12000
        raw_score = base_score + index * 37 + (index % 5) * 11
        if distribution == "normal":
            final_score = raw_score - (index % 4)
        elif distribution == "high":
            final_score = raw_score + 100 + (index % 7) * 5
        else:
            final_score = raw_score
        competition_score = final_score
        return {
            "username": username,
            "display_name": display_name,
            "source_record_id": "sim_classic_{}".format(index),
            "record_type": "locked_first_game",
            "started_at": _dt_text(started_at),
            "ended_at": _dt_text(ended_at),
            "raw_score": raw_score,
            "final_score": final_score,
            "competition_score": competition_score,
            "primary_time_ms": None,
            "target_tile_value": None,
            "score_before_target": None,
            "result_state": "completed",
            "evidence_json": json.dumps(
                {"scenario": scenario["name"], "distribution": distribution, "position": index, "players": players},
                ensure_ascii=False,
            ),
        }

    if mode_code == "stone_x_4x4":
        target_value = 2048
        base_score = 180000
        raw_score = base_score + index * 711 + (index % 13) * 23
        if distribution == "high":
            competition_score = raw_score + 2500 + (index % 9) * 17
        else:
            competition_score = raw_score
        return {
            "username": username,
            "display_name": display_name,
            "source_record_id": "sim_stone_{}".format(index),
            "record_type": "manual_import",
            "started_at": _dt_text(started_at),
            "ended_at": _dt_text(ended_at),
            "raw_score": raw_score,
            "final_score": raw_score,
            "competition_score": competition_score,
            "primary_time_ms": None,
            "target_tile_value": target_value,
            "score_before_target": max(0, competition_score - target_value),
            "result_state": "completed",
            "evidence_json": json.dumps(
                {"scenario": scenario["name"], "distribution": distribution, "position": index},
                ensure_ascii=False,
            ),
        }

    if mode_code == "no_x_4x4":
        target_value = 8192
        base_score = 260000
        if distribution == "extreme":
            competition_score = base_score + (index % 6) * 25000 + index * 97
        else:
            competition_score = base_score + index * 301
        return {
            "username": username,
            "display_name": display_name,
            "source_record_id": "sim_no_x_{}".format(index),
            "record_type": "manual_import",
            "started_at": _dt_text(started_at),
            "ended_at": _dt_text(ended_at),
            "raw_score": competition_score,
            "final_score": competition_score,
            "competition_score": competition_score,
            "primary_time_ms": None,
            "target_tile_value": target_value,
            "score_before_target": max(0, competition_score - target_value),
            "result_state": "completed",
            "evidence_json": json.dumps(
                {"scenario": scenario["name"], "distribution": distribution, "position": index},
                ensure_ascii=False,
            ),
        }

    if mode_code == "speedrun_4x4":
        base_time = 1000
        if distribution == "boundary":
            primary_time_ms = base_time + (index % 17)
            if index % 9 == 0:
                primary_time_ms = base_time
        else:
            primary_time_ms = base_time + index * 2 + (index % 3)
        return {
            "username": username,
            "display_name": display_name,
            "source_record_id": "sim_speedrun_{}".format(index),
            "record_type": "manual_import",
            "started_at": _dt_text(started_at),
            "ended_at": _dt_text(ended_at),
            "raw_score": None,
            "final_score": None,
            "competition_score": primary_time_ms,
            "primary_time_ms": primary_time_ms,
            "target_tile_value": None,
            "score_before_target": None,
            "result_state": "completed",
            "evidence_json": json.dumps(
                {"scenario": scenario["name"], "distribution": distribution, "position": index},
                ensure_ascii=False,
            ),
        }

    raise ValueError("Unsupported mode code: {}".format(mode_code))


def _build_score_records(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    base_time = _now_base()
    records = []
    for index in range(1, scenario["players"] + 1):
        records.append(_build_record(scenario, index, base_time))
    return records


def _write_score_file(temp_dir: Path, scenario: dict[str, Any]) -> Path:
    file_path = temp_dir / "{}.json".format(scenario["name"])
    file_path.write_text(json.dumps(_build_score_records(scenario), ensure_ascii=False, indent=2), encoding="utf-8")
    return file_path


def _scenario_event_setup(connection, scenario: dict[str, Any]) -> dict[str, Any]:
    mode = get_competition_mode(scenario["mode_code"])
    target_value = mode.get("default_target_value")
    if scenario["mode_code"] == "stone_x_4x4":
        target_value = 2048
    if scenario["mode_code"] == "no_x_4x4":
        target_value = 8192
    if scenario["mode_code"] == "speedrun_4x4":
        target_value = 2048
    rating_bucket_code = infer_rating_bucket_code(mode["variant_code"], mode["competition_type"], target_value)

    base = _now_base()
    start_time = _dt_text(base)
    end_time = _dt_text(base + timedelta(hours=1))
    event_code = "SIM_{:s}_{:s}".format(scenario["mode_code"].upper(), str(scenario["players"]))
    with transaction(connection):
        create_or_update_event(
            connection,
            event_code=event_code,
            event_name="{} 模拟赛事".format(mode["label"]),
            platform_code=mode["platform_code"],
            variant_code=mode["variant_code"],
            event_type=mode["event_type_default"],
            competition_type=mode["competition_type"],
            rating_bucket_code=rating_bucket_code,
            status="finished",
            is_official=True,
            is_rated=True,
            registration_close_time=start_time,
            start_time=start_time,
            end_time=end_time,
            seal_time=end_time,
            source="simulation_suite",
            tags=["simulation"],
            target_value=target_value,
            metadata={
                "simulation": True,
                "scenario": scenario["name"],
                "players": scenario["players"],
                "distribution": scenario["distribution"],
            },
            rule_overrides={},
        )
    return {
        "event_code": event_code,
        "rating_bucket_code": rating_bucket_code,
        "mode": mode,
        "target_value": target_value,
    }


def run_scenario(scenario: dict[str, Any], *, emit_lines: bool = True):
    with fresh_test_connection() as connection:
        try:
            setup = _scenario_event_setup(connection, scenario)
            if emit_lines:
                print("[SIM] {}".format(_scenario_title(scenario)))
                print("创建赛事成功")

            with test_temporary_directory(prefix="tournament_sim_") as temp_dir:
                score_file = _write_score_file(temp_dir, scenario)
                imported = None
                with transaction(connection):
                    imported = import_score_file(connection, setup["event_code"], score_file)
                if emit_lines:
                    print("生成虚拟选手成功: {}".format(imported["new_players"]))
                    print("导入成绩成功: {}".format(imported["record_count"]))

            preview = build_ranked_results(connection, setup["event_code"])
            if emit_lines:
                print("排行榜生成成功: {}".format(preview["record_count"]))

            with transaction(connection):
                settled = settle_event(connection, setup["event_code"])
            if emit_lines:
                print("结算成功: {}".format(settled["player_count"]))

            with transaction(connection):
                ratings = recalculate_event_bucket_ratings(connection, setup["event_code"])
            if emit_lines:
                print("计算积分成功: {}".format(ratings["history_count"]))
                print("PASS")

            return {
                "name": scenario["name"],
                "players": scenario["players"],
                "distribution": scenario["distribution"],
                "event_code": setup["event_code"],
                "ok": True,
                "preview": preview,
                "settled": settled,
                "ratings": ratings,
            }
        except Exception as exc:
            if emit_lines:
                print("FAIL")
                print(str(exc))
            return {
                "name": scenario["name"],
                "players": scenario["players"],
                "distribution": scenario["distribution"],
                "ok": False,
                "error": str(exc),
            }


def run_suite(players: int | None = None, distribution: str | None = None, *, emit_lines: bool = True):
    scenarios = _select_scenarios(players, distribution)
    results = [run_scenario(scenario, emit_lines=emit_lines) for scenario in scenarios]
    passed = sum(1 for item in results if item["ok"])
    total = len(results)
    return {
        "results": results,
        "passed": passed,
        "total": total,
        "failed": [item for item in results if not item["ok"]],
    }


def run_default_suite(*, emit_lines: bool = True):
    results = [run_scenario(scenario, emit_lines=emit_lines) for scenario in SCENARIOS]
    passed = sum(1 for item in results if item["ok"])
    total = len(results)
    return {
        "results": results,
        "passed": passed,
        "total": total,
        "failed": [item for item in results if not item["ok"]],
    }


def run_fast_suite(*, emit_lines: bool = True):
    results = [run_scenario(scenario, emit_lines=emit_lines) for scenario in FAST_SCENARIOS]
    passed = sum(1 for item in results if item["ok"])
    total = len(results)
    return {
        "results": results,
        "passed": passed,
        "total": total,
        "failed": [item for item in results if not item["ok"]],
    }


def main():
    parser = argparse.ArgumentParser(description="Run lightweight tournament simulations.")
    parser.add_argument("--players", type=int, choices=[10, 50, 100, 1000])
    parser.add_argument("--distribution", choices=["normal", "high", "extreme", "boundary"])
    parser.add_argument("--fast", action="store_true", help="Run the reduced simulation set.")
    args = parser.parse_args()

    if args.players is None and args.distribution is None:
        result = run_fast_suite(emit_lines=True) if args.fast else run_default_suite(emit_lines=True)
    else:
        result = run_suite(args.players, args.distribution, emit_lines=True)

    print("")
    print("Simulation Summary: {} passed, {} failed, {} total".format(result["passed"], len(result["failed"]), result["total"]))
    raise SystemExit(0 if not result["failed"] else 1)


if __name__ == "__main__":
    main()
