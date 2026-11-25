"""首次启动时播种的现行口径。

量程：水位 0..100，流量 0..1000（整数闭区间，单位由资料员按测站约定解读）。
常规分区按水位三带 × 流量三带切成 9 个互不重叠的网格，优先级 10；
“洪水样本区”作为高水位 + 高流量同时出现时的优先叠加规则（85..100 × 850..1000），
优先级 1，压在“高水高流量区”网格之上。全量程无漏分、最高级无并列。
"""

from __future__ import annotations

from .domain import Range, Rule

SEED_RANGE = Range(wl_min=0, wl_max=100, fl_min=0, fl_max=1000)

# (水位带下界, 上界, 带名)
_WL_BANDS = [
    (0, 49, "低水"),
    (50, 79, "中水"),
    (80, 100, "高水"),
]
# (流量带下界, 上界, 带名)
_FL_BANDS = [
    (0, 399, "低流量"),
    (400, 799, "中流量"),
    (800, 1000, "高流量"),
]


def build_seed_rules() -> list[Rule]:
    rules: list[Rule] = []
    ord_no = 0
    for wl_lo, wl_hi, wl_name in _WL_BANDS:
        for fl_lo, fl_hi, fl_name in _FL_BANDS:
            rules.append(
                Rule(
                    zone=f"{wl_name}{fl_name}区",
                    priority=10,
                    wl_min=wl_lo,
                    wl_max=wl_hi,
                    fl_min=fl_lo,
                    fl_max=fl_hi,
                    ord=ord_no,
                )
            )
            ord_no += 1

    # 高水位、高流量同时出现：优先归入洪水样本区。
    rules.append(
        Rule(
            zone="洪水样本区",
            priority=1,
            wl_min=85,
            wl_max=100,
            fl_min=850,
            fl_max=1000,
            ord=ord_no,
        )
    )
    return rules
