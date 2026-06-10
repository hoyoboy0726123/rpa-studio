# -*- coding: utf-8 -*-
"""視窗控制 (ctypes user32) — 錄製時最小化 RPA Studio 自己的視窗。

目的:桌面錄製是「全域監聽鍵鼠 + 對任意視窗 from_point 抓 UIA」。如果 RPA
Studio 自己的視窗還浮在前面,使用者很可能不小心點到工具自己,或工具的視窗
被截進 anchor。錄製開始時把自己最小化、停止後還原,可大幅減少這種干擾。

設計重點:
  - 全部用 ctypes user32,不依賴 PySide6(headless / 測試也能 import)。
  - 視窗操作抽成可測純函式:
      get_window_rect(hwnd)          查矩形(供防鬼影排除區計算)
      minimize_window(hwnd) / restore_window(hwnd)
      WindowMinimizer(hwnd)          context manager(進入最小化、離開還原)
  - 無 hwnd / 非 Windows / ctypes 失敗一律 graceful:回傳 False、不崩。
  - 提供 hwnd_from_qt_widget() 把 Qt widget 轉成 HWND,供 UI 錄製頁呼叫;
    取不到時回 None(由呼叫端決定要不要最小化)。
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger("rpa_studio.window")

# ShowWindow nCmdShow 常數
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOWNORMAL = 1


def is_windows() -> bool:
    return sys.platform.startswith("win")


def _user32():
    """取得 user32(僅 Windows);失敗回 None。"""
    if not is_windows():
        return None
    try:
        import ctypes
        return ctypes.windll.user32  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        logger.debug("載入 user32 失敗:%s: %s", type(e).__name__, e)
        return None


def _valid_hwnd(hwnd) -> bool:
    """hwnd 是否為可用的視窗 handle(非 None、可轉 int、IsWindow 為真)。"""
    if hwnd is None:
        return False
    try:
        h = int(hwnd)
    except (TypeError, ValueError):
        return False
    if h == 0:
        return False
    u = _user32()
    if u is None:
        # 非 Windows:無法驗證,但至少 handle 形狀合理 → 交給呼叫端判斷。
        return False
    try:
        return bool(u.IsWindow(h))
    except Exception:  # noqa: BLE001
        return False


def get_window_rect(hwnd) -> tuple[int, int, int, int] | None:
    """回傳視窗的螢幕矩形 (left, top, right, bottom);取不到回 None。

    供防鬼影排除區計算(把工具自己的視窗矩形丟給 recorder 的 excluded_rects)。
    """
    u = _user32()
    if u is None or not _valid_hwnd(hwnd):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        ok = u.GetWindowRect(int(hwnd), ctypes.byref(rect))
        if not ok:
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception as e:  # noqa: BLE001
        logger.debug("GetWindowRect 失敗:%s: %s", type(e).__name__, e)
        return None


def _show_window(hwnd, cmd: int) -> bool:
    """ShowWindow 包裝:成功(hwnd 有效且呼叫不拋)回 True,否則 False。"""
    u = _user32()
    if u is None:
        logger.debug("非 Windows 或 user32 不可用,略過視窗操作 cmd=%d。", cmd)
        return False
    if not _valid_hwnd(hwnd):
        logger.debug("無效 hwnd,略過視窗操作 cmd=%d。", cmd)
        return False
    try:
        u.ShowWindow(int(hwnd), cmd)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("ShowWindow(cmd=%d) 失敗:%s: %s", cmd, type(e).__name__, e)
        return False


def minimize_window(hwnd) -> bool:
    """最小化指定視窗。無 hwnd / 非 Windows / 失敗 → 回 False(graceful)。"""
    ok = _show_window(hwnd, SW_MINIMIZE)
    if ok:
        logger.info("已最小化視窗 hwnd=%s。", hwnd)
    return ok


def restore_window(hwnd) -> bool:
    """還原指定視窗。無 hwnd / 非 Windows / 失敗 → 回 False(graceful)。"""
    ok = _show_window(hwnd, SW_RESTORE)
    if ok:
        logger.info("已還原視窗 hwnd=%s。", hwnd)
    return ok


def hwnd_from_qt_widget(widget) -> int | None:
    """把 PySide6/PyQt 的 top-level widget 轉成 Win32 HWND。

    widget.winId() 在 Windows 上即為 HWND。取不到 / 非 Windows / 失敗回 None。
    這層刻意吃掉所有例外,讓 UI 錄製頁可以「能最小化就最小化、不能就算了」。
    """
    if not is_windows() or widget is None:
        return None
    try:
        wid = widget.winId()
        h = int(wid)
        return h or None
    except Exception as e:  # noqa: BLE001
        logger.debug("hwnd_from_qt_widget 失敗:%s: %s", type(e).__name__, e)
        return None


class WindowMinimizer:
    """Context manager:進入時最小化視窗、離開時還原。

    用法(UI 錄製頁):
        hwnd = core.window.hwnd_from_qt_widget(main_window)
        with core.window.WindowMinimizer(hwnd):
            recorder.start(); recorder.wait()   # 錄製期間自己縮起來
        # 離開 with → 自動還原

    hwnd 為 None 或操作失敗時什麼都不做(graceful),仍可正常進出 with。
    minimized 屬性記錄是否真的最小化成功(供 UI / 測試判斷)。
    """

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.minimized = False

    def __enter__(self) -> "WindowMinimizer":
        self.minimized = minimize_window(self.hwnd)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.minimized:
            restore_window(self.hwnd)
        self.minimized = False
        return False  # 不吞例外
