"""JSON API：查现行归属、取现行口径、三类发布、只核对不发布。"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .db import get_db, load_current
from .domain import coerce_pair, parse_range, parse_rules, classify_pair, verify
from .publish import submit_publish

bp = Blueprint("api", __name__)


def _current_payload():
    current = load_current(get_db())
    rng = current["range"]
    return {
        "range_version_id": current["range_id"],
        "rules_version_id": current["rules_id"],
        "range": rng.to_dict() if rng else None,
        "rules": [r.to_dict() for r in current["rules"]],
        "range_published_at": (current["range_meta"] or {}).get("created_at"),
        "rules_published_at": (current["rules_version_meta"] or {}).get("created_at"),
    }


@bp.get("/current")
def api_current():
    return jsonify(_current_payload())


@bp.get("/classify")
def api_classify():
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

    current = load_current(get_db())
    if current["range"] is None:
        return jsonify({"status": "not_initialized"}), 503

    return jsonify(classify_pair(current["range"], current["rules"], wl, fl))


def _extract_range(body: dict):
    nested = body.get("range")
    if isinstance(nested, dict):
        return nested
    return body


def _do_publish(kind: str):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"accepted": False, "errors": ["请求体必须是 JSON 对象"]}), 400

    result = submit_publish(
        current_app.config["DATABASE"],
        kind=kind,
        range_data=_extract_range(body),
        rules_data=body.get("rules"),
        note=str(body.get("note") or ""),
        source="api",
    )
    status = 200 if result["accepted"] else 422
    return jsonify(result), status


@bp.post("/publish/ranges")
def api_publish_ranges():
    return _do_publish("ranges")


@bp.post("/publish/rules")
def api_publish_rules():
    return _do_publish("rules")


@bp.post("/publish/bundle")
def api_publish_bundle():
    return _do_publish("bundle")


@bp.post("/verify")
def api_verify():
    """只核对候选口径，不写库、不影响现行口径。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "errors": ["请求体必须是 JSON 对象"]}), 400

    rng, range_errors = parse_range(_extract_range(body))
    rules, rule_errors = parse_rules(body.get("rules"))
    errors = range_errors + rule_errors
    if errors:
        return jsonify({"ok": False, "structural_errors": errors}), 422

    report = verify(rng, rules)
    return jsonify(report), (200 if report["ok"] else 422)
