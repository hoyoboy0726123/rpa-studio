# -*- coding: utf-8 -*-
"""RPA Studio 桌面控制台進入點 (entry point)。

建 QApplication、套全域深色 QSS、開 MainWindow、進入事件迴圈。
所有重運算/長任務都在 ui.run_worker.RunWorker 執行緒,不卡 UI 主執行緒。
"""
from __future__ import annotations
import os
import sys

# 確保 import 路徑含本專案根(core / engines / ui 皆以此為根)
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# DPI awareness 必須在「載入 Qt / 任何抓螢幕的套件(pyautogui)之前」設定,
# 否則 125% / 150% 縮放下座標與截圖會被系統虛擬化而偏移。graceful:失敗不崩。
try:
    from core.dpi import setup_dpi_awareness
    setup_dpi_awareness()
except Exception:  # noqa: BLE001
    pass

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from ui.style import APP_QSS


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("RPA Studio")
    app.setStyleSheet(APP_QSS)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
