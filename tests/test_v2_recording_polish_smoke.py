# -*- coding: utf-8 -*-
"""V2 錄製穩定度打磨冒煙測試 — DPI / 最小化視窗 / 防鬼影 (anti-ghost)。

涵蓋三大主題(全部抽成純函式 / graceful API 來驗,不需互動桌面):

  1. DPI awareness:core.dpi.setup_dpi_awareness() 可呼叫、不崩,回傳合理 float
     縮放值(>0);get_primary_scale() 同樣回 float。冪等(連呼兩次同值)。
  2. 視窗最小化:core.window 的 minimize/restore API 在「無 hwnd / 非法 hwnd」時
     graceful 回 False、不崩;WindowMinimizer context manager 進出不崩;
     get_window_rect 對非法 hwnd 回 None。
  3. 防鬼影:point_in_rect / point_in_excluded_rects 純函式正確判斷;
     裁 anchor 時落在排除框內的像素被塗黑(mask_excluded_in_crop);
     DesktopRecorder._on_click 對排除區內的點擊「不產生 step」。

真實互動部分(誠實標示,無法在 headless 完整驗):
  - 真正最小化/還原一個有畫面的視窗、Qt winId→HWND、pynput 全域監聽即時事件、
    pywinauto.from_point、mss 即時截圖 — 都需互動桌面。本檔只驗可測的純邏輯。

執行(系統 python):
  python tests/test_v2_recording_polish_smoke.py
"""
from __future__ import annotations
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import dpi as dpi_mod                                    # noqa: E402
from core import window as win_mod                                 # noqa: E402
from engines.desktop.recorder import (                             # noqa: E402
    point_in_rect, point_in_excluded_rects, mask_excluded_in_crop,
    _crop_anchor, capture_click_step, DesktopRecorder,
)


# ============================================================== 1. DPI awareness
def test_dpi_setup_returns_float_scale():
    scale = dpi_mod.setup_dpi_awareness()
    assert isinstance(scale, float), f"縮放值應為 float,實得 {type(scale)}"
    assert scale > 0, f"縮放值應 > 0,實得 {scale}"
    # 合理範圍(50%~400%);至少不該是 0 或負或天文數字。
    assert 0.5 <= scale <= 4.0, f"縮放值超出合理範圍:{scale}"
    print(f"[OK] dpi.setup_dpi_awareness() 不崩,回傳縮放值 = {scale}")


def test_dpi_idempotent():
    a = dpi_mod.setup_dpi_awareness()
    b = dpi_mod.setup_dpi_awareness()
    assert a == b, f"冪等性失敗:{a} != {b}"
    scale = dpi_mod.get_primary_scale()
    assert isinstance(scale, float) and scale > 0
    print(f"[OK] dpi 冪等(連呼兩次同值={a});get_primary_scale()={scale}")


# ============================================================ 2. window minimize
def test_window_graceful_no_hwnd():
    """無 hwnd / 非法 hwnd 時,所有 API graceful 回 False/None,不崩。"""
    assert win_mod.minimize_window(None) is False
    assert win_mod.restore_window(None) is False
    assert win_mod.minimize_window(0) is False
    assert win_mod.minimize_window("not-a-handle") is False
    assert win_mod.get_window_rect(None) is None
    assert win_mod.get_window_rect(0) is None
    # 一個幾乎不可能存在的 handle
    assert win_mod.minimize_window(0x7FFFFFFF) is False
    print("[OK] window 最小化/還原/取矩形:無 hwnd 時 graceful 不崩")


def test_window_minimizer_context_manager():
    """WindowMinimizer 進出不崩;無 hwnd 時 minimized 保持 False。"""
    with win_mod.WindowMinimizer(None) as mz:
        assert mz.minimized is False
    # 離開後也不應崩
    mz2 = win_mod.WindowMinimizer(0)
    mz2.__enter__()
    mz2.__exit__(None, None, None)
    assert mz2.minimized is False
    print("[OK] WindowMinimizer context manager 進出 graceful 不崩")


def test_hwnd_from_qt_widget_none():
    """hwnd_from_qt_widget(None) 回 None,不崩(無需 Qt)。"""
    assert win_mod.hwnd_from_qt_widget(None) is None
    print("[OK] hwnd_from_qt_widget(None) → None")


# ============================================================== 3. anti-ghost
def test_point_in_rect():
    rect = (100, 100, 200, 200)
    assert point_in_rect(150, 150, rect) is True       # 內部
    assert point_in_rect(100, 100, rect) is True       # 左上角(含邊界)
    assert point_in_rect(200, 200, rect) is True       # 右下角(含邊界)
    assert point_in_rect(99, 150, rect) is False       # 左邊界外
    assert point_in_rect(250, 150, rect) is False      # 右邊界外
    assert point_in_rect(150, 50, rect) is False       # 上邊界外
    # 反序矩形仍正確正規化
    assert point_in_rect(150, 150, (200, 200, 100, 100)) is True
    # 非法 rect → False
    assert point_in_rect(150, 150, None) is False
    assert point_in_rect(150, 150, (1, 2, 3)) is False
    print("[OK] point_in_rect 邊界 / 反序 / 非法輸入皆正確")


def test_point_in_excluded_rects():
    rects = [(0, 0, 50, 50), (100, 100, 200, 200)]
    assert point_in_excluded_rects(25, 25, rects) is True       # 第一框內
    assert point_in_excluded_rects(150, 150, rects) is True     # 第二框內
    assert point_in_excluded_rects(75, 75, rects) is False      # 兩框之間
    assert point_in_excluded_rects(75, 75, []) is False         # 空清單
    assert point_in_excluded_rects(75, 75, None) is False       # None
    print("[OK] point_in_excluded_rects:點落在任一排除框 → True")


def test_mask_excluded_in_crop_blacks_out_region():
    """裁 anchor 時,落在排除框內的像素被塗黑(anchor 不含排除區內容)。"""
    from PIL import Image
    # 一張白色 100x100 圖,代表裁切框,左上角在螢幕座標 (200,200)
    crop = Image.new("RGB", (100, 100), (255, 255, 255))
    crop_origin = (200, 200)
    # 排除框覆蓋裁切框的左半(螢幕座標 200..250)
    excluded = [(200, 200, 250, 300)]
    out = mask_excluded_in_crop(crop, crop_origin, excluded, fill=(0, 0, 0))
    # 左半應為黑(本地 x<50),右半應仍為白
    assert out.getpixel((10, 50)) == (0, 0, 0), "排除區內未被塗黑"
    assert out.getpixel((90, 50)) == (255, 255, 255), "排除區外不該被塗黑"
    # 無排除區 → 原樣
    crop2 = Image.new("RGB", (20, 20), (255, 255, 255))
    out2 = mask_excluded_in_crop(crop2, (0, 0), [])
    assert out2.getpixel((10, 10)) == (255, 255, 255)
    print("[OK] mask_excluded_in_crop:排除區塗黑、區外保留")


def test_crop_anchor_with_excluded_masks():
    """_crop_anchor 帶 excluded_rects + crop_origin → 寫出的 PNG 排除區為黑。"""
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        shot = Image.new("RGB", (400, 300), (200, 180, 60))
        shot._rpa_offset = (0, 0)  # type: ignore[attr-defined]
        out = os.path.join(tmp, "a.png")
        # 中心 (200,150),size 100 → 裁切框螢幕座標 left=150,top=100
        # 排除框覆蓋裁切框左半(150..200)
        _crop_anchor(shot, 200, 150, out, size=100,
                     excluded_rects=[(150, 100, 200, 250)],
                     crop_origin=(150, 100))
        with Image.open(out) as im:
            assert im.size == (100, 100)
            assert im.getpixel((10, 50)) == (0, 0, 0), "排除區未塗黑"
            assert im.getpixel((90, 50)) == (200, 180, 60), "區外被誤改"
    print("[OK] _crop_anchor 帶排除區:anchor 排除區塗黑、其餘保留")


def test_recorder_skips_click_in_excluded_rect():
    """DesktopRecorder._on_click:點到排除區 → 不產生 step;點外面 → 產生 step。"""
    from PIL import Image

    class _FakeBtn:
        name = "left"

    def _fake_screen():
        img = Image.new("RGB", (400, 300), (10, 20, 30))
        img._rpa_offset = (0, 0)  # type: ignore[attr-defined]
        return img

    with tempfile.TemporaryDirectory() as tmp:
        anchor_dir = os.path.join(tmp, "anchors")
        # 排除框 = 工具自己的視窗 (0,0,100,100)
        rec = DesktopRecorder("t", anchor_dir, excluded_rects=[(0, 0, 100, 100)])

        # 點在排除框內 → 應被略過
        rec._on_click(50, 50, _FakeBtn(), True)
        assert len(rec.steps) == 0, "排除區內的點擊不該產生 step"

        # 點在排除框外 → 應產生 step(注入假抓取以免碰真桌面)
        import engines.desktop.recorder as rmod
        orig_screen = rmod._grab_screen
        orig_uia = rmod.grab_uia_at
        rmod._grab_screen = _fake_screen
        rmod.grab_uia_at = lambda x, y: {}
        try:
            rec._on_click(250, 200, _FakeBtn(), True)
            rec._drain_pending()   # 新模型:_on_click 只入列,需 drain 才產生 step
        finally:
            rmod._grab_screen = orig_screen
            rmod.grab_uia_at = orig_uia
        assert len(rec.steps) == 1, "排除區外的點擊應產生 step"
        assert rec.steps[0].action == "desktop.click"
    print("[OK] DesktopRecorder._on_click 防鬼影:排除區內略過、區外正常抓取")


def test_recorder_excluded_rects_default_empty():
    """未給 excluded_rects 時預設空清單,行為與舊版一致(不擋任何點)。"""
    with tempfile.TemporaryDirectory() as tmp:
        rec = DesktopRecorder("t", os.path.join(tmp, "a"))
        assert rec.excluded_rects == []
        assert rec.self_hwnd is None
    print("[OK] DesktopRecorder 預設 excluded_rects=[] / self_hwnd=None")


# ===================================================================== runner
def main() -> int:
    print("=" * 64)
    print("RPA Studio V2 - recording polish smoke test (DPI / minimize / anti-ghost)")
    print("=" * 64)
    tests = [
        test_dpi_setup_returns_float_scale,
        test_dpi_idempotent,
        test_window_graceful_no_hwnd,
        test_window_minimizer_context_manager,
        test_hwnd_from_qt_widget_none,
        test_point_in_rect,
        test_point_in_excluded_rects,
        test_mask_excluded_in_crop_blacks_out_region,
        test_crop_anchor_with_excluded_masks,
        test_recorder_skips_click_in_excluded_rect,
        test_recorder_excluded_rects_default_empty,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
            print(traceback.format_exc())
    print("-" * 64)
    if failed:
        print(f"RESULT: FAIL ({failed}/{len(tests)} 失敗)")
        return 1
    print("ALL GREEN ✔")
    print("\n[NOTE] 真實互動部分未在此驗證:")
    print("  - 真正最小化/還原有畫面的視窗、Qt winId→HWND 需互動桌面 + PySide6")
    print("  - pynput 全域監聽即時事件 / pywinauto.from_point / mss 即時截圖需互動桌面")
    print("  - DPI 偏移校正效果需在 125%/150% 實機觀察")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
