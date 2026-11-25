"""服务端渲染页面：查算、发布编辑器、核对记录。"""

from __future__ import annotations

import json

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    url_for,
)

from .db import get_attempt, get_db, list_attempts, load_current
from .domain import RANGE_KEYS, coerce_pair, classify_pair
from .publish import KIND_LABELS, VALID_KINDS, submit_publish

bp = Blueprint("web", __name__)


@bp.app_template_filter("from_json")
def from_json_filter(text):
    try:
        return json.loads(text) if text else None
    except (ValueError, TypeError):
        return None


@bp.app_template_filter("kind_label")
def kind_label_filter(kind):
    return KIND_LABELS.get(kind, kind)


@bp.get("/")
def index():
    db = get_db()
    current = load_current(db)

    wl_raw = (request.args.get("wl") or "").strip()
    fl_raw = (request.args.get("flow") or "").strip()
    result = None

    if wl_raw or fl_raw:
        wl, fl, errors = coerce_pair(wl_raw or None, fl_raw or None)
        if errors:
            result = {"status": "bad_input", "errors": errors}
        elif current["range"] is None:
            result = {
                "status": "not_initialized",
                "errors": ["现行口径尚未初始化"],
            }
        else:
            result = classify_pair(current["range"], current["rules"], wl, fl)

    return render_template(
        "index.html",
        current=current,
        result=result,
        wl_raw=wl_raw,
        fl_raw=fl_raw,
    )


def _range_data_from_form(form) -> dict[str, str]:
    return {key: form.get(key, "") for key in RANGE_KEYS}


def _rules_data_from_form(form) -> list[dict[str, str]]:
    zones = form.getlist("zone")
    priorities = form.getlist("priority")
    rule_ids = form.getlist("rule_id")
    columns = {key: form.getlist(key) for key in RANGE_KEYS}
    rules: list[dict[str, str]] = []
    for i, zone in enumerate(zones):
        item = {
            "zone": zone,
            "priority": priorities[i] if i < len(priorities) else "",
            "id": rule_ids[i] if i < len(rule_ids) else "",
        }
        for key, values in columns.items():
            item[key] = values[i] if i < len(values) else ""
        rules.append(item)
    return rules


@bp.route("/publish", methods=["GET", "POST"])
def publish_page():
    if request.method == "POST":
        action = request.form.get("action", "")
        if action not in VALID_KINDS:
            return render_template(
                "publish.html",
                current=load_current(get_db()),
                editor_range=None,
                editor_rules=None,
                restore=None,
                fatal=f"无效的发布动作：{action!r}",
            ), 400

        result = submit_publish(
            current_app.config["DATABASE"],
            kind=action,
            range_data=_range_data_from_form(request.form),
            rules_data=_rules_data_from_form(request.form),
            note=request.form.get("note", ""),
            source="web",
        )
        return redirect(
            url_for("web.attempt_detail", attempt_id=result["attempt_id"])
        )

    current = load_current(get_db())
    editor_range = None
    editor_rules = None
    restore = None

    restore_id = request.args.get("restore", type=int)
    if restore_id:
        attempt = get_attempt(get_db(), restore_id)
        if attempt and attempt["candidate_json"]:
            candidate = json.loads(attempt["candidate_json"])
            restore = attempt
            editor_range = candidate.get("range")
            editor_rules = candidate.get("rules")

    return render_template(
        "publish.html",
        current=current,
        editor_range=editor_range,
        editor_rules=editor_rules,
        restore=restore,
        fatal=None,
    )


@bp.get("/attempts")
def attempts_page():
    rows = list_attempts(get_db(), limit=100)
    return render_template("attempts.html", attempts=rows)


@bp.get("/attempts/<int:attempt_id>")
def attempt_detail(attempt_id: int):
    attempt = get_attempt(get_db(), attempt_id)
    if attempt is None:
        return render_template("not_found.html"), 404
    report = json.loads(attempt["report_json"])
    candidate = (
        json.loads(attempt["candidate_json"]) if attempt["candidate_json"] else None
    )
    current = load_current(get_db())
    return render_template(
        "attempt_detail.html",
        attempt=attempt,
        report=report,
        candidate=candidate,
        current=current,
    )
