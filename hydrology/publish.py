"""发布服务：量程 / 分区表单独或一并发布。

交错提交的核心规则
------------------
每笔发布在 ``BEGIN IMMEDIATE`` 事务内：

1. 读取 **届时生效** 的现行量程版本与分区表版本；
2. 确定本次待核口径：
   * ``ranges``：新量程 + 现行分区表（用另一项届时生效的版本复核）；
   * ``rules``：现行量程 + 新分区表；
   * ``bundle``：新量程 + 新分区表（成对复核）；
3. 跑坐标压缩核对；
4. 通过才写入新版本并移动 ``meta`` 现行指针；不通过则现行指针原样保留，
   只落一条 ``accepted=0`` 的核对记录。

读请求从不使用候选数据，因此核对期间网页查到的始终是已提交的现行归属。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from .db import connect, immediate, load_current, now_iso
from .domain import parse_range, parse_rules, verify

VALID_KINDS = ("ranges", "rules", "bundle")

KIND_LABELS = {
    "ranges": "仅量程修订",
    "rules": "仅分区表修订",
    "bundle": "量程 + 分区表一并发布",
    "seed": "初始播种",
}


def submit_publish(
    db_path: str,
    *,
    kind: str,
    range_data: Optional[Mapping[str, Any]] = None,
    rules_data: Any = None,
    note: str = "",
    source: str = "web",
) -> dict[str, Any]:
    """提交一笔发布。永远返回结构化结果，核对失败不抛异常。"""
    if kind not in VALID_KINDS:
        raise ValueError(f"未知发布类型: {kind!r}，应为 {VALID_KINDS}")

    note = (note or "").strip()[:200]
    candidate_json = json.dumps(
        {"kind": kind, "range": range_data, "rules": rules_data},
        ensure_ascii=False,
        default=str,
    )

    # ---- 1) 解析候选（事务外做纯计算，错误也落成 rejected 记录） ----------
    parse_errors: list[str] = []
    candidate_range = None
    candidate_rules = None

    if kind in ("ranges", "bundle"):
        candidate_range, errs = parse_range(range_data)
        parse_errors.extend(errs)
    if kind in ("rules", "bundle"):
        candidate_rules, errs = parse_rules(rules_data)
        parse_errors.extend(errs)

    conn = connect(db_path)
    try:
        with immediate(conn):
            current = load_current(conn)
            cur_range = current["range"]
            cur_rules = current["rules"]
            base_range_id = current["range_id"]
            base_rules_id = current["rules_id"]

            # 初始口径必须存在（init_database 已播种），否则无法交叉复核。
            if cur_range is None or not cur_rules:
                parse_errors.append("现行口径尚未初始化，无法复核发布")

            if parse_errors:
                report = {
                    "ok": False,
                    "structural_errors": parse_errors,
                    "warnings": [],
                    "gap": None,
                    "tie": None,
                }
                attempt_id = _insert_attempt(
                    conn,
                    kind=kind,
                    source=source,
                    accepted=False,
                    note=note,
                    range_id=None,
                    rules_id=None,
                    base_range_id=base_range_id,
                    base_rules_id=base_rules_id,
                    report=report,
                    candidate_json=candidate_json,
                )
                return {
                    "attempt_id": attempt_id,
                    "accepted": False,
                    "report": report,
                    "current_range_id": base_range_id,
                    "current_rules_id": base_rules_id,
                    "new_range_id": None,
                    "new_rules_id": None,
                }

            # ---- 2) 用“届时生效”的另一项版本拼出待核口径 ----------------
            if kind == "ranges":
                check_range = candidate_range
                check_rules = cur_rules
                checked_range_id = None  # 候选，尚未入库
                checked_rules_id = base_rules_id
            elif kind == "rules":
                check_range = cur_range
                check_rules = candidate_rules
                checked_range_id = base_range_id
                checked_rules_id = None
            else:  # bundle
                check_range = candidate_range
                check_rules = candidate_rules
                checked_range_id = None
                checked_rules_id = None

            report = verify(check_range, check_rules)
            report["checked_against"] = {
                "range_version_id": checked_range_id,
                "rules_version_id": checked_rules_id,
                "note": (
                    "单独发布的一类，已与另一项届时生效的现行版本交叉复核"
                    if kind != "bundle"
                    else "量程与分区表成对复核"
                ),
            }

            # ---- 3a) 核对失败：只记录，不动现行指针 -----------------------
            if not report["ok"]:
                attempt_id = _insert_attempt(
                    conn,
                    kind=kind,
                    source=source,
                    accepted=False,
                    note=note,
                    range_id=None,
                    rules_id=None,
                    base_range_id=base_range_id,
                    base_rules_id=base_rules_id,
                    report=report,
                    candidate_json=candidate_json,
                )
                return {
                    "attempt_id": attempt_id,
                    "accepted": False,
                    "report": report,
                    "current_range_id": base_range_id,
                    "current_rules_id": base_rules_id,
                    "new_range_id": None,
                    "new_rules_id": None,
                }

            # ---- 3b) 通过：写新版本并移动现行指针 -------------------------
            ts = now_iso()
            new_range_id = base_range_id
            new_rules_id = base_rules_id

            if kind in ("ranges", "bundle"):
                cur = conn.execute(
                    """INSERT INTO range_versions
                       (wl_min, wl_max, fl_min, fl_max, note, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        candidate_range.wl_min,
                        candidate_range.wl_max,
                        candidate_range.fl_min,
                        candidate_range.fl_max,
                        note or f"{KIND_LABELS[kind]}",
                        ts,
                    ),
                )
                new_range_id = cur.lastrowid

            if kind in ("rules", "bundle"):
                cur = conn.execute(
                    "INSERT INTO rules_versions (note, created_at) VALUES (?, ?)",
                    (note or KIND_LABELS[kind], ts),
                )
                new_rules_id = cur.lastrowid
                for r in candidate_rules:
                    conn.execute(
                        """INSERT INTO rules
                           (version_id, ord, zone, priority,
                            wl_min, wl_max, fl_min, fl_max)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            new_rules_id,
                            r.ord if r.ord is not None else 0,
                            r.zone,
                            r.priority,
                            r.wl_min,
                            r.wl_max,
                            r.fl_min,
                            r.fl_max,
                        ),
                    )

            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('current_range_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_range_id),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('current_rules_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_rules_id),),
            )

            attempt_id = _insert_attempt(
                conn,
                kind=kind,
                source=source,
                accepted=True,
                note=note,
                range_id=new_range_id,
                rules_id=new_rules_id,
                base_range_id=base_range_id,
                base_rules_id=base_rules_id,
                report=report,
                candidate_json=candidate_json,
            )
            return {
                "attempt_id": attempt_id,
                "accepted": True,
                "report": report,
                "current_range_id": new_range_id,
                "current_rules_id": new_rules_id,
                "new_range_id": new_range_id,
                "new_rules_id": new_rules_id,
            }
    finally:
        conn.close()


def _insert_attempt(
    conn,
    *,
    kind: str,
    source: str,
    accepted: bool,
    note: str,
    range_id: Optional[int],
    rules_id: Optional[int],
    base_range_id: Optional[int],
    base_rules_id: Optional[int],
    report: dict[str, Any],
    candidate_json: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO publish_attempts
           (created_at, kind, source, accepted, note,
            range_id, rules_id, base_range_id, base_rules_id,
            report_json, candidate_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now_iso(),
            kind,
            source,
            1 if accepted else 0,
            note,
            range_id,
            rules_id,
            base_range_id,
            base_rules_id,
            json.dumps(report, ensure_ascii=False),
            candidate_json,
        ),
    )
    return cur.lastrowid
