# -*- coding: utf-8 -*-
"""DPI awareness — 在 Windows 設定 per-monitor v2 DPI awareness。

問題:Windows 在 125% / 150% 縮放時,若行程未宣告 DPI awareness,系統會對
視窗做「DPI 虛擬化」(bitmap stretching),導致:
  - pyautogui / pywinauto 取得的座標是「邏輯座標」而非「實體像素座標」,
    點擊/截圖會偏移。
  - mss / ImageGrab 截到的螢幕尺寸與滑鼠座標系不一致。

解法:在 **import pyautogui 之前**(pyautogui 在 import 時就會抓螢幕尺寸),
呼叫 SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2),讓本行程拿到
實體像素座標系,座標就不再被系統縮放扭曲。

設計重點:
  - 必須早(進入點最前面、headless 執行前)呼叫,且只需呼叫一次(冪等)。
  - 非 Windows / ctypes 失敗 / 舊版 Windows 一律 graceful:只記 log、回傳
    合理縮放值(預設 1.0),絕不讓行程崩。
  - 回傳「主螢幕縮放比例」(float,例 125% → 1.25),供呼叫端參考/記 log。
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger("rpa_studio.dpi")

# 模組級旗標:確保 SetProcessDpiAwarenessContext 只設定一次(設過再設會回 ERROR)。
_DPI_SETUP_DONE = False
_DPI_SCALE_CACHE: float | None = None

# DPI_AWARENESS_CONTEXT 常數(以負整數的指標值表示;見 Win32 windef.h)。
# PER_MONITOR_AWARE_V2 = (DPI_AWARENESS_CONTEXT)-4
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
_DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2

# Shcore PROCESS_DPI_AWARENESS(舊 API SetProcessDpiAwareness 用)
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def is_windows() -> bool:
    """目前是否為 Windows 平台。"""
    return sys.platform.startswith("win")


def get_primary_scale() -> float:
    """回傳主螢幕的 DPI 縮放比例(例 125% → 1.25)。

    透過 user32.GetDpiForSystem() / GetDeviceCaps 取得;失敗回 1.0。
    非 Windows 一律回 1.0。此函式不改變任何行程狀態,純查詢。
    """
    if not is_windows():
        return 1.0
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        # GetDpiForSystem 從 Windows 10 1607 起提供;基準 DPI 為 96。
        try:
            dpi = user32.GetDpiForSystem()
            if dpi:
                return round(float(dpi) / 96.0, 4)
        except Exception:  # noqa: BLE001
            pass
        # Fallback:GetDeviceCaps(LOGPIXELSX) 於整個桌面 DC 上查。
        gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
        LOGPIXELSX = 88
        hdc = user32.GetDC(0)
        if hdc:
            try:
                dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
                if dpi:
                    return round(float(dpi) / 96.0, 4)
            finally:
                user32.ReleaseDC(0, hdc)
    except Exception as e:  # noqa: BLE001
        logger.debug("get_primary_scale 失敗(已忽略):%s: %s", type(e).__name__, e)
    return 1.0


def setup_dpi_awareness() -> float:
    """設定本行程為 per-monitor v2 DPI aware,回傳主螢幕縮放比例(float)。

    必須在 **import pyautogui 之前** 呼叫(進入點最前面 / headless 執行前)。
    冪等:重複呼叫只會在第一次真正設定,之後直接回傳快取的縮放值。

    降級策略(全程不拋例外):
      - 非 Windows:直接回傳 1.0。
      - PER_MONITOR_AWARE_V2 不支援(舊 Windows):退到 PER_MONITOR_AWARE,
        再退到 shcore.SetProcessDpiAwareness,再退到 user32.SetProcessDPIAware。
      - 全部失敗:只記 log、回傳查到的縮放值(或 1.0)。
    """
    global _DPI_SETUP_DONE, _DPI_SCALE_CACHE

    if _DPI_SETUP_DONE and _DPI_SCALE_CACHE is not None:
        return _DPI_SCALE_CACHE

    if not is_windows():
        logger.debug("非 Windows 平台,略過 DPI awareness 設定。")
        _DPI_SETUP_DONE = True
        _DPI_SCALE_CACHE = 1.0
        return 1.0

    applied = False
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        # 首選:SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)。
        # 此 API 自 Windows 10 1703 起提供。參數型別為 HANDLE,用 c_void_p 包負值。
        setctx = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if setctx is not None:
            import ctypes as _c
            setctx.restype = _c.c_bool
            setctx.argtypes = [_c.c_void_p]
            for ctx in (
                _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
                _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
                _DPI_AWARENESS_CONTEXT_SYSTEM_AWARE,
            ):
                try:
                    if setctx(_c.c_void_p(ctx)):
                        applied = True
                        logger.info("DPI awareness 已設定 (context=%d)。", ctx)
                        break
                except Exception:  # noqa: BLE001
                    continue

        # Fallback 1:shcore.SetProcessDpiAwareness(PER_MONITOR)(Win 8.1+)。
        if not applied:
            try:
                shcore = ctypes.windll.shcore  # type: ignore[attr-defined]
                rv = shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)
                # S_OK == 0;E_ACCESSDENIED 表已設過,也算成功。
                if rv in (0, -2147024891):  # 0x80070005 = E_ACCESSDENIED
                    applied = True
                    logger.info("DPI awareness 已設定 (shcore PER_MONITOR)。")
            except Exception:  # noqa: BLE001
                pass

        # Fallback 2:user32.SetProcessDPIAware()(Vista+,system-aware)。
        if not applied:
            try:
                if user32.SetProcessDPIAware():
                    applied = True
                    logger.info("DPI awareness 已設定 (system aware)。")
            except Exception:  # noqa: BLE001
                pass

    except Exception as e:  # noqa: BLE001
        logger.warning(
            "設定 DPI awareness 失敗(graceful 降級,座標可能在高縮放下偏移):%s: %s",
            type(e).__name__, e,
        )

    if not applied:
        logger.warning("未能套用任何 DPI awareness 模式;沿用系統預設行為。")

    scale = get_primary_scale()
    _DPI_SETUP_DONE = True
    _DPI_SCALE_CACHE = scale
    logger.info("主螢幕 DPI 縮放比例 = %.4f", scale)
    return scale
