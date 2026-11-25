"""发布链路测试：交错修订的交叉复核、失败沿用现行、网页与 API。"""

import copy
import threading
import time

from hydrology.domain import Range, Rule, verify


def _current_ids(client):
    d = client.get("/api/current").get_json()
    return d["range_version_id"], d["rules_version_id"], d


def test_seed_current_endpoint_and_index(client):
    data = client.get("/api/current").get_json()
    assert data["range"]["wl_max"] == 100
    assert len(data["rules"]) == 10
    assert data["range_version_id"] == 1
    assert data["rules_version_id"] == 1

    resp = client.get("/?wl=90&flow=900")
    assert resp.status_code == 200
    assert "洪水样本区".encode() in resp.data


def test_classify_api_validation(client):
    missing = client.get("/api/classify?wl=1")
    assert missing.status_code == 400

    bad = client.get("/api/classify?wl=abc&flow=10")
    assert bad.status_code == 400
    assert bad.get_json()["status"] == "bad_input"

    ok = client.get("/api/classify?wl=10&flow=100")
    assert ok.status_code == 200
    assert ok.get_json()["zone"] == "低水低流量区"


def test_interleaved_expand_range_requires_new_rules_first(client):
    rid0, vid0, cur = _current_ids(client)
    seed_rules = cur["rules"]

    # 第一笔：先单独扩大量程 -> 现行分区表盖不住新条带 -> 驳回
    resp = client.post(
        "/api/publish/ranges",
        json={"wl_min": 0, "wl_max": 200, "fl_min": 0, "fl_max": 1000},
    )
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["accepted"] is False
    assert body["report"]["gap"] is not None
    # 新露出的第一条带从水位 101 开始，扫描首个漏分格即 (101, 0)
    assert body["report"]["gap"]["water_level"] == 101

    rid1, vid1, _ = _current_ids(client)
    assert (rid1, vid1) == (rid0, vid0), "驳回后现行版本指针必须不变"

    # 第二笔：先把所有顶到 100 的水位区间延伸到 200，单独发分区表。
    # 用届时生效的现行量程 0..100 复核：只是越界延伸，口径不变 -> 通过
    extended = copy.deepcopy(seed_rules)
    for r in extended:
        if r["wl_max"] == 100:
            r["wl_max"] = 200
    resp = client.post("/api/publish/rules", json={"rules": extended})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["accepted"] is True

    rid2, vid2, cur2 = _current_ids(client)
    assert rid2 == rid0, "仅发分区表不应移动量程版本"
    assert vid2 > vid1, "分区表版本应前进"

    # 第三笔：再扩量程，此时用届时生效的新分区表复核 -> 通过
    resp = client.post(
        "/api/publish/ranges",
        json={"wl_min": 0, "wl_max": 200, "fl_min": 0, "fl_max": 1000},
    )
    assert resp.status_code == 200, resp.get_json()
    rid3, vid3, _ = _current_ids(client)
    assert rid3 > rid2 and vid3 == vid2

    # 新条带可正常判定：延伸后的洪水样本区盖住 (150, 900)
    verdict = client.get("/api/classify?wl=150&flow=900").get_json()
    assert verdict["status"] == "classified"
    assert verdict["zone"] == "洪水样本区"


def test_bad_rules_publish_keeps_current_and_queryable(client):
    _, vid0, cur = _current_ids(client)
    broken = [r for i, r in enumerate(cur["rules"]) if i != 0]

    resp = client.post("/api/publish/rules", json={"rules": broken})
    assert resp.status_code == 422
    report = resp.get_json()["report"]
    assert report["gap"]["water_level"] == 0
    assert report["gap"]["flow"] == 0

    _, vid1, cur_after = _current_ids(client)
    assert vid1 == vid0
    assert len(cur_after["rules"]) == 10

    # 核对 / 驳回期间与之后，网页查单条读数始终是现行归属
    verdict = client.get("/api/classify?wl=0&flow=0").get_json()
    assert verdict["zone"] == "低水低流量区"


def test_tied_rules_publish_rejected_and_lists_zones(client):
    _, vid0, cur = _current_ids(client)
    rules = cur["rules"]
    rules.append(
        {
            "zone": "并列洪峰区",
            "priority": 1,
            "wl_min": 90,
            "wl_max": 100,
            "fl_min": 900,
            "fl_max": 1000,
        }
    )
    resp = client.post("/api/publish/rules", json={"rules": rules})
    assert resp.status_code == 422
    tie = resp.get_json()["report"]["tie"]
    assert (tie["water_level"], tie["flow"]) == (90, 900)
    assert "洪水样本区" in tie["top_zones"]
    assert "并列洪峰区" in tie["top_zones"]

    _, vid1, _ = _current_ids(client)
    assert vid1 == vid0
    verdict = client.get("/api/classify?wl=90&flow=900").get_json()
    assert verdict["zone"] == "洪水样本区"  # 现行口径仍是唯一最高


def test_bundle_failure_moves_neither_pointer(client):
    rid0, vid0, cur = _current_ids(client)
    resp = client.post(
        "/api/publish/bundle",
        json={
            "wl_min": 0,
            "wl_max": 300,
            "fl_min": 0,
            "fl_max": 1000,
            "rules": cur["rules"],
            "note": "故意只扩量程不扩规则",
        },
    )
    assert resp.status_code == 422
    assert resp.get_json()["report"]["gap"] is not None
    rid1, vid1, _ = _current_ids(client)
    assert (rid1, vid1) == (rid0, vid0)


def test_verify_endpoint_does_not_persist(client):
    resp = client.post(
        "/api/verify",
        json={
            "wl_min": 0,
            "wl_max": 4,
            "fl_min": 0,
            "fl_max": 4,
            "rules": [
                {"zone": "全盖", "priority": 1, "wl_min": 0, "wl_max": 4,
                 "fl_min": 0, "fl_max": 4}
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    rid, vid, _ = _current_ids(client)
    assert (rid, vid) == (1, 1)


def test_form_publish_prg_and_attempt_pages(client):
    # 合法的“仅量程”收缩：流量上限 1000 -> 900，是现行口径子集，应通过
    resp = client.post(
        "/publish",
        data={
            "action": "ranges",
            "wl_min": "0",
            "wl_max": "100",
            "fl_min": "0",
            "fl_max": "900",
            "note": "表单收缩流量量程",
        },
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/attempts/" in location

    detail = client.get(location)
    assert detail.status_code == 200
    assert "核对通过".encode() in detail.data

    current = client.get("/api/current").get_json()
    assert current["range"]["fl_max"] == 900

    # 非法整数表单：同样 PRG 到记录页并显示结构错误
    resp = client.post(
        "/publish",
        data={
            "action": "ranges",
            "wl_min": "abc",
            "wl_max": "100",
            "fl_min": "0",
            "fl_max": "900",
        },
    )
    assert resp.status_code == 302
    detail_path = resp.headers["Location"]
    detail = client.get(detail_path)
    assert "必须是整数".encode() in detail.data
    assert "驳回".encode() in detail.data

    # 载回此稿：候选值应回到编辑器
    restore_path = "/publish?restore=" + detail_path.rstrip("/").split("/")[-1]
    restored = client.get(restore_path)
    assert restored.status_code == 200
    assert b'name="wl_min"' in restored.data
    assert b'value="abc"' in restored.data

    assert client.get("/attempts").status_code == 200
    assert client.get("/publish").status_code == 200


def test_queries_keep_working_while_heavy_check_runs(client):
    # 构造端点很多、重叠很多的大口径，让核对持续一小段时间
    big_range = Range(0, 100_000, 0, 100_000)
    heavy = []
    for i in range(70):
        heavy.append(
            Rule(
                zone=f"密集规则{i}",
                priority=1000 + i,
                wl_min=i * 400,
                wl_max=100_000 - i * 150,
                fl_min=i * 400,
                fl_max=100_000 - i * 150,
                ord=i,
            )
        )

    errors = []

    def run_checks():
        verify(big_range, heavy)

    worker = threading.Thread(target=run_checks)
    worker.start()

    statuses = []
    zones = []
    for _ in range(60):
        rv = client.get("/api/classify?wl=90&flow=900")
        statuses.append(rv.status_code)
        if rv.status_code == 200:
            zones.append(rv.get_json().get("zone"))
        else:
            errors.append(rv.data)
        time.sleep(0.002)

    worker.join(timeout=30)
    assert not worker.is_alive()
    assert errors == []
    assert statuses and all(s == 200 for s in statuses)
    assert zones and all(z == "洪水样本区" for z in zones)
