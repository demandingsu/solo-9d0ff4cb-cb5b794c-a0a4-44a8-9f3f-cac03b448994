"""历史档案页面：JSON 收料、按来源/发布号列出缺项、核对后查原分区。"""

from __future__ import annotations

import json

from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
)

from .archive import (
    ARCHIVE_KIND_LABELS,
    ArchiveConflictError,
    ArchiveInputError,
    ArchiveNotReadyError,
    classify_archived,
    get_archive_record,
    list_archive_materials,
    list_archive_records,
    save_material,
)
from .db import get_db
from .domain import coerce_pair

bp = Blueprint("archive", __name__)


def _load_upload() -> tuple[object, str]:
    """从 JSON 请求体、textarea 或上传文件里取出待收材料。"""
    if request.is_json:
        body = request.get_json(silent=True)
        if body is None:
            raise ArchiveInputError(["请求体不是合法 JSON"])
        return body, ""

    uploaded = request.files.get("file")
    text = ""
    if uploaded is not None and uploaded.filename:
        raw = uploaded.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ArchiveInputError(["上传文件必须是 UTF-8 编码的 JSON"])
    else:
        text = (request.form.get("payload") or "").strip()

    if not text:
        raise ArchiveInputError(["请粘贴 JSON 内容或选择 JSON 文件"])
    try:
        return json.loads(text), text
    except ValueError as exc:
        raise ArchiveInputError([f"JSON 解析失败：{exc}"])


def _submit_materials(payload: object) -> list[dict]:
    """单份或数组（分批中的一批）逐份入库；每份独立事务。"""
    items = payload if isinstance(payload, list) else [payload]
    results: list[dict] = []
    for idx, item in enumerate(items):
        entry: dict = {"index": idx + 1}
        try:
            entry["ok"] = True
            entry["result"] = save_material(current_app.config["DATABASE"], item)
            entry["summary"] = _material_summary(entry["result"])
        except ArchiveInputError as exc:
            entry["ok"] = False
            entry["status"] = "invalid"
            entry["errors"] = exc.errors
            entry["summary"] = "格式不正确，整份退回"
        except ArchiveConflictError as exc:
            entry["ok"] = False
            entry["status"] = "conflict"
            entry["conflict"] = {
                "source": exc.source,
                "kind": exc.kind,
                "kind_label": ARCHIVE_KIND_LABELS.get(exc.kind, exc.kind),
                "no": exc.no,
                "existing_content": exc.existing_content,
                "incoming_content": exc.incoming_content,
            }
            entry["summary"] = exc.message
        results.append(entry)
    return results


def _material_summary(result: dict) -> str:
    mat = result["material"]
    base = (
        f"已收{mat['kind_label']}（来源 {mat['source']}，编号 {mat['no']}）"
        if mat["created"]
        else f"同号同内容，已去重：{mat['kind_label']}（来源 {mat['source']}，编号 {mat['no']}）"
    )
    if result["completed"]:
        base += f"；刚补齐并完成核对 {len(result['completed'])} 条档案"
    return base


@bp.route("/archive", methods=["GET", "POST"])
def archive_page():
    upload_error: list[str] | None = None
    upload_text = ""
    results: list[dict] | None = None
    status = 200

    if request.method == "POST":
        try:
            payload, upload_text = _load_upload()
        except ArchiveInputError as exc:
            upload_error = exc.errors
            upload_text = (request.form.get("payload") or "").strip()
            status = 400
        else:
            results = _submit_materials(payload)
            if len(results) == 1 and not results[0]["ok"]:
                status = 409 if results[0].get("status") == "conflict" else 400

    db = get_db()
    source_filter = (request.args.get("source") or "").strip()
    records = list_archive_records(db, source=source_filter or None)
    materials = list_archive_materials(db, source=source_filter or None)

    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record["source"], []).append(record)

    return (
        render_template(
            "archive.html",
            grouped=grouped,
            materials=materials,
            source_filter=source_filter,
            results=results,
            upload_error=upload_error,
            upload_text=upload_text,
        ),
        status,
    )


@bp.route("/archive/records/<int:record_id>", methods=["GET", "POST"])
def archive_detail(record_id: int):
    db = get_db()
    record = get_archive_record(db, record_id)
    if record is None:
        return render_template("not_found.html"), 404

    wl_raw = (request.values.get("wl") or "").strip()
    fl_raw = (request.values.get("flow") or "").strip()
    query_result = None
    query_errors: list[str] | None = None

    if request.method == "POST" or wl_raw or fl_raw:
        wl, fl, errors = coerce_pair(wl_raw or None, fl_raw or None)
        if errors:
            query_errors = errors
        elif record["state"] != "verified":
            query_errors = [record["state_label"] + "，核对通过前不开放查算"]
        else:
            try:
                query_result = classify_archived(db, record, wl, fl)
            except ArchiveNotReadyError as exc:
                query_errors = [str(exc)]

    return render_template(
        "archive_detail.html",
        record=record,
        query_result=query_result,
        query_errors=query_errors,
        wl_raw=wl_raw,
        fl_raw=fl_raw,
    )
