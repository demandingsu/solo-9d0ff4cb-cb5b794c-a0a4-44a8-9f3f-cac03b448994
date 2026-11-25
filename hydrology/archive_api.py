"""历史档案 JSON API：收料、列缺项、查原分区。"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .archive import (
    ARCHIVE_KIND_LABELS,
    ARCHIVE_STATE_LABELS,
    ArchiveConflictError,
    ArchiveInputError,
    classify_archived,
    find_record,
    get_archive_record,
    list_archive_records,
    save_material,
)
from .db import get_db
from .domain import coerce_pair

bp = Blueprint("archive_api", __name__)


@bp.post("/materials")
def api_save_material():
    body = request.get_json(silent=True)
    if body is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "status": "invalid",
                    "errors": ["请求体必须是 JSON（对象，或对象数组）"],
                }
            ),
            400,
        )

    if isinstance(body, list):
        results = []
        for idx, item in enumerate(body, start=1):
            result = _save_one(item, index=idx)
            results.append(result)
        failed = [r for r in results if not r["ok"]]
        status = 207 if failed and len(failed) < len(results) else (
            400 if failed else 200
        )
        return jsonify({"ok": not failed, "batch": True, "results": results}), status

    result = _save_one(body)
    if result["ok"]:
        return jsonify(result), 200
    return jsonify(result), (409 if result.get("status") == "conflict" else 400)


def _save_one(payload, index: int | None = None) -> dict:
    try:
        result = save_material(current_app.config["DATABASE"], payload)
    except ArchiveInputError as exc:
        out = {"ok": False, "status": "invalid", "errors": exc.errors}
    except ArchiveConflictError as exc:
        out = {
            "ok": False,
            "status": "conflict",
            "errors": [exc.message],
            "conflict": {
                "source": exc.source,
                "kind": exc.kind,
                "kind_label": ARCHIVE_KIND_LABELS.get(exc.kind, exc.kind),
                "no": exc.no,
                "existing_content": exc.existing_content,
                "incoming_content": exc.incoming_content,
            },
        }
    else:
        out = {"ok": True, **result}
        out["material"]["kind_label"] = ARCHIVE_KIND_LABELS.get(
            out["material"]["kind"], out["material"]["kind"]
        )
        for record in out["records"] + out["completed"]:
            record["state_label"] = ARCHIVE_STATE_LABELS.get(
                record["state"], record["state"]
            )
        if index is not None:
            out["index"] = index
    if index is not None:
        out["index"] = index
    return out


@bp.get("/records")
def api_list_records():
    source = (request.args.get("source") or "").strip() or None
    state = (request.args.get("state") or "").strip() or None
    rows = list_archive_records(get_db(), source=source, state=state)
    return jsonify(
        {
            "records": [
                {
                    "id": r["id"],
                    "source": r["source"],
                    "publish_no": r["publish_no"],
                    "range_no": r["range_no"],
                    "rules_no": r["rules_no"],
                    "state": r["state"],
                    "state_label": r["state_label"],
                    "missing": r["missing"],
                    "checked_at": r["checked_at"],
                    "verified_at": r["verified_at"],
                }
                for r in rows
            ]
        }
    )


@bp.get("/records/<int:record_id>")
def api_record(record_id: int):
    record = get_archive_record(get_db(), record_id)
    if record is None:
        return jsonify({"ok": False, "errors": ["档案记录不存在"]}), 404
    return jsonify(
        {
            "ok": True,
            "record": {
                "id": record["id"],
                "source": record["source"],
                "publish_no": record["publish_no"],
                "range_no": record["range_no"],
                "rules_no": record["rules_no"],
                "state": record["state"],
                "state_label": record["state_label"],
                "missing": record["missing"],
                "report": record["report"],
                "reason": record["reason"],
                "checked_at": record["checked_at"],
                "verified_at": record["verified_at"],
                "range_content": record.get("range_content"),
                "rules_content": record.get("rules_content"),
            },
        }
    )


@bp.get("/classify")
def api_classify_archived():
    source = (request.args.get("source") or "").strip()
    publish_no = (request.args.get("publish_no") or "").strip()
    if not source or not publish_no:
        return (
            jsonify(
                {
                    "status": "bad_request",
                    "errors": ["必须提供 source（来源库号）与 publish_no（发布原编号）"],
                }
            ),
            400,
        )

    wl_raw = request.args.get("wl")
    fl_raw = request.args.get("flow")
    if wl_raw is None or fl_raw is None:
        return (
            jsonify(
                {
                    "status": "bad_request",
                    "errors": ["必须同时提供查询参数 wl（水位）和 flow（流量）"],
                }
            ),
            400,
        )

    wl, fl, errors = coerce_pair(wl_raw, fl_raw)
    if errors:
        return jsonify({"status": "bad_input", "errors": errors}), 400

    db = get_db()
    record = find_record(db, source, publish_no)
    if record is None:
        return (
            jsonify(
                {
                    "status": "not_found",
                    "errors": [
                        f"未找到来源 {source} 发布号 {publish_no} 的档案记录"
                    ],
                }
            ),
            404,
        )

    if record["state"] != "verified":
        return (
            jsonify(
                {
                    "status": "not_verified",
                    "state": record["state"],
                    "state_label": record["state_label"],
                    "missing": record["missing"],
                    "reason": record["reason"],
                    "message": "核对通过前不开放查算",
                }
            ),
            409,
        )

    return jsonify(classify_archived(db, record, wl, fl))
