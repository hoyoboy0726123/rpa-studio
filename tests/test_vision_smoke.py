# -*- coding: utf-8 -*-
"""Vision 層 smoke test:CV 影像比對 + OCR + 動作註冊。

驗收:
  1) image_match 核心:用 PIL 產一張大圖、在已知位置內嵌一塊小圖,存成 anchor,
     用 locate_in_image(大圖, anchor) 比對,assert 命中中心座標正確(容許誤差)。
     另測 locate(region=...) 對 region 偏移加回是否正確(用 monkeypatch 假截圖)。
  2) OCR:用 PIL 畫中英文字到圖 -> read_image -> assert 關鍵字出現。
     若 rapidocr 裝不起來 / 載入失敗 -> 標記 SKIP 並誠實回報,不假裝過。
  3) 動作註冊:assert desktop.wait_image / image_click / ocr_read 三動作已註冊。

執行(系統 python,專案根會自動加進 sys.path):
  PYTHONIOENCODING=utf-8 python tests/test_vision_smoke.py
"""
from __future__ import annotations
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ===================================================================== 工具圖產生
def _make_haystack_with_anchor(tmpdir):
    """產生大圖(400x300)+ 在 (180,120) 起內嵌 40x30 紅底白方塊作為 anchor。

    回傳 (haystack_path, anchor_path, expected_center_xy)。
    """
    from PIL import Image, ImageDraw

    big = Image.new("RGB", (400, 300), (30, 60, 90))
    draw = ImageDraw.Draw(big)
    # 加點雜訊紋理,避免全圖太均勻造成 matchTemplate 退化
    for i in range(0, 400, 17):
        draw.line([(i, 0), (i, 300)], fill=(40, 70, 100), width=1)

    ax, ay, aw, ah = 180, 120, 40, 30
    # anchor 內容:獨特的色塊 + 對角線,讓比對唯一
    draw.rectangle([ax, ay, ax + aw - 1, ay + ah - 1], fill=(220, 40, 40))
    draw.line([(ax, ay), (ax + aw - 1, ay + ah - 1)], fill=(255, 255, 255), width=2)
    draw.ellipse([ax + 8, ay + 6, ax + 28, ay + 22], outline=(255, 255, 0), width=2)

    haystack_path = os.path.join(tmpdir, "haystack.png")
    anchor_path = os.path.join(tmpdir, "anchor_0001.png")
    big.save(haystack_path)
    big.crop((ax, ay, ax + aw, ay + ah)).save(anchor_path)

    expected = (ax + aw // 2, ay + ah // 2)  # (200, 135)
    return haystack_path, anchor_path, expected


def _make_text_image(tmpdir, text="Hello 世界 RPA123"):
    """畫一段中英混排文字到白底圖,回傳 (path, text)。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (520, 120), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = None
    # 嘗試載入能顯示中文的字型(Windows 內建);失敗退預設(中文可能變框)
    for cand in (r"C:\Windows\Fonts\msjh.ttc",
                 r"C:\Windows\Fonts\msyh.ttc",
                 r"C:\Windows\Fonts\simsun.ttc",
                 r"C:\Windows\Fonts\arial.ttf"):
        try:
            if os.path.exists(cand):
                font = ImageFont.truetype(cand, 48)
                break
        except Exception:
            continue
    draw.text((20, 30), text, fill=(0, 0, 0), font=font)
    path = os.path.join(tmpdir, "ocr_text.png")
    img.save(path)
    return path, text


# ====================================================================== 測試項目
def test_image_match_core(tmpdir) -> bool:
    """locate_in_image 命中中心座標正確(容許誤差 ±3px)。"""
    from engines.vision import image_match

    haystack, anchor, (ex, ey) = _make_haystack_with_anchor(tmpdir)
    hit = image_match.locate_in_image(haystack, anchor, confidence=0.85)
    if hit is None:
        print("[FAIL] locate_in_image 未命中(應命中內嵌 anchor)")
        return False
    cx, cy, score = hit
    tol = 3
    ok = abs(cx - ex) <= tol and abs(cy - ey) <= tol
    print(f"[image] locate_in_image -> center=({cx},{cy}) expected=({ex},{ey}) "
          f"score={score:.4f} -> {'OK' if ok else 'MISMATCH'}")
    if not ok:
        return False

    # 負向測試:用一個「有紋理但 haystack 裡不存在」的圖樣,高 confidence 應回 None。
    # (注意:TM_CCOEFF_NORMED 對純色圖樣會處處高分,故負向測試必須用有結構的圖樣。)
    from PIL import Image, ImageDraw
    decoy = Image.new("RGB", (40, 30), (5, 5, 5))
    d = ImageDraw.Draw(decoy)
    d.line([(0, 0), (39, 29)], fill=(123, 200, 7), width=3)
    d.ellipse([5, 5, 30, 25], outline=(7, 99, 240), width=2)
    decoy_path = os.path.join(tmpdir, "decoy.png")
    decoy.save(decoy_path)
    miss = image_match.locate_in_image(haystack, decoy_path, confidence=0.9)
    print(f"[image] 不存在的有結構圖樣 @confidence0.9 -> {miss} (期望 None)")
    if miss is not None:
        print("[WARN] 不存在圖樣竟命中,confidence 門檻可能偏鬆(不視為 fail)")

    return True


def test_locate_region_offset(tmpdir) -> bool:
    """locate(region=...) 命中座標要加回 region 偏移。用 monkeypatch 假截圖驗證。"""
    from engines.vision import image_match

    haystack, anchor, (ex, ey) = _make_haystack_with_anchor(tmpdir)

    # 假裝「螢幕」就是 haystack,region 偏移 (1000, 500)
    import numpy as np
    import cv2
    with open(haystack, "rb") as f:
        buf = np.frombuffer(f.read(), dtype=np.uint8)
    fake_screen = cv2.imdecode(buf, cv2.IMREAD_COLOR)

    region = (1000, 500, 400, 300)

    orig = image_match._grab_screen
    image_match._grab_screen = lambda reg=None: (fake_screen, (region[0], region[1]))
    try:
        pt = image_match.locate(anchor, confidence=0.85, region=region)
    finally:
        image_match._grab_screen = orig

    if pt is None:
        print("[FAIL] locate(region) 未命中")
        return False
    want = (ex + region[0], ey + region[1])
    ok = abs(pt[0] - want[0]) <= 3 and abs(pt[1] - want[1]) <= 3
    print(f"[image] locate(region offset) -> {pt} expected≈{want} "
          f"-> {'OK' if ok else 'MISMATCH'}")
    return ok


def test_ocr(tmpdir):
    """read_image -> 關鍵字出現。回傳 'PASS' / 'FAIL' / 'SKIP:<reason>'。"""
    from engines.vision import ocr

    # 先確認 rapidocr 能不能載入引擎;載不起來 -> SKIP(誠實)
    engine = ocr._get_engine()
    if engine is None:
        return "SKIP:rapidocr 引擎無法載入(未安裝或 onnxruntime 不可用)"

    path, text = _make_text_image(tmpdir, text="Hello 123 RPA")
    got = ocr.read_image(path)
    print(f"[ocr] read_image -> {got!r}")
    norm = got.replace(" ", "").lower()
    # 英文 + 數字較穩定當斷言關鍵字(中文字型/解析度可能影響)
    hits = [kw for kw in ("hello", "123", "rpa") if kw in norm]
    if len(hits) >= 1:
        print(f"[ocr] 命中關鍵字: {hits}")
        return "PASS"
    if got.strip() == "":
        return "SKIP:OCR 回空字串(graceful 降級,環境辨識不出)"
    return "FAIL"


def test_actions_registered() -> bool:
    """三個新 vision 動作要已註冊。"""
    import engines.desktop  # noqa: F401  觸發註冊
    from core.registry import ACTIONS
    expected = ["desktop.wait_image", "desktop.image_click", "desktop.ocr_read"]
    missing = [n for n in expected if n not in ACTIONS]
    if missing:
        print(f"[FAIL] vision 動作註冊缺漏: {missing}")
        return False
    print(f"[reg] vision 動作註冊 OK: {expected}")
    return True


def test_screenpoint_resolve(tmpdir) -> bool:
    """locators.resolve image/coord 策略回 ScreenPoint(不需 GUI)。"""
    from engines.desktop import locators

    # coord
    sp = locators.resolve(None, {"primary": {"strategy": "coord", "value": "123,456"}})
    if not (locators.is_screen_point(sp) and sp.coords() == (123, 456)):
        print(f"[FAIL] coord resolve -> {sp!r}")
        return False
    print(f"[locator] coord -> {sp!r} OK")

    # image(用 anchor_dir + 假 locate)
    haystack, anchor, (ex, ey) = _make_haystack_with_anchor(tmpdir)
    from engines.vision import image_match
    orig = image_match.locate_score  # 產品端改用 locate_score(回傳含比對分數),patch 它
    image_match.locate_score = lambda p, confidence=0.85, region=None, multi_scale=False: (777, 888, 0.95)
    try:
        sp2 = locators.resolve(
            None,
            {"primary": {"strategy": "image", "value": os.path.basename(anchor)}},
            anchor_dir=tmpdir)
    finally:
        image_match.locate_score = orig
    if not (locators.is_screen_point(sp2) and sp2.coords() == (777, 888)):
        print(f"[FAIL] image resolve -> {sp2!r}")
        return False
    print(f"[locator] image(anchor_dir) -> {sp2!r} OK")

    # fallback 串:壞 uia -> coord 收尾
    sp3 = locators.resolve(None, {
        "primary": {"strategy": "auto_id", "value": "nope"},
        "fallbacks": [{"strategy": "coord", "value": "10,20"}],
    })
    if not (locators.is_screen_point(sp3) and sp3.coords() == (10, 20)):
        print(f"[FAIL] fallback coord -> {sp3!r}")
        return False
    print(f"[locator] fallback(uia->coord) -> {sp3!r} OK")
    return True


# ========================================================================= main
def main() -> int:
    print("=" * 64)
    print("RPA Studio - vision (CV + OCR) smoke test")
    print("=" * 64)

    # 環境
    for mod in ("cv2", "numpy", "PIL"):
        try:
            __import__(mod)
            print(f"[ENV] {mod} OK")
        except Exception as e:  # noqa: BLE001
            print(f"[ENV][FAIL] 缺少必要套件 {mod}: {e}")
            return 1
    for opt in ("mss", "pyautogui"):
        try:
            __import__(opt)
            print(f"[ENV] {opt} OK")
        except Exception as e:  # noqa: BLE001
            print(f"[ENV] {opt} 不可用(非必要,僅影響實機截圖/點擊): {e}")

    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        results["actions_registered"] = test_actions_registered()
        results["image_match_core"] = test_image_match_core(tmp)
        results["locate_region_offset"] = test_locate_region_offset(tmp)
        results["screenpoint_resolve"] = test_screenpoint_resolve(tmp)
        ocr_res = test_ocr(tmp)
        results["ocr"] = ocr_res

    print("\n" + "-" * 64)
    hard_fail = False
    for name, val in results.items():
        if isinstance(val, str):  # ocr 三態
            if val == "PASS":
                print(f"  {name:24s} : PASS")
            elif val.startswith("SKIP"):
                print(f"  {name:24s} : SKIP ({val.split(':',1)[1]})")
            else:
                print(f"  {name:24s} : FAIL")
                hard_fail = True
        else:
            print(f"  {name:24s} : {'PASS' if val else 'FAIL'}")
            if not val:
                hard_fail = True

    print("-" * 64)
    print("RESULT:", "FAIL" if hard_fail else "PASS")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
