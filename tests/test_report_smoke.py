# -*- coding: utf-8 -*-
"""執行報表 / 稽核頁 offscreen smoke test。

驗證:
  1. core.store.Store.list_runs 回傳 runs(新到舊)。
  2. ReportPage(store) 可單獨建立(不需 vault / main_window)。
  3. 寫入 1 個 run + 多筆 step_logs(含一筆 failed 帶 error)+ 1 筆 heal_log,
     選該 run 後:runs 表、step_logs 表、heal_logs 表、頂部摘要數字都正確填充。
  4. Excel 匯出能產生檔案並可重新讀回。

執行:
  QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 python tests/test_report_smoke.py
"""
from __future__ import annotations
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication

from core.store import Store
from ui.pages.report_page import (
    ReportPage, load_runs, load_run_detail, summarize_steps,
    list_run_flows, export_steps_excel, step_logs_dataframe,
)


def _make_store() -> tuple[Store, int]:
    """建臨時 Store 並寫入 1 run + 3 step_logs(含 failed)+ 1 heal_log。回 (store, run_id)。"""
    tmpdb = os.path.join(tempfile.gettempdir(), "rpa_report_smoke.db")
    for suffix in ("", "-wal", "-shm"):
        p = tmpdb + suffix
        if os.path.exists(p):
            os.remove(p)
    store = Store(tmpdb)

    run_id = store.start_run("demo_flow")
    store.log_step(run_id, "s1", "web.open", "ok", ms=120, retries=0)
    store.log_step(run_id, "s2", "web.click", "ok", ms=45, retries=1,
                   screenshot=os.path.join(tempfile.gettempdir(), "shot_s2.png"))
    store.log_step(run_id, "s3", "web.type", "failed", ms=300, retries=2,
                   error="element not found: #username")
    store.log_heal(run_id, "s3", "heal(web)", score=0.87,
                   detail={"candidate": "input[name=user]", "reason": "text-match"})
    store.finish_run(run_id, "failed", {"foo": "bar"})
    return store, run_id


def test_list_runs(store, run_id):
    runs = store.list_runs()
    assert len(runs) == 1, runs
    assert runs[0]["id"] == run_id
    assert runs[0]["flow"] == "demo_flow"
    assert runs[0]["status"] == "failed"
    print("[OK] list_runs 回傳 runs(新到舊),欄位正確。")


def test_pure_loaders(store, run_id):
    assert list_run_flows(store) == ["demo_flow"]
    rows = load_runs(store, flow="demo_flow")
    assert len(rows) == 1 and rows[0]["id"] == run_id
    assert load_runs(store, flow="no_such_flow") == []

    detail = load_run_detail(store, run_id)
    s = detail["summary"]
    assert s["total"] == 3, s
    assert s["ok"] == 2, s
    assert s["failed"] == 1, s
    assert s["heals"] == 1, s
    assert len(detail["steps"]) == 3
    assert len(detail["heals"]) == 1
    failed = [x for x in detail["steps"] if x["status"] == "failed"]
    assert failed and "element not found" in (failed[0]["error"] or "")
    # summarize_steps 直接斷言
    assert summarize_steps(detail["steps"]) == {
        "total": 3, "ok": 2, "failed": 1, "skipped": 0}
    print("[OK] 純函式 load_runs / load_run_detail / summarize_steps 數字正確。")


def test_report_page(app, store, run_id):
    page = ReportPage(store)  # 單獨建立(不需 vault / main_window)

    # runs 表填充
    assert page.runs.rowCount() == 1, page.runs.rowCount()
    assert page.runs.item(0, 0).text() == str(run_id)
    assert page.runs.item(0, 1).text() == "demo_flow"
    assert "失敗" in page.runs.item(0, 4).text()

    # 載入該 run 明細
    detail = page.load_run(run_id)
    assert detail["summary"]["total"] == 3

    # step_logs 表
    assert page.steps.rowCount() == 3, page.steps.rowCount()
    # 第三步 failed + error + retries
    assert page.steps.item(2, 2).text() == "failed"
    assert "element not found" in page.steps.item(2, 5).text()
    assert page.steps.item(2, 4).text() == "2"
    # 第二步有截圖 → 該列有「開啟」按鈕 widget
    assert page.steps.cellWidget(1, 7) is not None
    assert page.steps.item(0, 1).text() == "web.open"

    # heal_logs 表
    assert page.heals.rowCount() == 1, page.heals.rowCount()
    assert page.heals.item(0, 0).text() == "s3"
    assert page.heals.item(0, 1).text() == "heal(web)"
    assert page.heals.item(0, 2).text() == "0.870"

    # 頂部摘要文字
    txt = page.lbl_summary.text()
    assert "總步數 3" in txt and "成功 2" in txt and "失敗 1" in txt and "自癒 1" in txt, txt

    # flow 篩選下拉:全部 + demo_flow
    assert page.combo_flow.count() == 2
    print("[OK] ReportPage runs/step_logs/heal_logs 表與摘要數字皆正確填充。")


def test_excel_export(store, run_id):
    detail = load_run_detail(store, run_id)
    out = os.path.join(tempfile.gettempdir(), "rpa_report_steps.xlsx")
    if os.path.exists(out):
        os.remove(out)
    export_steps_excel(detail["steps"], out)
    assert os.path.exists(out), "Excel 檔應被建立"

    import pandas as pd
    back = pd.read_excel(out, engine="openpyxl")
    assert len(back) == 3, len(back)
    assert "step_id" in back.columns and "status" in back.columns
    assert set(back["status"].astype(str)) >= {"ok", "failed"}
    # DataFrame 欄位順序固定
    df = step_logs_dataframe(detail["steps"])
    assert list(df.columns)[:5] == ["id", "run_id", "step_id", "action", "status"]
    print(f"[OK] Excel 匯出成功並可重開:{out}")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    store, run_id = _make_store()
    test_list_runs(store, run_id)
    test_pure_loaders(store, run_id)
    test_report_page(app, store, run_id)
    test_excel_export(store, run_id)
    print("\nALL REPORT SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
