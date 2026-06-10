# -*- coding: utf-8 -*-
"""產生美化後 UI 截圖(真 windows 平台,系統字型才正常)。

對「流程清單 / 編輯器 / 執行 / 排程」四頁各 grab() 一張存到 _shots/。
會先把 flows/*.json 載進臨時 Store,讓畫面有內容(清單 / 表格不空)。

執行(系統 python,務必用真平台,不要 offscreen):
  python tools/ui_shots.py
"""
from __future__ import annotations
import os
import sys
import glob
import tempfile

# 真平台:不要設 offscreen(註解提醒:offscreen 會抓到空白 / 無字型)
os.environ.pop("QT_QPA_PLATFORM", None)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication

from core.schema import Flow
from core.store import Store
from core.vault import Vault
from ui.main_window import MainWindow
from ui.style import APP_QSS

_SHOTS = os.path.join(_ROOT, "_shots")
_FLOWS_DIR = os.path.join(_ROOT, "flows")


def _seed_store(store: Store):
    """把 flows/*.json 載進 Store,讓畫面有內容。"""
    for path in sorted(glob.glob(os.path.join(_FLOWS_DIR, "*.json"))):
        try:
            store.save_flow(Flow.load(path).to_dict())
        except Exception as e:  # noqa: BLE001
            print(f"  (略過 {os.path.basename(path)}: {e})")


def main() -> int:
    os.makedirs(_SHOTS, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(APP_QSS)

    tmpdir = tempfile.mkdtemp(prefix="rpa_shots_")
    store = Store(os.path.join(tmpdir, "shots.db"))
    vault = Vault(tmpdir)
    _seed_store(store)

    win = MainWindow(store=store, vault=vault)
    win.resize(1200, 780)
    win.show()
    for _ in range(8):
        app.processEvents()

    # 各頁刷新並選一筆,讓清單 / 詳情有內容
    win.flows_page.refresh()
    if win.flows_page.list.count() > 0:
        win.flows_page.list.setCurrentRow(0)
    win.run_page.refresh()
    win.schedule_page.refresh()
    win.editor_page.refresh()

    pages = {
        "flows": (0, win.flows_page),
        "editor": (1, win.editor_page),
        "run": (3, win.run_page),
        "schedule": (4, win.schedule_page),
    }

    saved = []
    for key, (idx, _page) in pages.items():
        win._goto(idx)
        for _ in range(6):
            app.processEvents()
        out = os.path.join(_SHOTS, f"ui_{key}.png")
        win.grab().save(out)
        saved.append(out)
        print(f"[OK] 已存 {out}")

    print("\n四張截圖完成:")
    for s in saved:
        print("  " + s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
