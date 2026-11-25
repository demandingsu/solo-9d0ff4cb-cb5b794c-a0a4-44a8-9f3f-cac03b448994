"""历史档案测试：三类材料收料、判重/冲突、引用组合核对、查算开放门槛。"""

import copy
import json
import threading

import pytest

from hydrology.archive import (
    ArchiveConflictError,
    ArchiveInputError,
    ArchiveNotReadyError,
    classify_archived,
    find_record,
    save_material,
)
from hydrology.db import connect, load_current

GOOD_RANGE = {"wl_min": 0, "wl_max": 100, "fl_min": 0, "fl_max": 1000}
GOOD_RULES = [
    {"zone": "左岸", "priority": 10, "wl_min": 0, "wl_max": 49,
     "fl_min": 0, "fl_max": 1000},
    {"zone": "右岸", "priority": 10, "wl_min": 50, "wl_max": 100,
     "fl_min": 0, "fl_max": 1000},
    {"zone": "洪峰", "priority": 1, "wl_min": 90, "wl_max": 100,
     "fl_min": 900, "fl_max": 1000},
]


def _publish(source="S1", no="P1", range_no="R1", rules_no="G1", **content):
    return {
        "source": source,
        "kind": "publish",
        "no": no,
        "content": {"range_no": range_no, "rules_no": rules_no, **content},
    }


def _range(source="S1", no="R1", content=None):
    return {"source": source, "kind": "range", "no": no, "content": content or GOOD_RANGE}


def _rules(source="S1", no="G1", content=None):
    return {
        "source": source,
        "kind": "rules",
        "no": no,
        "content": copy.deepcopy(content or GOOD_RULES),
    }


# --------------------------------------------------------------------------- #
# 纯服务层
# --------------------------------------------------------------------------- #


def test_material_requires_source_kind_no_and_content(db_path):
    with pytest.raises(ArchiveInputError) as exc:
        save_material(db_path, {"source": "", "kind": "range", "no": "R1",
                                "content": GOOD_RANGE})
    assert any("来源" in e for e in exc.value.errors)

    with pytest.raises(ArchiveInputError) as exc:
        save_material(db_path, {"source": "S", "kind": "wat", "no": "x",
                                "content": GOOD_RANGE})
    assert any("类别" in e for e in exc.value.errors)

    with pytest.raises(ArchiveInputError):
        save_material(db_path, {"source": "S", "kind": "range", "no": "R",
                                "content": {"wl_min": 0, "wl_max": 1}})


def test_same_source_same_kind_same_no_is_deduplicated(db_path):
    first = save_material(db_path, _range(content={"wl_min": "0", "wl_max": 100,
                                                   "fl_min": 0, "fl_max": 1000}))
    assert first["material"]["created"] is True
    # 字符串形式整数会规范化成同一内容 -> 判为同号同内容
    second = save_material(db_path, _range())
    assert second["material"]["created"] is False
    assert second["material"]["deduplicated"] is True
    assert second["material"]["id"] == first["material"]["id"]


def test_conflict_rejects_whole_material_and_keeps_existing(db_path):
    save_material(db_path, _range())
    with pytest.raises(ArchiveConflictError) as exc:
        save_material(db_path, _range(content={"wl_min": 0, "wl_max": 50,
                                               "fl_min": 0, "fl_max": 1000}))
    assert exc.value.source == "S1" and exc.value.no == "R1"
    assert exc.value.existing_content["wl_max"] == 100

    # 已入库内容未被改写
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT content_json FROM archive_materials WHERE source='S1' AND kind='range' AND no='R1'"
        ).fetchone()
        assert json.loads(row["content_json"])["wl_max"] == 100
        count = conn.execute("SELECT COUNT(*) AS n FROM archive_materials").fetchone()["n"]
        assert count == 1
    finally:
        conn.close()


def test_same_no_allowed_across_sources(db_path):
    save_material(db_path, _range(source="S1", no="R1"))
    save_material(db_path, _range(source="S2", no="R1",
                                  content={"wl_min": 0, "wl_max": 10,
                                           "fl_min": 0, "fl_max": 10}))
    conn = connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM archive_materials WHERE kind='range' AND no='R1'"
        ).fetchone()["n"]
        assert n == 2
    finally:
        conn.close()


def test_publish_record_created_incomplete_and_missing_listed(db_path):
    out = save_material(db_path, _publish())
    assert out["record_created"] is True
    rec = out["record"]
    assert rec["state"] == "incomplete"
    assert {m["kind"] for m in rec["missing"]} == {"range", "rules"}
    assert out["completed"] == []


def test_rules_arriving_before_publish_do_not_verify_early(db_path):
    # 先到量程 + 分区表，再到发布记录：发布记录到达时才核对
    save_material(db_path, _range())
    out = save_material(db_path, _rules())
    assert out["completed"] == []
    assert out["records"] == []

    out = save_material(db_path, _publish())
    assert len(out["completed"]) == 1
    rec = out["completed"][0]
    assert rec["state"] == "verified"
    assert rec["report"]["ok"] is True


def test_out_of_order_then_complete_verifies_exact_referenced_combo(db_path):
    save_material(db_path, _publish(range_no="R2", rules_no="G2", note="引用二号"))
    # 一份无关的一号材料不应触发核对
    save_material(db_path, _range(no="R1"))
    save_material(db_path, _rules(no="G1"))
    conn = connect(db_path)
    try:
        assert find_record(conn, "S1", "P1")["state"] == "incomplete"
    finally:
        conn.close()

    # 二号量程到了，仍缺二号分区表
    out = save_material(
        db_path,
        _range(no="R2", content={"wl_min": 0, "wl_max": 50,
                                 "fl_min": 0, "fl_max": 500}),
    )
    assert out["completed"] == []
    # 二号分区表到齐 -> 按 R2 + G2 组合核对（与一号材料无关）
    out = save_material(
        db_path,
        _rules(no="G2", content=[{"zone": "全覆盖", "priority": 1,
                                  "wl_min": 0, "wl_max": 50,
                                  "fl_min": 0, "fl_max": 500}]),
    )
    assert len(out["completed"]) == 1
    rec = out["completed"][0]
    assert rec["state"] == "verified"
    assert rec["report"]["archived_criteria"]["range_no"] == "R2"
    assert rec["report"]["archived_criteria"]["rules_no"] == "G2"


def test_gap_combo_fails_with_reason_but_material_accepted(db_path):
    save_material(db_path, _publish())
    save_material(db_path, _range())
    bad = [r for i, r in enumerate(GOOD_RULES) if i != 0]  # 删掉左岸 -> 0 条带漏分
    out = save_material(db_path, _rules(content=bad))
    assert len(out["completed"]) == 1
    rec = out["completed"][0]
    assert rec["state"] == "failed"
    assert rec["reason"]["summary"]
    assert rec["report"]["gap"]["water_level"] == 0
    assert rec["report"]["gap"]["flow"] == 0
    # 材料本身照收
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM archive_materials WHERE kind='rules' AND no='G1'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()

    with pytest.raises(ArchiveNotReadyError):
        conn = connect(db_path)
        try:
            rec2 = find_record(conn, "S1", "P1")
            classify_archived(conn, rec2, 0, 0)
        finally:
            conn.close()


def test_tie_combo_fails_with_top_zones(db_path):
    save_material(db_path, _publish())
    save_material(db_path, _range())
    tied = GOOD_RULES + [{"zone": "并列洪峰", "priority": 1,
                          "wl_min": 90, "wl_max": 100,
                          "fl_min": 900, "fl_max": 1000}]
    out = save_material(db_path, _rules(content=tied))
    rec = out["completed"][0]
    assert rec["state"] == "failed"
    tie = rec["report"]["tie"]
    assert (tie["water_level"], tie["flow"]) == (90, 900)
    assert set(tie["top_zones"]) == {"洪峰", "并列洪峰"}
    assert any("并列" in line for line in rec["reason"]["summary"])


def test_verified_combo_classifies_gap_and_out_of_range(db_path):
    save_material(db_path, _publish())
    save_material(db_path, _range())
    save_material(db_path, _rules())
    conn = connect(db_path)
    try:
        rec = find_record(conn, "S1", "P1")
        hit = classify_archived(conn, rec, 95, 950)
        assert hit["status"] == "classified" and hit["zone"] == "洪峰"
        assert hit["archive"]["publish_no"] == "P1"
        out = classify_archived(conn, rec, 150, 1)
        assert out["status"] == "out_of_range"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 网页 / API
# --------------------------------------------------------------------------- #


def test_one_combo_completes_all_records_referencing_it(db_path):
    """同一量程 / 分区表被多条发布记录引用：材料到齐时全部补齐并各自核对。"""
    save_material(db_path, _publish(no="PA", range_no="R9", rules_no="G9"))
    save_material(db_path, _publish(no="PB", range_no="R9", rules_no="G9"))
    save_material(db_path, _range(no="R9"))
    out = save_material(db_path, _rules(no="G9"))
    assert {rec["publish_no"] for rec in out["completed"]} == {"PA", "PB"}
    assert all(rec["state"] == "verified" for rec in out["completed"])

    # 再次上传同内容不会重复核对（记录已非 incomplete）
    again = save_material(db_path, _rules(no="G9"))
    assert again["material"]["deduplicated"] is True
    assert again["completed"] == []


def test_failed_record_is_not_rechecked_by_duplicate_uploads(db_path):
    save_material(db_path, _publish())
    save_material(db_path, _range())
    bad = [r for i, r in enumerate(GOOD_RULES) if i != 0]
    first = save_material(db_path, _rules(content=bad))
    assert first["completed"][0]["state"] == "failed"
    checked_at = first["completed"][0]["checked_at"]

    repeat = save_material(db_path, _rules(content=bad))
    assert repeat["material"]["deduplicated"] is True
    assert repeat["completed"] == []
    conn = connect(db_path)
    try:
        rec = find_record(conn, "S1", "P1")
        assert rec["state"] == "failed"
        assert rec["checked_at"] == checked_at
    finally:
        conn.close()


def test_api_batch_preserves_order_and_isolates_failures(client):
    payload = [
        _publish(),
        {"source": "S1", "kind": "nope", "no": "x"},  # 无效
        _range(),
        _rules(),
    ]
    resp = client.post("/api/archive/materials", json=payload)
    assert resp.status_code == 207
    body = resp.get_json()
    assert body["batch"] is True
    assert [r["ok"] for r in body["results"]] == [True, False, True, True]
    # 最后一份分区表触发核对
    assert body["results"][3]["completed"][0]["state"] == "verified"


def test_api_conflict_status_409_and_dedupe_200(client):
    assert client.post("/api/archive/materials", json=_range()).status_code == 200
    again = client.post("/api/archive/materials", json=_range())
    assert again.status_code == 200
    assert again.get_json()["material"]["deduplicated"] is True

    conflict = client.post(
        "/api/archive/materials",
        json=_range(content={"wl_min": 0, "wl_max": 1, "fl_min": 0, "fl_max": 1}),
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["status"] == "conflict"


def test_api_classify_refuses_until_verified(client):
    client.post("/api/archive/materials", json=_publish())
    early = client.get("/api/archive/classify?source=S1&publish_no=P1&wl=1&flow=1")
    assert early.status_code == 409
    assert early.get_json()["status"] == "not_verified"

    client.post("/api/archive/materials", json=_range())
    client.post("/api/archive/materials", json=_rules())
    ok = client.get("/api/archive/classify?source=S1&publish_no=P1&wl=95&flow=950")
    assert ok.status_code == 200
    assert ok.get_json()["zone"] == "洪峰"


def test_api_classify_unknown_record_404(client):
    resp = client.get("/api/archive/classify?source=NA&publish_no=X&wl=1&flow=1")
    assert resp.status_code == 404


def test_web_page_upload_batch_and_lists_missing(client):
    # JSON API 收发布记录，网页列出缺项
    client.post("/api/archive/materials", json=_publish())
    page = client.get("/archive")
    assert page.status_code == 200
    html = page.data.decode()
    assert "P1" in html and "缺量程" in html and "缺分区表" in html

    # 网页表单一次粘贴一批（含冲突项）
    batch = json.dumps([_range(), _rules()])
    resp = client.post("/archive", data={"payload": batch})
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "刚补齐" in html and "核对通过" in html

    detail = client.get("/archive/records/1")
    assert detail.status_code == 200
    assert "按历史口径查原分区".encode() in detail.data

    # 网页查算
    res = client.post("/archive/records/1", data={"wl": "95", "flow": "950"})
    assert res.status_code == 200
    assert "洪峰".encode() in res.data


def test_web_page_bad_json_reports_error(client):
    resp = client.post("/archive", data={"payload": "{not json"})
    assert resp.status_code == 400
    assert "JSON 解析失败".encode() in resp.data


def test_web_conflict_shows_both_versions(client):
    client.post("/api/archive/materials", json=_range())
    resp = client.post(
        "/archive",
        data={"payload": json.dumps(_range(content={"wl_min": 0, "wl_max": 7,
                                                    "fl_min": 0, "fl_max": 7}))},
    )
    assert resp.status_code == 409
    html = resp.data.decode()
    assert "整份退回" in html and "已入库" in html and "本次上传" in html


def test_archive_intake_does_not_touch_current_criteria(client):
    before = client.get("/api/current").get_json()
    save = [
        _publish(), _range(), _rules(),
    ]
    for item in save:
        client.post("/api/archive/materials", json=item)
    after = client.get("/api/current").get_json()
    assert before == after
    verdict = client.get("/api/classify?wl=90&flow=900").get_json()
    assert verdict["zone"] == "洪水样本区"


def test_queries_work_while_archive_verification_is_heavy(client, monkeypatch):
    """核对事务进行中，现行读数与已通过核对的档案照常可查；新档案未提交前查不到。"""
    import hydrology.archive as archive_mod
    from hydrology.domain import verify as real_verify

    db_path = client.application.config["DATABASE"]

    # 先建一份已核对通过的档案，供核对期间查询
    save_material(db_path, _publish(no="PV", range_no="RV", rules_no="GV"))
    save_material(db_path, _range(no="RV"))
    save_material(db_path, _rules(no="GV"))
    rec = find_record(connect(db_path), "S1", "PV")
    assert rec["state"] == "verified"

    # 另一份档案：发布记录 + 量程已到，等分区表；分区表一到就进入核对
    save_material(db_path, _publish(no="PS", range_no="RS", rules_no="GS"))
    save_material(db_path, _range(no="RS"))

    # 让这次核对在事务内停留一段时间，模拟重型压缩核对
    checking = threading.Event()
    release = threading.Event()

    def slow_verify(rng, rules):
        checking.set()
        release.wait(timeout=10)
        return real_verify(rng, rules)

    monkeypatch.setattr(archive_mod, "verify", slow_verify)

    errors: list[str] = []

    def upload_rules():
        try:
            save_material(db_path, _rules(no="GS"))
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    worker = threading.Thread(target=upload_rules)
    worker.start()
    assert checking.wait(timeout=10), "核对未按预期开始"

    try:
        # 核对事务尚未提交：现行读数与已核对档案照常可查
        for _ in range(10):
            rv = client.get("/api/classify?wl=90&flow=900")
            assert rv.status_code == 200 and rv.get_json()["zone"] == "洪水样本区"
            av = client.get(
                "/api/archive/classify?source=S1&publish_no=PV&wl=95&flow=950"
            )
            assert av.status_code == 200 and av.get_json()["zone"] == "洪峰"
            # 正在核对的新档案尚未开放
            nv = client.get(
                "/api/archive/classify?source=S1&publish_no=PS&wl=95&flow=950"
            )
            assert nv.status_code == 409
    finally:
        release.set()
        worker.join(timeout=20)

    assert not worker.is_alive()
    assert errors == []
    after = client.get("/api/archive/classify?source=S1&publish_no=PS&wl=95&flow=950")
    assert after.status_code == 200
    assert after.get_json()["zone"] == "洪峰"


def test_concurrent_uploads_complete_once_and_never_expose_early(client):
    """并发上传不同材料：每条记录只核对一次，且通过前查算一律 409。"""
    db_path = client.application.config["DATABASE"]

    # 30 条互不相干的档案，每条引用自己的 combination
    for i in range(30):
        save_material(db_path, _publish(no=f"P{i}", range_no=f"R{i}", rules_no=f"G{i}"))

    seen_states = []
    errors = []

    # 每份材料另起短连接，避免线程共享同一 sqlite3 连接
    def upload_range(i):
        try:
            save_material(db_path, _range(no=f"R{i}"))
        except Exception as exc:  # pragma: no cover - 失败时给断言信息
            errors.append(exc)

    def upload_rules(i):
        try:
            save_material(db_path, _rules(no=f"G{i}"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = []
    for i in range(30):
        threads.append(threading.Thread(target=upload_range, args=(i,)))
        threads.append(threading.Thread(target=upload_rules, args=(i,)))
    for t in threads:
        t.start()

    # 并发上传期间反复查算：未通过前必须 409，通过后必须正确
    pending_checks = 0
    while any(t.is_alive() for t in threads):
        for i in range(30):
            resp = client.get(
                f"/api/archive/classify?source=S1&publish_no=P{i}&wl=95&flow=950"
            )
            if resp.status_code == 200:
                seen_states.append(("ok", i, resp.get_json().get("zone")))
            else:
                assert resp.status_code == 409, resp.data
                seen_states.append(("pending", i, None))
                pending_checks += 1
    assert pending_checks > 0, "并发窗口内应至少观察到一次“未通过前不可查”"

    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    assert errors == []

    # 全部核对通过且可查
    for i in range(30):
        resp = client.get(
            f"/api/archive/classify?source=S1&publish_no=P{i}&wl=95&flow=950"
        )
        assert resp.status_code == 200, (i, resp.data)
        assert resp.get_json()["zone"] == "洪峰"

    # 任何一次成功的查算都必须基于已核对内容
    assert all(z == "洪峰" for tag, _, z in seen_states if tag == "ok")

    # 每条记录只核对一次：report 中的 archived_criteria 与自身引用一致
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT publish_no, range_no, rules_no, state, report_json FROM archive_records ORDER BY id"
        ).fetchall()
        assert len(rows) == 30
        for row in rows:
            assert row["state"] == "verified"
            report = json.loads(row["report_json"])
            criteria = report["archived_criteria"]
            assert criteria["publish_no"] == row["publish_no"]
            assert criteria["range_no"] == row["range_no"]
            assert criteria["rules_no"] == row["rules_no"]
    finally:
        conn.close()

    # 并发上传没有污染现行口径
    current = load_current(connect(db_path))
    assert current["range"].wl_max == 100
