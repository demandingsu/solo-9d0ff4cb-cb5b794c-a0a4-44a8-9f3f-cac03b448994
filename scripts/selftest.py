#!/usr/bin/env python3
"""交付前自测 / 演示脚本。

用临时 SQLite 库 + Flask test client 实际跑：
  1. 现行口径摘要与若干对读数的分区判定（含洪水优先）；
  2. 漏分见证点、最高级并列见证点（各给可复现读数对与最高级分区）；
  3. 两类修订交错提交：失败驳回 -> 沿用现行 -> 换序成功；
  4. 核对进行期间连续查单条读数，确认网页/API 不受影响；
  5. 主要页面冒烟。

退出码 0 表示全部断言通过。
"""

from __future__ import annotations

import copy
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydrology import create_app  # noqa: E402
from hydrology.domain import Range, Rule, verify  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="hydrology-selftest-")
    db_path = str(Path(tmpdir) / "selftest.db")
    app = create_app({"TESTING": True, "DATABASE": db_path})
    client = app.test_client()
    print(f"临时 SQLite：{db_path}")

    # ------------------------------------------------------------------ #
    banner("1. 现行口径与分区判定")
    # ------------------------------------------------------------------ #
    current = client.get("/api/current").get_json()
    rng = current["range"]
    rules = current["rules"]
    print(
        f"现行量程：水位 {rng['water_level']}，流量 {rng['flow']}；"
        f"规则 {len(rules)} 条；版本 range#{current['range_version_id']} "
        f"rules#{current['rules_version_id']}"
    )

    cases = [
        (90, 900, "洪水样本区"),
        (85, 850, "洪水样本区（闭区间下界）"),
        (84, 849, "高水高流量区"),
        (82, 820, "高水高流量区"),
        (60, 500, "中水中流量区"),
        (10, 100, "低水低流量区"),
    ]
    for wl, fl, expected in cases:
        d = client.get(f"/api/classify?wl={wl}&flow={fl}").get_json()
        zone = d.get("zone")
        ok = zone == expected or (expected.endswith("）") and zone == expected.split("（")[0])
        check(
            f"判定 ({wl:>3}, {fl:>4}) -> {zone}",
            d.get("status") == "classified" and (zone == expected or expected.startswith(zone)),
            f"期望 {expected}",
        )

    out = client.get("/api/classify?wl=150&flow=100").get_json()
    check("超量程读数被识别", out["status"] == "out_of_range", out.get("message", ""))

    # ------------------------------------------------------------------ #
    banner("2. 核对：漏分 / 最高级并列的可复现见证")
    # ------------------------------------------------------------------ #
    gap_rules = [r for i, r in enumerate(copy.deepcopy(rules)) if i != 0]
    gap_resp = client.post("/api/publish/rules", json={"rules": gap_rules})
    gap_report = gap_resp.get_json()["report"]
    gw = gap_report["gap"]
    print(f"  漏分见证：水位={gw['water_level']}, 流量={gw['flow']}  ({gw['reproduce']})")
    check("漏分稿被驳回(422)", gap_resp.status_code == 422)
    check("漏分见证读数对为 (0,0)", (gw["water_level"], gw["flow"]) == (0, 0))
    again = verify(
        Range(0, 100, 0, 1000),
        [
            Rule(
                zone=r["zone"], priority=r["priority"],
                wl_min=r["wl_min"], wl_max=r["wl_max"],
                fl_min=r["fl_min"], fl_max=r["fl_max"], ord=r.get("ord"),
            )
            for r in gap_rules
        ],
    )
    check(
        "漏分见证可复现",
        (again["gap"]["water_level"], again["gap"]["flow"])
        == (gw["water_level"], gw["flow"]),
    )

    tie_rules = copy.deepcopy(rules)
    tie_rules.append(
        {
            "zone": "并列洪峰复核区", "priority": 1,
            "wl_min": 90, "wl_max": 100, "fl_min": 900, "fl_max": 1000,
        }
    )
    tie_resp = client.post("/api/publish/rules", json={"rules": tie_rules})
    tie_report = tie_resp.get_json()["report"]
    tw = tie_report["tie"]
    print(
        f"  并列见证：水位={tw['water_level']}, 流量={tw['flow']}，"
        f"最高级(p={tw['priority']})分区：{'、'.join(tw['top_zones'])}"
    )
    check("并列稿被驳回(422)", tie_resp.status_code == 422)
    check("并列见证读数对为 (90,900)", (tw["water_level"], tw["flow"]) == (90, 900))
    check(
        "并列列出两个最高级分区",
        set(tw["top_zones"]) == {"洪水样本区", "并列洪峰复核区"},
    )
    still = client.get("/api/classify?wl=90&flow=900").get_json()
    check("驳回后现行 (90,900) 仍唯一归洪水样本区", still.get("zone") == "洪水样本区")

    # ------------------------------------------------------------------ #
    banner("3. 交错提交：每笔发布前用另一项届时生效版本复核")
    # ------------------------------------------------------------------ #
    ids0 = client.get("/api/current").get_json()
    r0, v0 = ids0["range_version_id"], ids0["rules_version_id"]
    print(f"  起点现行指针：range#{r0}, rules#{v0}")

    # 3.1 先扩量程 -> 现行规则盖不住 -> 失败沿用现行
    resp = client.post(
        "/api/publish/ranges",
        json={"wl_min": 0, "wl_max": 200, "fl_min": 0, "fl_max": 1000,
              "note": "交错A：先扩量程"},
    )
    b = resp.get_json()
    ids1 = client.get("/api/current").get_json()
    check(
        "先扩量程被驳回（新条带漏分）",
        resp.status_code == 422 and b["report"]["gap"]["water_level"] == 101,
        f"见证 ({b['report']['gap']['water_level']}, {b['report']['gap']['flow']})",
    )
    check(
        "驳回后两个指针均不变",
        (ids1["range_version_id"], ids1["rules_version_id"]) == (r0, v0),
    )

    # 3.2 改为先发延伸规则（按现行量程 0..100 复核通过）
    extended = copy.deepcopy(rules)
    for r in extended:
        if r["wl_max"] == 100:
            r["wl_max"] = 200
    resp = client.post(
        "/api/publish/rules",
        json={"rules": extended, "note": "交错B：先延伸规则"},
    )
    ids2 = client.get("/api/current").get_json()
    check("单独发布延伸规则通过", resp.status_code == 200)
    check(
        "仅发规则：量程指针不动、规则指针前进",
        ids2["range_version_id"] == r0 and ids2["rules_version_id"] > v0,
        f"range#{ids2['range_version_id']} rules#{ids2['rules_version_id']}",
    )

    # 3.3 再发同一量程 -> 用届时生效的新规则复核 -> 通过
    resp = client.post(
        "/api/publish/ranges",
        json={"wl_min": 0, "wl_max": 200, "fl_min": 0, "fl_max": 1000,
              "note": "交错C：规则就位后再扩量程"},
    )
    ids3 = client.get("/api/current").get_json()
    check("规则就位后扩量程通过", resp.status_code == 200)
    check(
        "量程指针前进、规则指针保持",
        ids3["range_version_id"] > r0
        and ids3["rules_version_id"] == ids2["rules_version_id"],
    )
    verdict = client.get("/api/classify?wl=150&flow=900").get_json()
    check("新条带 (150,900) 归洪水样本区", verdict.get("zone") == "洪水样本区")

    # 3.4 一并发布但内部不匹配 -> 两个指针都不动
    ra, va = ids3["range_version_id"], ids3["rules_version_id"]
    resp = client.post(
        "/api/publish/bundle",
        json={
            "wl_min": 0, "wl_max": 400, "fl_min": 0, "fl_max": 1000,
            "rules": ids3["rules"], "note": "bundle 故意只扩量程",
        },
    )
    ids4 = client.get("/api/current").get_json()
    check("不匹配的一并发布被驳回", resp.status_code == 422)
    check(
        "bundle 驳回后两个指针都不动",
        (ids4["range_version_id"], ids4["rules_version_id"]) == (ra, va),
    )

    # ------------------------------------------------------------------ #
    banner("4. 核对期间网页/API 照常查现行归属")
    # ------------------------------------------------------------------ #
    big_range = Range(0, 200_000, 0, 200_000)
    heavy = [
        Rule(
            zone=f"压力规则{i}", priority=10_000 + i,
            wl_min=i * 500, wl_max=200_000 - i * 260,
            fl_min=i * 500, fl_max=200_000 - i * 260, ord=i,
        )
        for i in range(80)
    ]

    def heavy_worker():
        for _ in range(2):
            verify(big_range, heavy)

    t = threading.Thread(target=heavy_worker)
    t.start()
    during_ok = 0
    during_bad = 0
    started = time.perf_counter()
    while t.is_alive():
        d = client.get("/api/classify?wl=90&flow=900")
        if d.status_code == 200 and d.get_json().get("zone") == "洪水样本区":
            during_ok += 1
        else:
            during_bad += 1
    t.join()
    elapsed = time.perf_counter() - started
    # 线程结束后再补若干请求
    after_ok = 0
    for _ in range(20):
        d = client.get("/api/classify?wl=10&flow=100")
        if d.status_code == 200:
            after_ok += 1
    check(
        f"重型核对运行的 {elapsed:.2f}s 窗口内连续查算 {during_ok} 次全部正确"
        f"（失败 {during_bad}），窗口后 {after_ok}/20 正常",
        during_bad == 0 and during_ok >= 1 and after_ok == 20,
    )

    # ------------------------------------------------------------------ #
    banner("5. 页面冒烟")
    # ------------------------------------------------------------------ #
    for path, token in [
        ("/", "现行分区表"),
        ("/publish", "发布口径修订"),
        ("/attempts", "发布与核对记录"),
        ("/attempts/1", "初始播种"),
        ("/archive", "历史档案收料"),
    ]:
        html = client.get(path)
        ok = html.status_code == 200 and token.encode() in html.data
        check(f"GET {path} -> 200 且含“{token}”", ok)

    # ------------------------------------------------------------------ #
    banner("6. 历史档案：按来源保存、缺项、核对与查算")
    # ------------------------------------------------------------------ #
    arc_rules = [
        {"zone": "左岸区", "priority": 10, "wl_min": 0, "wl_max": 5,
         "fl_min": 0, "fl_max": 10},
        {"zone": "右岸区", "priority": 10, "wl_min": 6, "wl_max": 10,
         "fl_min": 0, "fl_max": 10},
        {"zone": "洪峰区", "priority": 1, "wl_min": 8, "wl_max": 10,
         "fl_min": 8, "fl_max": 10},
    ]
    pub = {"source": "站A档案室", "kind": "publish", "no": "F-1001",
           "content": {"range_no": "R-11", "rules_no": "G-11"}}
    rngm = {"source": "站A档案室", "kind": "range", "no": "R-11",
            "content": {"wl_min": 0, "wl_max": 10, "fl_min": 0, "fl_max": 10}}
    rlsm = {"source": "站A档案室", "kind": "rules", "no": "G-11",
            "content": {"rules": arc_rules}}

    r = client.post("/api/archive/materials", json=pub)
    body = r.get_json()
    rec = body["record"]
    check(
        "发布记录先到 -> 材料未齐并列出缺项",
        r.status_code == 200 and rec["state"] == "incomplete"
        and {m["kind"] for m in rec["missing"]} == {"range", "rules"},
        "缺：" + "、".join(m["label"].replace("缺", "") for m in rec["missing"]),
    )
    early = client.get(
        "/api/archive/classify?source=站A档案室&publish_no=F-1001&wl=9&flow=9"
    )
    check("材料未齐时查算被拒(409)", early.status_code == 409)

    client.post("/api/archive/materials", json=rngm)
    mid = client.get("/api/archive/records?source=站A档案室").get_json()["records"][0]
    check(
        "量程到 -> 仍缺分区表",
        mid["state"] == "incomplete" and [m["kind"] for m in mid["missing"]] == ["rules"],
    )

    r = client.post("/api/archive/materials", json=rlsm)
    body = r.get_json()
    check(
        "分区表到 -> 刚补齐并核对通过",
        body["material"]["created"]
        and len(body["completed"]) == 1
        and body["completed"][0]["state"] == "verified",
    )
    hit = client.get(
        "/api/archive/classify?source=站A档案室&publish_no=F-1001&wl=9&flow=9"
    ).get_json()
    check("核对通过后查原分区 -> 洪峰区", hit.get("zone") == "洪峰区")

    dup = client.post("/api/archive/materials", json=rlsm)
    check(
        "同来源同号同内容 -> 去重不新增",
        dup.status_code == 200 and dup.get_json()["material"]["deduplicated"],
    )

    conflict_payload = dict(rngm)
    conflict_payload["content"] = {
        "wl_min": 0, "wl_max": 20, "fl_min": 0, "fl_max": 10,
    }
    conflict = client.post("/api/archive/materials", json=conflict_payload)
    check(
        "同来源同号不同内容 -> 整份退回(409)",
        conflict.status_code == 409
        and conflict.get_json()["status"] == "conflict",
    )
    still = client.get(
        "/api/archive/classify?source=站A档案室&publish_no=F-1001&wl=9&flow=9"
    )
    check("冲突退回后已入库版本未变，仍可查算", still.status_code == 200)

    other = {"source": "站B水文站", "kind": "publish", "no": "F-1001",
             "content": {"range_no": "R-11", "rules_no": "G-11"}}
    r = client.post("/api/archive/materials", json=other)
    check(
        "不同来源允许重号，各自独立",
        r.status_code == 200
        and r.get_json()["record"]["source"] == "站B水文站"
        and r.get_json()["record"]["state"] == "incomplete",
    )

    bad_steps = [
        {"source": "站A档案室", "kind": "publish", "no": "F-1002",
         "content": {"range_no": "R-12", "rules_no": "G-12"}},
        {"source": "站A档案室", "kind": "range", "no": "R-12",
         "content": {"wl_min": 0, "wl_max": 10, "fl_min": 0, "fl_max": 10}},
        {"source": "站A档案室", "kind": "rules", "no": "G-12",
         "content": {"rules": [arc_rules[1], arc_rules[2]]}},
    ]
    for item in bad_steps:
        r = client.post("/api/archive/materials", json=item)
    body = r.get_json()
    failed_rec = body["completed"][0] if body.get("completed") else None
    check(
        "组合漏分 -> 核对未通过并给出原因，材料照收",
        r.status_code == 200
        and failed_rec is not None
        and failed_rec["state"] == "failed"
        and bool(failed_rec["reason"]["summary"]),
        (failed_rec or {}).get("reason", {}).get("summary", [""])[0],
    )
    failed_q = client.get(
        "/api/archive/classify?source=站A档案室&publish_no=F-1002&wl=0&flow=0"
    )
    check("核对未通过的档案不开放查算(409)", failed_q.status_code == 409)

    page = client.get("/archive")
    check(
        "档案页列出缺项",
        page.status_code == 200
        and "F-1001".encode() in page.data
        and "缺量程".encode() in page.data,
    )
    detail = client.get("/archive/records/1")
    check(
        "已核对档案详情页可查原分区",
        detail.status_code == 200 and "按历史口径查原分区".encode() in detail.data,
    )

    # ------------------------------------------------------------------ #
    banner("自测汇总")
    # ------------------------------------------------------------------ #
    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    if failed:
        for name in failed:
            print("  FAIL:", name)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
