"""领域逻辑测试：种子口径、优先级、漏分/并列见证、压缩算法可扩展性。"""

import time

from hydrology.domain import (
    Range,
    Rule,
    classify_pair,
    coerce_pair,
    parse_range,
    parse_rules,
    verify,
)
from hydrology.seed import SEED_RANGE, build_seed_rules


def test_seed_published_criteria_are_valid():
    report = verify(SEED_RANGE, build_seed_rules())
    assert report["ok"], report
    assert report["gap"] is None
    assert report["tie"] is None
    assert report["gap_cell_count"] == 0
    assert report["tie_cell_count"] == 0
    assert report["elementary_cells"] >= 1


def test_flood_zone_wins_over_conventional_grid():
    rules = build_seed_rules()

    flood = classify_pair(SEED_RANGE, rules, 90, 900)
    assert flood["status"] == "classified"
    assert flood["zone"] == "洪水样本区"
    assert flood["priority"] == 1

    # 同在“高水高流量”网格内，但未进入洪水叠加矩形 -> 常规分区
    edge = classify_pair(SEED_RANGE, rules, 82, 820)
    assert edge["status"] == "classified"
    assert edge["zone"] == "高水高流量区"

    just_outside = classify_pair(SEED_RANGE, rules, 86, 849)
    assert just_outside["zone"] == "高水高流量区"

    low = classify_pair(SEED_RANGE, rules, 10, 100)
    assert low["zone"] == "低水低流量区"


def test_out_of_range_and_bad_input():
    rules = build_seed_rules()
    out = classify_pair(SEED_RANGE, rules, 150, 100)
    assert out["status"] == "out_of_range"
    assert out["axis"] == "water_level"

    wl, fl, errors = coerce_pair("1.5", "10")
    assert wl is None and fl == 10
    assert any("水位" in e for e in errors)

    wl, fl, errors = coerce_pair(True, "10")
    assert errors  # 布尔不得当作整数接受


def test_gap_witness_is_reproducible_pair():
    rules = build_seed_rules()
    # 删掉覆盖 (0,0) 的第一条网格规则：低水低流量区
    broken = [r for i, r in enumerate(rules) if i != 0]

    rep1 = verify(SEED_RANGE, broken)
    rep2 = verify(SEED_RANGE, broken)

    assert not rep1["ok"]
    assert rep1["gap"] is not None
    w = rep1["gap"]
    # 压缩扫描的第一个漏分初等格左下角即 (0,0)
    assert (w["water_level"], w["flow"]) == (0, 0)
    # 可复现：同口径两次核对给出完全相同的读数对
    assert (rep2["gap"]["water_level"], rep2["gap"]["flow"]) == (0, 0)
    assert w["reproduce"] == "water_level=0&flow=0"

    verdict = classify_pair(SEED_RANGE, broken, 0, 0)
    assert verdict["status"] == "uncovered"


def test_tie_witness_lists_top_zones_and_is_reproducible():
    rules = build_seed_rules()
    rival = Rule(
        zone="并列洪峰复核区",
        priority=1,
        wl_min=90,
        wl_max=100,
        fl_min=900,
        fl_max=1000,
        ord=len(rules),
    )
    tied = rules + [rival]

    rep = verify(SEED_RANGE, tied)
    assert not rep["ok"]
    assert rep["tie"] is not None
    t = rep["tie"]
    assert (t["water_level"], t["flow"]) == (90, 900)
    assert t["priority"] == 1
    assert t["top_zones"] == ["洪水样本区", "并列洪峰复核区"]
    assert {r["zone"] for r in t["top_rules"]} == {
        "洪水样本区",
        "并列洪峰复核区",
    }

    verdict = classify_pair(SEED_RANGE, tied, 90, 900)
    assert verdict["status"] == "ambiguous"
    assert set(verdict["tied_top_zones"]) == {
        "洪水样本区",
        "并列洪峰复核区",
    }


def test_same_priority_non_overlapping_rules_are_fine():
    rng = Range(0, 9, 0, 9)
    rules = [
        Rule("左区", 5, 0, 4, 0, 9, ord=0),
        Rule("右区", 5, 5, 9, 0, 9, ord=1),
    ]
    rep = verify(rng, rules)
    assert rep["ok"], rep


def test_check_scales_with_endpoints_not_range_values():
    # 量程跨度十亿：若逐值枚举必然爆炸；压缩算法应瞬间完成。
    huge = Range(0, 10**9, 0, 10**9)
    rules = [
        Rule("左下", 10, 0, 499_999_999, 0, 499_999_999, ord=0),
        Rule("右下", 10, 500_000_000, 10**9, 0, 499_999_999, ord=1),
        Rule("左上", 10, 0, 499_999_999, 500_000_000, 10**9, ord=2),
        Rule("右上", 10, 500_000_000, 10**9, 500_000_000, 10**9, ord=3),
    ]
    start = time.perf_counter()
    rep = verify(huge, rules)
    elapsed = time.perf_counter() - start
    assert rep["ok"], rep
    assert elapsed < 2.0

    # 全量程两条同级规则 -> 并列见证 (0,0)，同样不依赖枚举
    tie_rep = verify(
        huge,
        [
            Rule("甲", 1, 0, 10**9, 0, 10**9, ord=0),
            Rule("乙", 1, 0, 10**9, 0, 10**9, ord=1),
        ],
    )
    assert tie_rep["tie"]["water_level"] == 0
    assert tie_rep["tie"]["flow"] == 0
    assert tie_rep["tie"]["top_zones"] == ["甲", "乙"]


def test_parsing_is_strict_about_integers_and_intervals():
    rng, errs = parse_range(
        {"wl_min": "0", "wl_max": "10", "fl_min": "1.5", "fl_max": "9"}
    )
    assert rng is None and any("流量下限" in e for e in errs)

    rng2, errs2 = parse_range(
        {"wl_min": "10", "wl_max": "1", "fl_min": "0", "fl_max": "9"}
    )
    assert rng2 is None and any("水位下限不能大于" in e for e in errs2)

    rules, errs3 = parse_rules(
        [
            {"zone": "A", "priority": "1", "wl_min": 0, "wl_max": 2,
             "fl_min": 0, "fl_max": 2},
            {"zone": "", "priority": "x", "wl_min": 0, "wl_max": 2,
             "fl_min": 0, "fl_max": 2},
            {"zone": "", "priority": "", "wl_min": "", "wl_max": "",
             "fl_min": "", "fl_max": ""},  # 全空行应被忽略
        ]
    )
    assert len(rules) == 1
    assert any("分区名不能为空" in e for e in errs3)
    assert any("优先级" in e for e in errs3)
