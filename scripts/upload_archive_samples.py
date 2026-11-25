#!/usr/bin/env python3
"""历史档案分批上传样例：按顺序把 samples/archive/*.json 投给运行中的服务。

用法（服务已由 docker compose 或本地启动）：

    python3 scripts/upload_archive_samples.py                    # 默认 http://127.0.0.1:8000
    python3 scripts/upload_archive_samples.py http://localhost:8000

每一批是一个 JSON 数组，按文件名顺序逐批 POST /api/archive/materials。
预期：
  01 发布记录先到 -> 材料未齐，列出缺量程 / 缺分区表；
  02 量程到 -> 仍缺分区表；
  03 分区表到 -> F-1001、F-1002 刚补齐并核对通过；
  04 R-11 同号不同内容 -> 409 整份退回，已入库版本不变；
  05 站B 重号 -> 与站A互不影响，材料齐备后核对通过；
  06 F-1003 的分区表漏掉左岸条带 -> 核对未通过并给出原因，材料照收。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "samples" / "archive"
DEFAULT_BASE = "http://127.0.0.1:8000"


def post_batch(base: str, items: list) -> tuple[int, dict]:
    body = json.dumps(items, ensure_ascii=False).encode("utf-8")
    req = Request(
        base.rstrip("/") + "/api/archive/materials",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def summarize(status: int, payload: dict) -> str:
    lines = []
    for result in payload.get("results", []):
        idx = result.get("index", "?")
        if result.get("ok"):
            mat = result["material"]
            line = (
                f"  [{idx}] OK  {mat['kind_label']} {mat['source']}/{mat['no']} "
                f"({'新建' if mat['created'] else '去重'})"
            )
            for rec in result.get("completed", []):
                line += f"\n        刚补齐 {rec['source']}/{rec['publish_no']} -> {rec['state_label']}"
                for reason in (rec.get("reason") or {}).get("summary", []):
                    line += f"\n        原因：{reason}"
            lines.append(line)
        else:
            lines.append(
                f"  [{idx}] {result.get('status', 'error').upper()}  "
                + "；".join(result.get("errors", []))
            )
    if not lines:
        lines.append(f"  HTTP {status}: {json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else DEFAULT_BASE
    files = sorted(SAMPLE_DIR.glob("*.json"))
    if not files:
        print(f"未找到样例：{SAMPLE_DIR}")
        return 1

    failures = 0
    for path in files:
        items = json.loads(path.read_text(encoding="utf-8"))
        label = path.name
        try:
            status, payload = post_batch(base, items)
        except URLError as exc:
            print(f"✗ {label} 无法连接 {base}：{exc}")
            return 2
        print(f"\n=== {label} -> HTTP {status}")
        print(summarize(status, payload))
        # 04 预期 409（冲突退回），06 预期 207/200（材料照收，档案 failed）
        if path.name.startswith("04") and status != 409:
            print(f"  !! 预期 409 冲突退回，实际 {status}")
            failures += 1

    print("\n样例投递完成。请打开 /archive 查看按来源与发布号的缺项与核对状态。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
