"""水文分区口径的领域模型、核对算法与单对读数判定。

约定
----
* 水位、流量均为 **整数闭区间**。
* 优先级 ``priority`` 数值越小，级别越高（``1`` 为最高级）。
* 核对目标：有效量程内的每一对整数读数，都恰好有一条“最高优先级规则”命中
  （允许低优先级规则重叠，只要最高级唯一）。

核对不逐值枚举水位/流量，而是收集所有区间端点（闭区间右端点 +1）做
**坐标压缩**。所有规则的边界都在坐标轴分点上，因此相邻分点构成的每个
“初等整数格”内部，命中规则集合与优先级名次完全不变，只需取格内一个代表点
（左下角整数）核对即可。复杂度约为 O(n^3) 上界（格数 O(n^2)，每格扫描 n 条），
与量程跨度（可能上亿）无关。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

# 每类问题最多保留多少个见证格用于页面展示；首个见证始终保留。
MAX_SAMPLES = 20

_INT_RE = re.compile(r"^[+-]?\d+$")

WL_KEYS = ("wl_min", "wl_max")
FL_KEYS = ("fl_min", "fl_max")
RANGE_KEYS = ("wl_min", "wl_max", "fl_min", "fl_max")
RULE_KEYS = ("zone", "priority") + RANGE_KEYS

FIELD_LABELS = {
    "zone": "分区名",
    "priority": "优先级",
    "wl_min": "水位下限",
    "wl_max": "水位上限",
    "fl_min": "流量下限",
    "fl_max": "流量上限",
}


class InputError(ValueError):
    """输入口径数据无法解析。"""


def coerce_int(value: Any) -> int:
    """把严格的整数形态转成 int；拒绝小数、布尔与非数字字符串。"""
    if isinstance(value, bool):
        raise ValueError("必须是整数，不能是布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _INT_RE.match(text):
            return int(text)
    raise ValueError("必须填写整数（不接受小数 / 空值）")


@dataclass(frozen=True)
class Range:
    """水位与流量的有效量程，均为整数闭区间。"""

    wl_min: int
    wl_max: int
    fl_min: int
    fl_max: int

    def contains(self, wl: int, fl: int) -> bool:
        return (
            self.wl_min <= wl <= self.wl_max
            and self.fl_min <= fl <= self.fl_max
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "wl_min": self.wl_min,
            "wl_max": self.wl_max,
            "fl_min": self.fl_min,
            "fl_max": self.fl_max,
            "water_level": [self.wl_min, self.wl_max],
            "flow": [self.fl_min, self.fl_max],
        }


@dataclass(frozen=True)
class Rule:
    """一条分区规则：分区名、优先级、两种读数的整数闭区间。"""

    zone: str
    priority: int
    wl_min: int
    wl_max: int
    fl_min: int
    fl_max: int
    id: Optional[int] = None
    ord: Optional[int] = None

    def covers(self, wl: int, fl: int) -> bool:
        return (
            self.wl_min <= wl <= self.wl_max
            and self.fl_min <= fl <= self.fl_max
        )

    def identity(self) -> dict[str, Any]:
        return {
            "rule_id": self.id,
            "ord": self.ord,
            "zone": self.zone,
            "priority": self.priority,
            "wl_interval": [self.wl_min, self.wl_max],
            "fl_interval": [self.fl_min, self.fl_max],
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.identity()
        d.update(
            {
                "wl_min": self.wl_min,
                "wl_max": self.wl_max,
                "fl_min": self.fl_min,
                "fl_max": self.fl_max,
            }
        )
        return d


def parse_range(data: Optional[Mapping[str, Any]]) -> tuple[Optional[Range], list[str]]:
    """从映射解析量程，返回 (Range|None, 错误列表)。"""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return None, ["量程数据缺失或格式不正确，应为对象，包含 wl_min/wl_max/fl_min/fl_max"]

    values: dict[str, int] = {}
    for key in RANGE_KEYS:
        try:
            values[key] = coerce_int(data.get(key))
        except (ValueError, TypeError):
            errors.append(f"{FIELD_LABELS[key]}：必须是整数")

    if len(values) == 4:
        if values["wl_min"] > values["wl_max"]:
            errors.append("水位下限不能大于水位上限")
        if values["fl_min"] > values["fl_max"]:
            errors.append("流量下限不能大于流量上限")

    if errors:
        return None, errors
    return Range(**values), []


def parse_rules(items: Any) -> tuple[list[Rule], list[str]]:
    """解析规则数组。空行（六个字段全空）忽略；半填写行报错。"""
    errors: list[str] = []
    rules: list[Rule] = []
    if items is None:
        return rules, ["分区规则数据缺失"]
    if not isinstance(items, (list, tuple)):
        return rules, ["分区规则应为数组"]

    for idx, item in enumerate(items):
        row_no = idx + 1
        if not isinstance(item, Mapping):
            errors.append(f"第 {row_no} 条规则：格式不正确，应为对象")
            continue

        raw = {key: item.get(key) for key in RULE_KEYS}
        blank = all(
            v is None or (isinstance(v, str) and v.strip() == "")
            for v in raw.values()
        )
        if blank:
            continue

        row_errors: list[str] = []
        zone_raw = raw["zone"]
        zone = zone_raw.strip() if isinstance(zone_raw, str) else ""
        if not zone:
            row_errors.append(f"第 {row_no} 条规则：{FIELD_LABELS['zone']}不能为空")

        parsed: dict[str, int] = {}
        for key in ("priority",) + RANGE_KEYS:
            try:
                parsed[key] = coerce_int(raw[key])
            except (ValueError, TypeError):
                row_errors.append(
                    f"第 {row_no} 条规则“{zone or '?'}”：{FIELD_LABELS[key]}必须是整数"
                )

        if len(parsed) == 5:
            if parsed["wl_min"] > parsed["wl_max"]:
                row_errors.append(f"第 {row_no} 条规则“{zone}”：水位下限不能大于上限")
            if parsed["fl_min"] > parsed["fl_max"]:
                row_errors.append(f"第 {row_no} 条规则“{zone}”：流量下限不能大于上限")

        if row_errors:
            errors.extend(row_errors)
            continue

        rules.append(
            Rule(
                zone=zone,
                priority=parsed["priority"],
                wl_min=parsed["wl_min"],
                wl_max=parsed["wl_max"],
                fl_min=parsed["fl_min"],
                fl_max=parsed["fl_max"],
                ord=len(rules),
                id=_safe_int(item.get("id")),
            )
        )

    if not rules and not errors:
        errors.append("至少需要一条分区规则")
    return rules, errors


def _safe_int(v: Any) -> Optional[int]:
    try:
        return coerce_int(v) if v is not None and v != "" else None
    except ValueError:
        return None


def _structural_check(rng: Range, rules: Sequence[Rule]) -> list[str]:
    errors: list[str] = []
    if rng.wl_min > rng.wl_max or rng.fl_min > rng.fl_max:
        errors.append("量程上下界不合法")
    for i, r in enumerate(rules):
        label = f"规则“{r.zone}”(第 {i + 1} 条)"
        if not r.zone:
            errors.append(f"{label}：分区名为空")
        if r.wl_min > r.wl_max or r.fl_min > r.fl_max:
            errors.append(f"{label}：区间上下界不合法")
    return errors


def _outside_warnings(rng: Range, rules: Sequence[Rule]) -> list[str]:
    warnings: list[str] = []
    for r in rules:
        disjoint = (
            r.wl_max < rng.wl_min
            or r.wl_min > rng.wl_max
            or r.fl_max < rng.fl_min
            or r.fl_min > rng.fl_max
        )
        if disjoint:
            warnings.append(
                f"规则“{r.zone}”（优先级 {r.priority}）完全在有效量程之外，不参与核对"
            )
    return warnings


def _witness_dict(
    wx: int,
    wx_next: int,
    fy: int,
    fy_next: int,
    top: Optional[list[Rule]] = None,
) -> dict[str, Any]:
    w: dict[str, Any] = {
        "water_level": wx,
        "flow": fy,
        "wl_cell": [wx, wx_next - 1],
        "flow_cell": [fy, fy_next - 1],
        "reproduce": f"water_level={wx}&flow={fy}",
    }
    if top is not None:
        best = min(r.priority for r in top)
        ordered = sorted(top, key=lambda r: (r.ord if r.ord is not None else 1 << 30))
        w["priority"] = best
        w["top_zones"] = [r.zone for r in ordered]
        w["top_rules"] = [r.identity() for r in ordered]
    return w


def verify(rng: Range, rules: Sequence[Rule]) -> dict[str, Any]:
    """核对量程内“每对读数都有唯一最高优先级规则”。

    返回可 JSON 序列化的报告字典：
    ``ok`` 为真当且仅当没有结构性错误、没有漏分格、没有最高级并列格。
    ``gap`` / ``tie`` 各给出一个确定性的、可复现的代表读数对。
    """
    struct_errors = _structural_check(rng, rules)
    if struct_errors:
        return {
            "ok": False,
            "algorithm": "endpoint-coordinate-compression",
            "structural_errors": struct_errors,
            "warnings": [],
            "rule_count": len(rules),
            "elementary_cells": 0,
            "gap_cell_count": 0,
            "tie_cell_count": 0,
            "gap": None,
            "tie": None,
            "gap_samples": [],
            "tie_samples": [],
        }

    warnings = _outside_warnings(rng, rules)

    # 1) 端点坐标集合（闭区间右端 +1，转为半开整数段）。
    xs: set[int] = {rng.wl_min, rng.wl_max + 1}
    ys: set[int] = {rng.fl_min, rng.fl_max + 1}
    active: list[Rule] = []
    for r in rules:
        x0 = max(rng.wl_min, r.wl_min)
        x1 = min(rng.wl_max + 1, r.wl_max + 1)
        y0 = max(rng.fl_min, r.fl_min)
        y1 = min(rng.fl_max + 1, r.fl_max + 1)
        if x0 < x1 and y0 < y1:
            active.append(r)
            xs.add(x0)
            xs.add(x1)
            ys.add(y0)
            ys.add(y1)

    x_axis = sorted(xs)
    y_axis = sorted(ys)

    gap_samples: list[dict[str, Any]] = []
    tie_samples: list[dict[str, Any]] = []
    gap_cells = 0
    tie_cells = 0

    # 2) 扫描初等格。轴点有序 => 首个见证按坐标字典序确定，可复现。
    for xi in range(len(x_axis) - 1):
        wx, wx_next = x_axis[xi], x_axis[xi + 1]
        x_hits = [r for r in active if r.wl_min <= wx <= r.wl_max]
        for yi in range(len(y_axis) - 1):
            fy, fy_next = y_axis[yi], y_axis[yi + 1]
            hits = [r for r in x_hits if r.fl_min <= fy <= r.fl_max]

            if not hits:
                gap_cells += 1
                if len(gap_samples) < MAX_SAMPLES:
                    gap_samples.append(_witness_dict(wx, wx_next, fy, fy_next))
                continue

            best_priority = min(r.priority for r in hits)
            top = [r for r in hits if r.priority == best_priority]
            if len(top) >= 2:
                tie_cells += 1
                if len(tie_samples) < MAX_SAMPLES:
                    tie_samples.append(
                        _witness_dict(wx, wx_next, fy, fy_next, top=top)
                    )

    total_cells = (len(x_axis) - 1) * (len(y_axis) - 1)
    ok = gap_cells == 0 and tie_cells == 0
    return {
        "ok": ok,
        "algorithm": "endpoint-coordinate-compression",
        "priority_convention": "数值越小优先级越高，1 为最高",
        "structural_errors": [],
        "warnings": warnings,
        "rule_count": len(rules),
        "active_rule_count": len(active),
        "elementary_cells": total_cells,
        "gap_cell_count": gap_cells,
        "tie_cell_count": tie_cells,
        "gap": gap_samples[0] if gap_samples else None,
        "tie": tie_samples[0] if tie_samples else None,
        "gap_samples": gap_samples,
        "tie_samples": tie_samples,
    }


def coerce_pair(wl_raw: Any, fl_raw: Any) -> tuple[Optional[int], Optional[int], list[str]]:
    """解析网页 / API 传入的一对读数。"""
    errors: list[str] = []
    wl: Optional[int] = None
    fl: Optional[int] = None
    try:
        wl = coerce_int(wl_raw)
    except (ValueError, TypeError):
        errors.append("水位必须是整数")
    try:
        fl = coerce_int(fl_raw)
    except (ValueError, TypeError):
        errors.append("流量必须是整数")
    return wl, fl, errors


def classify_pair(rng: Range, rules: Sequence[Rule], wl: int, fl: int) -> dict[str, Any]:
    """按现行口径判定单对读数的归属。"""
    base: dict[str, Any] = {"water_level": wl, "flow": fl}

    if not rng.contains(wl, fl):
        axis = "water_level" if not (rng.wl_min <= wl <= rng.wl_max) else "flow"
        base.update(
            {
                "status": "out_of_range",
                "in_range": False,
                "axis": axis,
                "range": rng.to_dict(),
                "message": "读数超出有效量程，现行口径不覆盖该取值",
            }
        )
        return base

    hits = [r for r in rules if r.covers(wl, fl)]
    if not hits:
        base.update(
            {
                "status": "uncovered",
                "in_range": True,
                "message": "量程内该读数没有任何规则命中（现行口径异常，请联系发布员核对）",
                "matched": [],
            }
        )
        return base

    best_priority = min(r.priority for r in hits)
    top = [r for r in hits if r.priority == best_priority]
    matched = [
        r.identity()
        for r in sorted(
            hits,
            key=lambda r: (
                r.priority,
                r.ord if r.ord is not None else 1 << 30,
                r.id or 0,
            ),
        )
    ]

    if len(top) >= 2:
        ordered = sorted(top, key=lambda r: (r.ord if r.ord is not None else 1 << 30))
        base.update(
            {
                "status": "ambiguous",
                "in_range": True,
                "priority": best_priority,
                "tied_top_zones": [r.zone for r in ordered],
                "tied_top_rules": [r.identity() for r in ordered],
                "matched": matched,
                "message": "最高优先级出现并列，现行口径存在二义性",
            }
        )
        return base

    winner = top[0]
    base.update(
        {
            "status": "classified",
            "in_range": True,
            "zone": winner.zone,
            "priority": winner.priority,
            "rule_id": winner.id,
            "matched": matched,
            "message": f"归入「{winner.zone}」",
        }
    )
    return base
