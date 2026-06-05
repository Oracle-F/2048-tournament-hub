COMPETITION_MODES = [
    {
        "code": "timed_2x4",
        "label": "2x4 限时赛",
        "default_name": "2x4 限时赛",
        "platform_code": "2048verse",
        "variant_code": "2x4",
        "competition_type": "timed_scoring",
        "tags": ["timed_scoring"],
        "targets": [],
        "default_target_value": None,
        "event_type_default": "timed_scoring",
        "aggregation_default": "sum",
        "aggregation_options": ["sum"],
        "uses_locking": False,
        "score_import_notes": "限时赛成绩（支持预约型子模式）。",
    },
    {
        "code": "timed_3x3",
        "label": "3x3 限时赛",
        "default_name": "3x3 限时赛",
        "platform_code": "2048verse",
        "variant_code": "3x3",
        "competition_type": "timed_scoring",
        "tags": ["timed_scoring"],
        "targets": [],
        "default_target_value": None,
        "event_type_default": "timed_scoring",
        "aggregation_default": "sum",
        "aggregation_options": ["sum"],
        "uses_locking": False,
        "score_import_notes": "限时赛成绩（支持预约型子模式）。",
    },
    {
        "code": "points_series_3x4",
        "label": "3x4 积分赛",
        "default_name": "3x4 积分赛",
        "platform_code": "2048verse",
        "variant_code": "3x4",
        "competition_type": "points_series_3x4",
        "tags": ["raw_score_hosted", "points_series"],
        "targets": [],
        "default_target_value": None,
        "event_type_default": "multi_attempt",
        "aggregation_default": "weighted_top_n",
        "aggregation_options": ["weighted_top_n"],
        "uses_locking": False,
        "score_import_notes": "提交 competition_score 作为终局盘面和；按前5高局加权积分。",
    },
    {
        "code": "classic_4x4_raw_score",
        "label": "经典 4x4 原始分",
        "default_name": "经典4x4",
        "platform_code": "2048verse",
        "variant_code": "4x4",
        "competition_type": "classic_raw_score",
        "tags": ["raw_score_hosted"],
        "targets": [],
        "default_target_value": None,
        "event_type_default": "single_attempt",
        "aggregation_default": "best_single",
        "aggregation_options": ["best_single", "best_of_n", "average_of_n", "sum_of_best_n"],
        "uses_locking": True,
        "score_import_notes": "Verse 锁局为主，也可导入补充成绩文件。",
    },
    {
        "code": "stone_x_4x4",
        "label": "Stone 4x4",
        "default_name": "Stone",
        "platform_code": "taihe",
        "variant_code": "4x4",
        "competition_type": "stone_x",
        "tags": ["raw_score_hosted", "stone"],
        "targets": [
            {"label": "1k Stone", "value": 1024, "default_name": "1k Stone"},
            {"label": "2k Stone", "value": 2048, "default_name": "2k Stone"},
        ],
        "default_target_value": 2048,
        "event_type_default": "multi_attempt",
        "aggregation_default": "best_of_n",
        "aggregation_options": ["best_single", "best_of_n", "average_of_n", "sum_of_best_n"],
        "uses_locking": False,
        "score_import_notes": "通过成绩文件导入，competition_score 表示 Stone 比赛分。",
    },
    {
        "code": "no_x_4x4",
        "label": "No-X 4x4",
        "default_name": "No-X 4x4",
        "platform_code": "taihe",
        "variant_code": "4x4",
        "competition_type": "no_x",
        "tags": ["raw_score_hosted", "no_x"],
        "targets": [
            {"label": "No-1k", "value": 1024, "default_name": "No-1k 4x4"},
            {"label": "No-2k", "value": 2048, "default_name": "No-2k 4x4"},
            {"label": "No-8k", "value": 8192, "default_name": "No-8k 4x4"},
            {"label": "No-16k", "value": 16384, "default_name": "No-16k 4x4"},
        ],
        "default_target_value": 2048,
        "event_type_default": "multi_attempt",
        "aggregation_default": "best_of_n",
        "aggregation_options": ["best_single", "best_of_n", "average_of_n", "sum_of_best_n"],
        "uses_locking": False,
        "score_import_notes": "通过成绩文件导入，competition_score 表示 No-X 比赛分。",
    },
    {
        "code": "speedrun_4x4",
        "label": "Speedrun 4x4",
        "default_name": "Speedrun 4x4",
        "platform_code": "taihe",
        "variant_code": "4x4",
        "competition_type": "speedrun",
        "tags": ["raw_score_hosted", "speedrun"],
        "targets": [
            {"label": "1k Speedrun", "value": 1024, "default_name": "1k Speedrun"},
            {"label": "2k Speedrun", "value": 2048, "default_name": "2k Speedrun"},
        ],
        "default_target_value": 2048,
        "event_type_default": "multi_attempt",
        "aggregation_default": "best_single",
        "aggregation_options": ["best_single", "best_of_n", "average_of_n", "sum_of_best_n"],
        "uses_locking": False,
        "score_import_notes": "通过成绩文件导入，primary_time_ms 表示完成时间，越低越好。",
    },
    {
        "code": "fibonacci_raw_score",
        "label": "斐波那契原始分",
        "default_name": "斐波那契",
        "platform_code": "taihe",
        "variant_code": "fibonacci_4x4",
        "variants": [
            {"label": "斐波那契 3x3", "value": "fibonacci_3x3", "default_name": "斐波那契3x3"},
            {"label": "斐波那契 4x4", "value": "fibonacci_4x4", "default_name": "斐波那契4x4"},
            {"label": "斐波那契 5x5", "value": "fibonacci_5x5", "default_name": "斐波那契5x5"},
        ],
        "competition_type": "fibonacci_raw_score",
        "tags": ["raw_score_hosted", "fibonacci"],
        "targets": [],
        "default_target_value": None,
        "event_type_default": "multi_attempt",
        "aggregation_default": "best_single",
        "aggregation_options": ["best_single", "best_of_n", "average_of_n"],
        "uses_locking": False,
        "score_import_notes": "通过成绩文件导入，raw_score/final_score 表示斐波那契规则下的原始分。",
    },
]


AGGREGATION_LABELS = {
    "best_single": "单局最好",
    "best_of_n": "多局取最好",
    "average_of_n": "多局平均",
    "sum_of_best_n": "取最高的X局求和",
    "weighted_top_n": "前N高局加权",
    "sum": "求和",
    "latest": "最近一局",
}


def list_competition_modes():
    return list(COMPETITION_MODES)


def get_competition_mode(mode_code):
    for mode in COMPETITION_MODES:
        if mode["code"] == mode_code:
            return dict(mode)
    raise ValueError("Unknown competition mode: {}".format(mode_code))


def get_target(mode, target_value):
    targets = mode.get("targets") or []
    if not targets:
        return None
    for target in targets:
        if target["value"] == target_value:
            return target
    raise ValueError("Unsupported target {} for mode {}".format(target_value, mode["code"]))


def get_variant(mode, variant_code):
    variants = mode.get("variants") or []
    if not variants:
        return None
    for variant in variants:
        if variant["value"] == variant_code:
            return variant
    raise ValueError("Unsupported variant {} for mode {}".format(variant_code, mode["code"]))


def aggregation_label(value):
    return AGGREGATION_LABELS.get(value, value)


def event_uses_locking(competition_type, platform_code, variant_code=None):
    for mode in COMPETITION_MODES:
        if mode.get("competition_type") != competition_type:
            continue
        if mode.get("platform_code") != platform_code:
            continue
        mode_variant = mode.get("variant_code")
        if mode_variant and variant_code and mode_variant != variant_code:
            continue
        return bool(mode.get("uses_locking"))
    return False
