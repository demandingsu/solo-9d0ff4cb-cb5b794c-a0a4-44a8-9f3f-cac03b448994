"""历史档案：按来源库号接收发布记录 / 量程 / 分区表，材料齐备后按引用组合核对。

材料主键
--------
``(来源库号 source, 类别 kind, 原编号 no)``。不同来源可以出现相同编号；
同一来源、同一类别、同一编号只保留一份：

* 再次收到**同内容** -> 幂等去重，不新增行；
* 再次收到**不同内容** -> 整份退回（不写材料、不动记录、不核对）。

三类材料
--------
``publish`` 发布记录，``no`` 为发布原编号，内容引用同一来源下的
``range_no``（量程原版本号）与 ``rules_no``（分区表原版本号）；
``range`` 量程，``no`` 为原版本号；``rules`` 分区表，``no`` 为原版本号。

核对时机
--------
每次材料入库都在 ``BEGIN IMMEDIATE`` 写事务内完成。写事务会找出因本次入库
而**刚补齐**的档案记录（发布记录存在且引用的量程、分区表都已到齐），
逐条按各自引用的组合跑坐标压缩核对。材料写入、核对结果、``verified`` 状态
与事务一起提交：并发上传时既不会漏核，也不会在核对完成前把档案开放查算。
核对期间 WAL 读者（现行读数、已通过核对的档案）照常可读。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping, Optional, Sequence

from .db import connect, immediate, now_iso
from .domain import (
    Range,
    Rule,
    classify_pair,
    parse_range,
    parse_rules,
    verify,
)

ARCHIVE_KINDS = ("publish", "range", "rules")

ARCHIVE_KIND_LABELS = {
    "publish": "发布记录",
    "range": "量程",
    "rules": "分区表",
}

ARCHIVE_STATE_LABELS = {
    "incomplete": "材料未齐",
    "failed": "核对未通过",
    "verified": "核对通过 · 可查原分区",
}

_KIND_ALIASES = {
    "publish": "publish",
    "发布": "publish",
    "发布记录": "publish",
    "record": "publish",
    "range": "range",
    "量程": "range",
    "rules": "rules",
    "rule": "rules",
    "分区表": "rules",
    "分区规则": "rules",
}

_ENVELOPE_KEYS = {"source", "kind", "no", "content"}
_RANGE_KEYS = ("wl_min", "wl_max", "fl_min", "fl_max")


class ArchiveInputError(ValueError):
    """上传的 JSON 不符合材料格式；整份拒收。"""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("；".join(self.errors))


class ArchiveConflictError(Exception):
    """同来源、同类别、同编号已存在且内容不同；整份退回。"""

    def __init__(
        self,
        *,
        source: str,
        kind: str,
        no: str,
        existing: Mapping[str, Any],
        incoming_content: Any,
    ) -> None:
        self.source = source
        self.kind = kind
        self.no = no
        self.existing = dict(existing)
        self.incoming_content = incoming_content
        self.existing_content = _loads(existing.get("content_json"), None)
        self.message = (
            f"来源 {source} 的{ARCHIVE_KIND_LABELS.get(kind, kind)}"
            f"（编号 {no}）已存在且内容不同，整份退回；未写入任何数据。"
        )
        super().__init__(self.message)


class ArchiveNotReadyError(Exception):
    """档案尚未核对通过，不能查原分区。"""

    def __init__(self, record: Mapping[str, Any]) -> None:
        self.record = dict(record)
        self.message = (
            f"档案 {record.get('source')} / 发布号 {record.get('publish_no')} "
            f"当前状态为“{ARCHIVE_STATE_LABELS.get(record.get('state'), record.get('state'))}”，"
            "核对通过前不开放查算。"
        )
        super().__init__(self.message)


# --------------------------------------------------------------------------- #
# 规范化与校验
# --------------------------------------------------------------------------- #


def _loads(text: Any, default: Any) -> Any:
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(obj: Any) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def _clean_key(value: Any, label: str, *, max_len: int = 100) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label}必须是非空字符串或整数")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(f"{label}必须是非空字符串或整数")
    if not text:
        raise ValueError(f"{label}不能为空")
    if len(text) > max_len:
        raise ValueError(f"{label}长度不能超过 {max_len} 个字符")
    return text


def _clean_kind(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("类别 kind 必须是 publish / range / rules 之一")
    text = value.strip()
    kind = _KIND_ALIASES.get(text) or _KIND_ALIASES.get(text.lower())
    if kind is None:
        raise ValueError(
            "类别 kind 必须是 publish（发布记录）/ range（量程）/ rules（分区表）之一"
        )
    return kind


def normalize_material(payload: Any) -> dict[str, Any]:
    """把三类材料收成统一的入库结构；格式错误抛 ``ArchiveInputError``。"""
    if not isinstance(payload, Mapping):
        raise ArchiveInputError(["上传内容必须是 JSON 对象"])

    errors: list[str] = []
    source = kind = no = ""
    try:
        source = _clean_key(payload.get("source"), "来源库号 source", max_len=64)
    except ValueError as exc:
        errors.append(str(exc))
    try:
        kind = _clean_kind(payload.get("kind"))
    except ValueError as exc:
        errors.append(str(exc))
    no_raw = payload.get("no")
    if no_raw in (None, ""):
        no_raw = payload.get("publish_no") if kind == "publish" else None
    if no_raw in (None, ""):
        no_raw = payload.get("version_no")
    try:
        no = _clean_key(no_raw, "原编号 no", max_len=64)
    except ValueError as exc:
        errors.append(str(exc))

    if errors:
        raise ArchiveInputError(errors)

    raw_content = payload.get("content")
    content: Any = raw_content if raw_content is not None else payload

    if kind == "publish":
        normalized_content = _normalize_publish(content, payload)
    elif kind == "range":
        normalized_content = _normalize_range(content)
    else:
        normalized_content = _normalize_rules(content, payload)

    return {
        "source": source,
        "kind": kind,
        "no": no,
        "content": normalized_content,
        "canonical_json": _canonical_json(normalized_content),
        "digest": _digest(normalized_content),
        "raw_json": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
    }


def _content_mapping(content: Any) -> Mapping[str, Any]:
    return content if isinstance(content, Mapping) else {}


def _without_envelope(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if k not in _ENVELOPE_KEYS}


def _normalize_range(content: Any) -> dict[str, int]:
    if isinstance(content, Mapping) and "content" in content:
        content = content.get("content")
    if isinstance(content, Mapping):
        source_map = _without_envelope(content)
        # 允许 {"range": {...}} 的嵌套写法
        if isinstance(source_map.get("range"), Mapping):
            source_map = source_map["range"]
    else:
        source_map = {}
    rng, errs = parse_range(source_map)
    if errs or rng is None:
        raise ArchiveInputError([f"量程内容：{e}" for e in (errs or ["格式不正确"])])
    return {
        "wl_min": rng.wl_min,
        "wl_max": rng.wl_max,
        "fl_min": rng.fl_min,
        "fl_max": rng.fl_max,
    }


def _normalize_rules(content: Any, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    items: Any = None
    if isinstance(content, list):
        items = content
    elif isinstance(content, Mapping):
        if isinstance(content.get("rules"), list):
            items = content["rules"]
        elif isinstance(content.get("content"), Mapping):
            nested = content["content"]
            items = nested.get("rules")
        else:
            items = content.get("rules")
    if items is None:
        items = payload.get("rules")

    rules, errs = parse_rules(items)
    if errs:
        raise ArchiveInputError([f"分区表内容：{e}" for e in errs])
    return [
        {
            "zone": r.zone,
            "priority": r.priority,
            "wl_min": r.wl_min,
            "wl_max": r.wl_max,
            "fl_min": r.fl_min,
            "fl_max": r.fl_max,
        }
        for r in rules
    ]


def _normalize_publish(content: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    mapping = _content_mapping(content)
    if not mapping:
        mapping = payload

    range_no_raw = mapping.get("range_no", mapping.get("range_version_no"))
    rules_no_raw = mapping.get("rules_no", mapping.get("rules_version_no"))
    errors: list[str] = []
    try:
        range_no = _clean_key(range_no_raw, "引用的量程原版本号 range_no", max_len=64)
    except ValueError as exc:
        errors.append(str(exc))
    try:
        rules_no = _clean_key(rules_no_raw, "引用的分区表原版本号 rules_no", max_len=64)
    except ValueError as exc:
        errors.append(str(exc))
    if errors:
        raise ArchiveInputError(errors)

    # 除引用号、四字段（假如误带）与信封字段外，其余作为发布记录元数据留存，
    # 参与“同号同内容”判定。
    meta = _without_envelope(mapping)
    for key in ("range_no", "range_version_no", "rules_no", "rules_version_no"):
        meta.pop(key, None)
    for key in _RANGE_KEYS + ("rules", "range"):
        meta.pop(key, None)

    return {
        "range_no": range_no,
        "rules_no": rules_no,
        "meta": meta,
    }


# --------------------------------------------------------------------------- #
# 读取材料内容 -> 领域对象
# --------------------------------------------------------------------------- #


def _range_from_content(content: Mapping[str, Any]) -> Range:
    return Range(
        wl_min=int(content["wl_min"]),
        wl_max=int(content["wl_max"]),
        fl_min=int(content["fl_min"]),
        fl_max=int(content["fl_max"]),
    )


def _rules_from_content(content: Sequence[Mapping[str, Any]]) -> list[Rule]:
    return [
        Rule(
            zone=str(item["zone"]),
            priority=int(item["priority"]),
            wl_min=int(item["wl_min"]),
            wl_max=int(item["wl_max"]),
            fl_min=int(item["fl_min"]),
            fl_max=int(item["fl_max"]),
            ord=idx,
        )
        for idx, item in enumerate(content)
    ]


# --------------------------------------------------------------------------- #
# 入库与核对
# --------------------------------------------------------------------------- #


def save_material(db_path: str, payload: Any) -> dict[str, Any]:
    """接收一份材料；返回材料、受影响记录与本次刚补齐的核对结果。"""
    norm = normalize_material(payload)

    conn = connect(db_path)
    try:
        with immediate(conn):
            existing = conn.execute(
                "SELECT * FROM archive_materials WHERE source=? AND kind=? AND no=?",
                (norm["source"], norm["kind"], norm["no"]),
            ).fetchone()

            if existing is not None and existing["digest"] != norm["digest"]:
                raise ArchiveConflictError(
                    source=norm["source"],
                    kind=norm["kind"],
                    no=norm["no"],
                    existing=dict(existing),
                    incoming_content=norm["content"],
                )

            if existing is not None:
                material_id = int(existing["id"])
                created = False
            else:
                cur = conn.execute(
                    """INSERT INTO archive_materials
                       (source, kind, no, content_json, digest, raw_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        norm["source"],
                        norm["kind"],
                        norm["no"],
                        norm["canonical_json"],
                        norm["digest"],
                        norm["raw_json"],
                        now_iso(),
                    ),
                )
                material_id = int(cur.lastrowid)
                created = True

            affected: list[int] = []
            primary_record_id: Optional[int] = None

            if norm["kind"] == "publish":
                record_id, record_created = _ensure_record(
                    conn, norm, material_id
                )
                primary_record_id = record_id
                affected.append(record_id)
            else:
                column = "range_no" if norm["kind"] == "range" else "rules_no"
                rows = conn.execute(
                    f"""SELECT id FROM archive_records
                        WHERE source=? AND {column}=? AND state='incomplete'
                        ORDER BY id""",
                    (norm["source"], norm["no"]),
                ).fetchall()
                affected.extend(int(r["id"]) for r in rows)

            # 所有刚补齐的记录：材料齐 -> 按各自引用的组合核对。
            checked: list[int] = []
            for record_id in dict.fromkeys(affected):
                if _try_complete_record(conn, record_id):
                    checked.append(record_id)

            keys = _material_key_set(conn)
            records = [
                _record_detail(conn, rid, material_keys=keys)
                for rid in dict.fromkeys(affected)
            ]
            completed = [
                _record_detail(conn, rid, material_keys=keys) for rid in checked
            ]
            primary = (
                _record_detail(conn, primary_record_id, material_keys=keys)
                if primary_record_id is not None
                else None
            )

            result = {
                "material": {
                    "id": material_id,
                    "source": norm["source"],
                    "kind": norm["kind"],
                    "kind_label": ARCHIVE_KIND_LABELS[norm["kind"]],
                    "no": norm["no"],
                    "digest": norm["digest"],
                    "created": created,
                    "deduplicated": not created,
                },
                "record_created": (
                    record_created if norm["kind"] == "publish" else False
                ),
                "record": primary,
                "records": records,
                "completed": completed,
            }
            return result
    finally:
        conn.close()


def _ensure_record(
    conn: sqlite3.Connection,
    norm: Mapping[str, Any],
    material_id: int,
) -> tuple[int, bool]:
    """为发布记录建立 / 定位档案记录；同号引用不同已在入库前被拒收。"""
    content = norm["content"]
    row = conn.execute(
        "SELECT * FROM archive_records WHERE source=? AND publish_no=?",
        (norm["source"], norm["no"]),
    ).fetchone()
    if row is not None:
        # 材料判重已保证同号同内容，引用号自然一致；仍做防御式断言。
        if (
            row["range_no"] != content["range_no"]
            or row["rules_no"] != content["rules_no"]
        ):
            raise ArchiveConflictError(
                source=norm["source"],
                kind=norm["kind"],
                no=norm["no"],
                existing=dict(row),
                incoming_content=content,
            )
        return int(row["id"]), False

    ts = now_iso()
    cur = conn.execute(
        """INSERT INTO archive_records
           (source, publish_no, publish_material_id, range_no, rules_no,
            state, report_json, reason_json, checked_at, verified_at,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'incomplete', NULL, NULL, NULL, NULL, ?, ?)""",
        (
            norm["source"],
            norm["no"],
            material_id,
            content["range_no"],
            content["rules_no"],
            ts,
            ts,
        ),
    )
    return int(cur.lastrowid), True


def _try_complete_record(conn: sqlite3.Connection, record_id: int) -> bool:
    """若引用的量程与分区表都到齐且记录仍未核对，则按该组合核对。

    返回是否在本次调用中真正执行了核对。所有写操作都在调用方的写事务内。
    """
    record = conn.execute(
        "SELECT * FROM archive_records WHERE id=?", (record_id,)
    ).fetchone()
    if record is None or record["state"] != "incomplete":
        return False

    range_row = _material_row(conn, record["source"], "range", record["range_no"])
    rules_row = _material_row(conn, record["source"], "rules", record["rules_no"])
    if range_row is None or rules_row is None:
        return False

    rng = _range_from_content(_loads(range_row["content_json"], {}))
    rules = _rules_from_content(_loads(rules_row["content_json"], []))
    report = verify(rng, rules)
    report["archived_criteria"] = {
        "source": record["source"],
        "publish_no": record["publish_no"],
        "range_no": record["range_no"],
        "rules_no": record["rules_no"],
        "range_material_id": int(range_row["id"]),
        "rules_material_id": int(rules_row["id"]),
    }

    ts = now_iso()
    if report["ok"]:
        state = "verified"
        reason_json = None
        verified_at = ts
    else:
        state = "failed"
        reason_json = json.dumps(
            _build_reason(report), ensure_ascii=False
        )
        verified_at = None

    conn.execute(
        """UPDATE archive_records
           SET state=?, report_json=?, reason_json=?, checked_at=?,
               verified_at=?, updated_at=?
           WHERE id=? AND state='incomplete'""",
        (
            state,
            json.dumps(report, ensure_ascii=False),
            reason_json,
            ts,
            verified_at,
            ts,
            record_id,
        ),
    )
    return True


def _build_reason(report: Mapping[str, Any]) -> dict[str, Any]:
    lines: list[str] = []
    for err in report.get("structural_errors") or []:
        lines.append(f"结构问题：{err}")
    gap = report.get("gap")
    if gap:
        lines.append(
            "漏分：水位 {wl}、流量 {fl} 没有任何规则命中（问题初等格 {n} 个）".format(
                wl=gap.get("water_level"),
                fl=gap.get("flow"),
                n=report.get("gap_cell_count", 0),
            )
        )
    tie = report.get("tie")
    if tie:
        zones = "、".join(tie.get("top_zones") or [])
        lines.append(
            "最高级并列：水位 {wl}、流量 {fl} 同时命中 {zones}"
            "（优先级 {p}，问题初等格 {n} 个）".format(
                wl=tie.get("water_level"),
                fl=tie.get("flow"),
                zones=zones or "多条规则",
                p=tie.get("priority"),
                n=report.get("tie_cell_count", 0),
            )
        )
    if not lines:
        lines.append("材料到齐，但组合核对未通过")
    return {"summary": lines, "report": report}


def _material_row(
    conn: sqlite3.Connection, source: str, kind: str, no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM archive_materials WHERE source=? AND kind=? AND no=?",
        (source, kind, no),
    ).fetchone()


# --------------------------------------------------------------------------- #
# 查询 / 列表
# --------------------------------------------------------------------------- #


def _record_detail(
    conn: sqlite3.Connection,
    record_id: int,
    material_keys: Optional[set[tuple[str, str, str]]] = None,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM archive_records WHERE id=?", (record_id,)
    ).fetchone()
    if row is None:
        return None

    if material_keys is None:
        material_keys = _material_key_set(conn)

    missing: list[dict[str, str]] = []
    if (row["source"], "range", row["range_no"]) not in material_keys:
        missing.append(
            {"kind": "range", "no": row["range_no"], "label": "缺量程"}
        )
    if (row["source"], "rules", row["rules_no"]) not in material_keys:
        missing.append(
            {"kind": "rules", "no": row["rules_no"], "label": "缺分区表"}
        )

    detail = dict(row)
    detail["missing"] = missing
    detail["state_label"] = ARCHIVE_STATE_LABELS.get(row["state"], row["state"])
    detail["report"] = _loads(row["report_json"], None)
    detail["reason"] = _loads(row["reason_json"], None)
    return detail


def _material_key_set(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT source, kind, no FROM archive_materials"
    ).fetchall()
    return {(r["source"], r["kind"], r["no"]) for r in rows}


def list_archive_records(
    conn: sqlite3.Connection,
    *,
    source: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM archive_records"
    clauses: list[str] = []
    params: list[Any] = []
    if source:
        clauses.append("source=?")
        params.append(source)
    if state:
        clauses.append("state=?")
        params.append(state)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY source, publish_no, id LIMIT ?"
    params.append(limit)

    keys = _material_key_set(conn)
    rows = conn.execute(sql, params).fetchall()
    return [_record_detail(conn, int(r["id"]), material_keys=keys) for r in rows]


def list_archive_materials(
    conn: sqlite3.Connection,
    *,
    source: Optional[str] = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """已收材料清单（按来源、类别、编号），便于查看没有对应发布记录的材料。"""
    sql = (
        "SELECT id, source, kind, no, digest, created_at FROM archive_materials"
    )
    params: list[Any] = []
    if source:
        sql += " WHERE source=?"
        params.append(source)
    sql += " ORDER BY source, kind, no LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            **dict(r),
            "kind_label": ARCHIVE_KIND_LABELS.get(r["kind"], r["kind"]),
        }
        for r in rows
    ]


def get_archive_record(
    conn: sqlite3.Connection, record_id: int
) -> Optional[dict[str, Any]]:
    detail = _record_detail(conn, record_id)
    if detail is None:
        return None
    detail["range_content"] = None
    detail["rules_content"] = None
    if not detail["missing"]:
        rrow = _material_row(conn, detail["source"], "range", detail["range_no"])
        srow = _material_row(conn, detail["source"], "rules", detail["rules_no"])
        if rrow is not None:
            detail["range_content"] = _loads(rrow["content_json"], None)
            detail["range_material_id"] = int(rrow["id"])
        if srow is not None:
            detail["rules_content"] = _loads(srow["content_json"], None)
            detail["rules_material_id"] = int(srow["id"])
    return detail


def find_record(
    conn: sqlite3.Connection, source: str, publish_no: str
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT id FROM archive_records WHERE source=? AND publish_no=?",
        (source, publish_no),
    ).fetchone()
    if row is None:
        return None
    return get_archive_record(conn, int(row["id"]))


def classify_archived(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    wl: int,
    fl: int,
) -> dict[str, Any]:
    """按某份已通过核对的档案（其引用的量程 + 分区表）查原分区。"""
    if record.get("state") != "verified":
        raise ArchiveNotReadyError(record)

    range_content = record.get("range_content")
    rules_content = record.get("rules_content")
    if not range_content or not rules_content:
        raise ArchiveNotReadyError(record)

    rng = _range_from_content(range_content)
    rules = _rules_from_content(rules_content)
    result = classify_pair(rng, rules, wl, fl)
    result["archive"] = {
        "source": record["source"],
        "publish_no": record["publish_no"],
        "range_no": record["range_no"],
        "rules_no": record["rules_no"],
        "record_id": record["id"],
        "verified_at": record.get("verified_at"),
    }
    return result
