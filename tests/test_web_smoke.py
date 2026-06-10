# -*- coding: utf-8 -*-
"""web 引擎冒煙測試 (smoke test) — 不靠外網,全程對 mock 站。

流程:
  1. 啟動 mock server(隨機 port)
  2. run_query_flow / RpaSerialSource:已知序號回正確 dict、未知序號回 None
  3. run_download_flow:下載出一個存在的 .xlsx

執行(專案根):
  PYTHONIOENCODING=utf-8 python tests/test_web_smoke.py
全綠回 exit 0;任一失敗 raise AssertionError 並 exit 1。
"""
from __future__ import annotations
import os
import sys
import json
import tempfile
import zipfile

# 把專案根加進 sys.path(本檔在 tests/ 下)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mock.server import MockServer
from engines.web.contracts import run_query_flow, run_download_flow, RpaSerialSource


FLOW_QUERY = os.path.join(ROOT, "flows", "web_query_demo.json")
FLOW_DOWNLOAD = os.path.join(ROOT, "flows", "web_download_demo.json")


def _load_flow_with_base(flow_path: str, base_url: str) -> dict:
    """讀 flow JSON 並把 base_url 設成 mock 站(隨機埠)。"""
    with open(flow_path, "r", encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("variables", {})["base_url"] = base_url
    return d


def _opts(tmpdir: str) -> dict:
    """共用 options:暫存 DB / 下載目錄 / headless。"""
    return {
        "db_path": os.path.join(tmpdir, "smoke.db"),
        "vault_dir": tmpdir,
        "screenshot_dir": os.path.join(tmpdir, "shots"),
        "session": {
            "headless": True,
            "download_dir": os.path.join(tmpdir, "downloads"),
        },
    }


def test_query_known_and_unknown(base_url: str, tmpdir: str):
    """已知序號回正確 dict;未知序號回 None。"""
    flow = _load_flow_with_base(FLOW_QUERY, base_url)
    opts = _opts(tmpdir)

    keys = ["SN12345", "NOPE-999"]
    result = run_query_flow(flow, keys, key_var="serial", options=opts)

    assert result["SN12345"] is not None, "已知序號不應為 None"
    info = result["SN12345"]
    assert info.get("status") == "正常出貨", f"status 不符: {info!r}"
    assert info.get("ship_date") == "2026-05-12", f"ship_date 不符: {info!r}"
    assert info.get("owner") == "王小明", f"owner 不符: {info!r}"

    assert result["NOPE-999"] is None, "未知序號應回 None"
    print("[OK] run_query_flow: 已知序號回正確 dict、未知序號回 None")


def test_serial_source(base_url: str, tmpdir: str):
    """RpaSerialSource.query 介面(專案2 adapter)。"""
    flow = _load_flow_with_base(FLOW_QUERY, base_url)
    # RpaSerialSource 吃 flow_path;這裡寫一份帶正確 base_url 的暫存 flow
    tmp_flow = os.path.join(tmpdir, "query_flow.json")
    with open(tmp_flow, "w", encoding="utf-8") as f:
        json.dump(flow, f, ensure_ascii=False)

    src = RpaSerialSource(tmp_flow, options=_opts(tmpdir))
    hit = src.query("SN67890")
    miss = src.query("DOES-NOT-EXIST")

    assert hit is not None and hit.get("status") == "維修中", f"adapter 命中錯誤: {hit!r}"
    assert miss is None, "adapter 查無應回 None"
    print("[OK] RpaSerialSource.query: 命中回 dict、查無回 None")


def test_download(base_url: str, tmpdir: str):
    """run_download_flow:下載出一個存在的 .xlsx。"""
    flow = _load_flow_with_base(FLOW_DOWNLOAD, base_url)
    opts = _opts(tmpdir)

    paths = run_download_flow(flow, variables={"month": "2026-05"}, options=opts)
    assert len(paths) == 1, f"應下載一個檔案,實得 {len(paths)}: {paths!r}"
    p = paths[0]
    assert os.path.exists(p), f"下載檔不存在: {p}"
    assert p.lower().endswith(".xlsx"), f"副檔名不是 .xlsx: {p}"
    assert os.path.getsize(p) > 0, "下載檔大小為 0"
    # .xlsx 即 zip;確認是合法 zip 容器
    assert zipfile.is_zipfile(p), f"下載檔不是合法 xlsx/zip: {p}"
    print(f"[OK] run_download_flow: 下載出存在的 .xlsx -> {p}")


def main():
    server = MockServer(port=0)
    server.start()
    base_url = server.base_url
    print(f"[mock] running at {base_url}")
    try:
        # ignore_cleanup_errors:SQLite WAL 殘留檔在 Windows 偶有檔案鎖,不阻斷測試
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            test_query_known_and_unknown(base_url, tmpdir)
            test_serial_source(base_url, tmpdir)
            test_download(base_url, tmpdir)
    finally:
        server.stop()
        print("[mock] stopped")
    print("\nALL GREEN ✔")


if __name__ == "__main__":
    main()
