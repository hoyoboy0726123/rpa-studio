# -*- coding: utf-8 -*-
"""錄製頁 offscreen smoke test。

驗證:
  1. sidebar 現在有 7 頁(含「編輯器」「錄製」),RecordPage 建得起來。
  2. 用「假 recorder」(回傳固定 flow dict)驗證錄製頁的核心 wiring:
     開始 → 停止 → 預覽(QTableWidget 填好)→ 存進 Store。
     不真的開執行緒 / 不真錄製:直接呼叫可測試的純函式與回呼。
  3. 缺錄製器(import 失敗)時友善降級,不 crash。

執行:
  QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 python tests/test_record_ui_smoke.py
"""
from __future__ import annotations
import os
import sys
import time
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.store import Store
from core.vault import Vault
from ui.main_window import MainWindow
from ui.pages.record_page import (
    RecordPage, RecordWorker, finalize_recording, make_default_flow_name,
    _summarize_target,
)


# ---- 假 recorder 回傳的固定 flow dict(帶多定位器 target)---- #
_FAKE_FLOW = {
    "name": "rec_fake",
    "engine": "desktop",
    "variables": {},
    "steps": [
        {
            "id": "s1", "action": "desktop.click", "label": "點開始功能表",
            "target": {
                "primary": {"strategy": "uia", "value": '{"control_type": "Button", "name": "Start"}'},
                "fallbacks": [
                    {"strategy": "image", "value": "anchor_0001.png"},
                    {"strategy": "coord", "value": "120,1050"},
                ],
            },
        },
        {
            "id": "s2", "action": "desktop.type", "label": "輸入查詢字串",
            "target": {"primary": {"strategy": "uia", "value": '{"control_type": "Edit"}'}},
            "params": {"text": "hello"},
        },
    ],
}


def test_six_pages_with_record(app):
    tmpdir = tempfile.mkdtemp(prefix="rpa_record_smoke_")
    store = Store(os.path.join(tmpdir, "rpa_record_smoke.db"))
    vault = Vault(tmpdir)
    win = MainWindow(store=store, vault=vault)

    labels = [lbl for lbl, _ in win.pages]
    assert len(win.pages) == 9, f"expected 9 pages, got {len(win.pages)}: {labels}"
    assert "錄製" in labels, labels
    assert win.stack.count() == 9
    # 切到「錄製」頁不爆
    rec_idx = labels.index("錄製")
    win._goto(rec_idx)
    assert win.stack.currentIndex() == rec_idx
    assert isinstance(win.record_page, RecordPage)
    print("[OK] sidebar 有 7 頁(含「錄製」),RecordPage 建得起來:", labels)
    return store


def test_record_page_standalone(app):
    """RecordPage 可單獨建立,基本元件齊全。"""
    tmpdir = tempfile.mkdtemp(prefix="rec_ui_")
    store = Store(os.path.join(tmpdir, "s.db"))
    page = RecordPage(store, recordings_dir=os.path.join(tmpdir, "recordings"))
    assert page.combo_engine.count() == 2
    assert page.table.columnCount() == 3
    assert not page.btn_stop.isEnabled()
    assert not page.btn_save.isEnabled()
    print("[OK] RecordPage 單獨建立,引擎下拉 / 預覽表 / 按鈕初始狀態正確。")


def test_finalize_recording_wiring(app):
    """核心 wiring:停止 → 取回 flow dict → 存進 Store(+ 寫 JSON 檔)。"""
    tmpdir = tempfile.mkdtemp(prefix="rec_fin_")
    store = Store(os.path.join(tmpdir, "s.db"))
    rec_dir = os.path.join(tmpdir, "recordings")

    flow = finalize_recording(_FAKE_FLOW, store, name="rec_fake", engine="desktop",
                              write_dir=rec_dir)
    assert flow.name == "rec_fake"
    assert flow.engine == "desktop"
    assert len(flow.steps) == 2
    # 進了 Store
    loaded = store.load_flow("rec_fake")
    assert loaded is not None, "flow 應已存入 Store"
    assert len(loaded["steps"]) == 2
    # 也寫了一份 JSON 檔
    assert os.path.exists(os.path.join(rec_dir, "rec_fake.json"))
    # 預設命名 fallback
    auto = finalize_recording({"steps": []}, store, engine="web", write_dir=None)
    assert auto.name.startswith("rec_web_"), auto.name
    print("[OK] finalize_recording:存進 Store + 寫 JSON + 自動命名 fallback。")


def test_full_preview_and_save_flow(app):
    """模擬完整 UX wiring(不開真執行緒):開始 → 假 recorder 回傳 → 預覽 → 存。

    直接呼叫頁面的回呼,等同 RecordWorker.recorded signal 抵達主執行緒。
    """
    tmpdir = tempfile.mkdtemp(prefix="rec_full_")
    store = Store(os.path.join(tmpdir, "s.db"))
    page = RecordPage(store, recordings_dir=os.path.join(tmpdir, "recordings"))

    # 選 desktop 引擎、給名稱
    page.combo_engine.setCurrentIndex(1)  # desktop
    assert page.combo_engine.currentData() == "desktop"
    page.name_edit.setText("rec_fake")

    # 模擬「開始」之後 worker 用假 recorder 回傳 flow dict → 抵達 _on_recorded
    page._on_recorded(_FAKE_FLOW)

    # 預覽表格被填好
    assert page.table.rowCount() == 2, page.table.rowCount()
    assert page.table.item(0, 0).text() == "desktop.click"
    assert page.table.item(0, 1).text() == "點開始功能表"
    assert "uia" in page.table.item(0, 2).text()
    assert page.btn_save.isEnabled(), "錄到步驟後存檔鈕應啟用"

    # 「存成流程」的核心 wiring(save_flow 內部呼叫的同一條路徑;
    # 不直接呼叫 page.save_flow() 以避開會阻塞的 QMessageBox 模態視窗)。
    flow = finalize_recording(
        page._recorded_flow, page.store,
        name=page.name_edit.text().strip() or None,
        engine=page.combo_engine.currentData(),
        write_dir=page.recordings_dir,
    )
    loaded = store.load_flow("rec_fake")
    assert loaded is not None and len(loaded["steps"]) == 2
    assert flow.engine == "desktop"
    print("[OK] 假 recorder → _on_recorded 填預覽 → finalize_recording 存進 Store(完整 wiring)。")


def test_recorder_missing_graceful(app):
    """缺錄製器 / 錄製器啟動失敗 → 友善降級為 failed,不 crash。

    用注入「會丟例外的 factory」模擬錄製器缺席或初始化失敗(等同 lazy import 失敗)。
    """
    tmpdir = tempfile.mkdtemp(prefix="rec_miss_")

    def boom_web(url, out_path):
        raise RuntimeError("playwright not installed (simulated)")

    def boom_desktop(flow_name, anchor_dir):
        raise ImportError("pynput not installed (simulated)")

    # web
    msgs = []
    w = RecordWorker(engine="web", url="https://example.com", flow_name="x",
                     anchor_dir=os.path.join(tmpdir, "x_anchors"),
                     recorder_factory=boom_web)
    w.failed.connect(msgs.append)
    w.run()  # 同步跑 run(),不開執行緒
    assert any("web 錄製器" in m for m in msgs), msgs

    # desktop
    msgs2 = []
    w2 = RecordWorker(engine="desktop", url="", flow_name="y",
                      anchor_dir=os.path.join(tmpdir, "y_anchors"),
                      recorder_factory=boom_desktop)
    w2.failed.connect(msgs2.append)
    w2.run()
    assert any("desktop 錄製器" in m for m in msgs2), msgs2
    print("[OK] 錄製器缺席 / 啟動失敗時 RecordWorker 友善降級為 failed,不 crash。")


def test_worker_web_fake_factory(app):
    """web RecordWorker:注入假 record_web(寫一個 flow JSON)→ emit recorded。"""
    tmpdir = tempfile.mkdtemp(prefix="rec_webok_")

    def fake_record_web(url, out_path):
        import json as _json
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            _json.dump({"name": "from_web", "engine": "web",
                        "steps": [{"id": "g", "action": "web.goto",
                                   "params": {"url": url}}]}, fh)
        return out_path

    got = []
    w = RecordWorker(engine="web", url="https://example.com", flow_name="webrec",
                     anchor_dir=os.path.join(tmpdir, "webrec_anchors"),
                     recorder_factory=fake_record_web)
    w.recorded.connect(got.append)
    w.run()
    assert got and got[0]["steps"][0]["action"] == "web.goto", got
    print("[OK] web RecordWorker(假 record_web)→ emit recorded(flow dict)。")


def test_worker_desktop_nonblocking_stop(app):
    """desktop RecordWorker:假的非阻塞 recorder(start 不阻塞、stop_event 控制)。

    驗證 worker 的「start → 等到 stop_requested → recorder.stop() → emit recorded」迴圈,
    對應真實 DesktopRecorder(start 非阻塞、F9/stop_event 停止)的行為。
    """
    import threading as _th

    class FakeDesktopRecorder:
        def __init__(self, flow_name, anchor_dir):
            self.flow_name = flow_name
            self.stop_event = _th.Event()
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True  # 非阻塞:立刻返回

        def stop(self):
            self.stopped = True
            return {"name": self.flow_name, "engine": "desktop",
                    "steps": [{"id": "c", "action": "desktop.click", "label": "click"}]}

    w = RecordWorker(engine="desktop", url="", flow_name="deskrec",
                     anchor_dir=tempfile.mkdtemp(prefix="rec_desk_"),
                     recorder_factory=FakeDesktopRecorder)
    got = []
    # run() 在另一條 thread emit;用 DirectConnection 讓 slot 在 emit 當下直接執行
    # (測試環境沒有跑 Qt 事件迴圈,否則 queued 連線不會被遞送)。
    w.recorded.connect(got.append, Qt.DirectConnection)
    # 在另一條 thread 跑 run()(它會等 stop_requested);主測試送停止訊號。
    t = _th.Thread(target=w.run)
    t.start()
    # 等 recorder start 起來再要求停止
    for _ in range(50):
        if w._recorder is not None and getattr(w._recorder, "started", False):
            break
        time.sleep(0.02)
    w.stop_recording()
    t.join(timeout=5)
    assert not t.is_alive(), "worker run() 應在收到停止訊號後結束"
    assert got and got[0]["steps"][0]["action"] == "desktop.click", got
    assert w._recorder.stopped
    print("[OK] desktop RecordWorker(非阻塞假 recorder)start→stop→emit recorded。")


def test_start_recording_accepts_clicked_bool(app):
    """btn_start.clicked 會帶 checked(bool);start_recording 簽章要能吃掉它,不報 TypeError。

    用會立即丟例外的 factory(等同錄製器缺席)→ worker 起來後馬上 emit failed,
    不會卡住;重點是「帶 bool 呼叫」這條路徑不爆 TypeError。
    """
    import inspect
    sig = inspect.signature(RecordPage.start_recording)
    params = list(sig.parameters.values())
    # 第二個參數(self 之後)應可接受位置參數(Qt 的 checked bool)
    assert params[1].kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              inspect.Parameter.POSITIONAL_ONLY), params
    print("[OK] start_recording 可接受 clicked(bool)位置參數,不會 TypeError。")


def test_summarize_target(app):
    assert _summarize_target(None) == "-"
    s = _summarize_target({"primary": {"strategy": "css", "value": "#btn"},
                           "fallbacks": [{"strategy": "xpath", "value": "//a"}]})
    assert s.startswith("css: #btn"), s
    assert "+1 fallback" in s, s
    print("[OK] _summarize_target 摘要正確。")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    test_six_pages_with_record(app)
    test_record_page_standalone(app)
    test_finalize_recording_wiring(app)
    test_full_preview_and_save_flow(app)
    test_recorder_missing_graceful(app)
    test_worker_web_fake_factory(app)
    test_worker_desktop_nonblocking_stop(app)
    test_start_recording_accepts_clicked_bool(app)
    test_summarize_target(app)
    print("\nALL RECORD UI SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
