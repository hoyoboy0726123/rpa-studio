# -*- coding: utf-8 -*-
"""錄製器冒煙測試 (smoke test) — 純解析/抓取邏輯,不開瀏覽器、不做全域監聽。

涵蓋:
  A. web codegen 轉譯:餵一段範例 codegen python 字串 → parse_codegen_python
     → 斷言產生的 web.* steps 動作與 target 正確(不開瀏覽器)。
  B. 桌面「一次點擊抓多組定位器」:把抓取邏輯抽成 capture_click_step,
     餵 mock UIA 元素 + 假截圖 + 座標 → 斷言 target 同時有
     uia primary + image fallback + coord,且 anchor PNG 有寫出。

真實錄製(無法在此環境驗證,誠實標示):
  - web:record_web() 會 subprocess 跑 `playwright codegen` 開瀏覽器讓人實際操作,
    關視窗後才有 python 輸出 — 需真實桌面 + 已裝 playwright。
  - desktop:DesktopRecorder.start() 用 pynput 做全域鍵鼠監聽、pywinauto.from_point
    抓即時 UIA、mss 即時截圖 — 都需互動桌面,headless CI 無法觸發即時事件。

執行(系統 python,專案根自動加進 sys.path):
  PYTHONIOENCODING=utf-8 python tests/test_recorder_smoke.py
"""
from __future__ import annotations
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engines.web.recorder import parse_codegen_python, codegen_line_to_step  # noqa: E402
from engines.desktop.recorder import (                                       # noqa: E402
    capture_click_step, build_click_step, uia_element_to_spec, _crop_anchor)


# ============================================================== A. web codegen
SAMPLE_CODEGEN = '''\
import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://erp.example.com/login")
    page.get_by_label("帳號").fill("alice")
    page.get_by_placeholder("請輸入密碼").fill("secret123")
    page.get_by_role("button", name="登入").click()
    page.get_by_text("查詢報表").click()
    page.get_by_test_id("month-select").select_option("2026-05")
    page.locator("#keyword").fill("SN12345")
    page.get_by_role("textbox", name="關鍵字").press("Enter")
    page.locator("css=.result-row").click()


with sync_playwright() as playwright:
    run(playwright)
'''


def test_web_codegen_parse():
    flow = parse_codegen_python(SAMPLE_CODEGEN, flow_name="erp_login")
    assert flow["engine"] == "web", "engine 應為 web"
    assert flow["name"] == "erp_login"
    steps = flow["steps"]

    actions = [s["action"] for s in steps]
    # 樣板列(import / launch / with)應被忽略,只留 page.* 動作
    expected = [
        "web.goto", "web.fill", "web.fill", "web.click", "web.click",
        "web.select", "web.fill", "web.press", "web.click",
    ]
    assert actions == expected, f"動作序列不符:\n got={actions}\n exp={expected}"

    # --- goto ---
    assert steps[0]["params"]["url"] == "https://erp.example.com/login"

    # --- get_by_label("帳號").fill("alice") → text 定位 + value ---
    s_label = steps[1]
    assert s_label["params"]["value"] == "alice"
    assert s_label["target"]["primary"]["strategy"] == "text"
    assert s_label["target"]["primary"]["value"] == "帳號"

    # --- get_by_placeholder("請輸入密碼").fill(...) → css [placeholder=...] ---
    s_ph = steps[2]
    assert s_ph["params"]["value"] == "secret123"
    assert s_ph["target"]["primary"]["strategy"] == "css"
    assert s_ph["target"]["primary"]["value"] == '[placeholder="請輸入密碼"]'

    # --- get_by_role("button", name="登入").click() → role primary "button:登入" ---
    s_btn = steps[3]
    assert s_btn["action"] == "web.click"
    assert s_btn["target"]["primary"] == {"strategy": "role", "value": "button:登入"}
    assert s_btn["target"]["fingerprint"]["text"] == "登入"

    # --- get_by_text("查詢報表").click() → text primary ---
    s_txt = steps[4]
    assert s_txt["target"]["primary"] == {"strategy": "text", "value": "查詢報表"}

    # --- get_by_test_id("month-select").select_option("2026-05") → web.select / testid ---
    s_sel = steps[5]
    assert s_sel["action"] == "web.select"
    assert s_sel["params"]["value"] == "2026-05"
    assert s_sel["target"]["primary"] == {"strategy": "testid", "value": "month-select"}

    # --- locator("#keyword").fill("SN12345") → css primary ---
    s_loc = steps[6]
    assert s_loc["params"]["value"] == "SN12345"
    assert s_loc["target"]["primary"] == {"strategy": "css", "value": "#keyword"}

    # --- get_by_role("textbox", name="關鍵字").press("Enter") → web.press / role ---
    s_press = steps[7]
    assert s_press["action"] == "web.press"
    assert s_press["params"]["key"] == "Enter"
    assert s_press["target"]["primary"] == {"strategy": "role", "value": "textbox:關鍵字"}

    # --- locator("css=.result-row").click() → css primary (帶 css= 前綴剝除) ---
    s_css = steps[8]
    assert s_css["action"] == "web.click"
    assert s_css["target"]["primary"] == {"strategy": "css", "value": ".result-row"}

    print(f"[OK] web codegen 轉譯:{len(steps)} steps,動作與 target 全部正確")


def test_web_codegen_ignores_boilerplate():
    """非 page.* 行(import / launch / 註解)應被忽略,不產 step。"""
    for line in [
        "import re",
        "    browser = playwright.chromium.launch(headless=False)",
        "    context = browser.new_context()",
        "    page = context.new_page()",
        "# a comment",
        "    with sync_playwright() as playwright:",
    ]:
        assert codegen_line_to_step(line) is None, f"不該產 step:{line!r}"
    print("[OK] web codegen 樣板/註解行正確被忽略")


# ============================================================== B. desktop grab
def _fake_screenshot(w=400, h=300, color=(123, 200, 50)):
    """造一張假的整螢幕截圖(PIL.Image),供裁 anchor 測試。"""
    from PIL import Image
    img = Image.new("RGB", (w, h), color)
    img._rpa_offset = (0, 0)  # type: ignore[attr-defined]
    return img


MOCK_UIA = {
    "name": "登入",
    "control_type": "Button",
    "auto_id": "loginBtn",
    "class_name": "Button",
    "window_title": "ERP 系統 - 登入",
}


def test_desktop_capture_multi_locator():
    """一次點擊 → step 同時帶 uia primary + image fallback + coord,且 anchor 寫出。"""
    with tempfile.TemporaryDirectory() as tmp:
        anchor_dir = os.path.join(tmp, "myflow_anchors")
        os.makedirs(anchor_dir, exist_ok=True)

        step = capture_click_step(
            x=250, y=120, anchor_dir=anchor_dir, index=1, button="left",
            uia_grabber=lambda x, y: dict(MOCK_UIA),       # 注入 mock UIA
            screen_grabber=_fake_screenshot,               # 注入假截圖
        )

        assert step.action == "desktop.click"
        tgt = step.target
        assert tgt is not None

        # --- primary 必須是 uia,且 value 是可被 desktop locators 解析的 JSON ---
        assert tgt["primary"]["strategy"] == "uia"
        import json
        spec = json.loads(tgt["primary"]["value"])
        assert spec["name"] == "登入"
        assert spec["control_type"] == "Button"
        assert spec["auto_id"] == "loginBtn"

        # --- fallbacks 必須同時有 image 與 coord ---
        str016 = {fb["strategy"]: fb["value"] for fb in tgt["fallbacks"]}
        assert str016["image"] == "anchor_0001.png", f"image fallback 錯:{str016}"
        assert str016["coord"] == "250,120", f"coord fallback 錯:{str016}"

        # --- fingerprint 完整 ---
        fp = tgt["fingerprint"]
        assert fp["uia"]["control_type"] == "Button"
        assert fp["anchor"] == "anchor_0001.png"
        assert fp["coord"] == "250,120"
        assert fp["window_title"] == "ERP 系統 - 登入"

        # --- anchor PNG 確實寫出且為合法 100x100(邊界內)PNG ---
        anchor_path = os.path.join(anchor_dir, "anchor_0001.png")
        assert os.path.exists(anchor_path), "anchor PNG 未寫出"
        assert os.path.getsize(anchor_path) > 0, "anchor PNG 大小為 0"
        from PIL import Image
        with Image.open(anchor_path) as im:
            assert im.format == "PNG"
            assert im.size == (100, 100), f"anchor 尺寸應 100x100,實得 {im.size}"

        # --- desktop locators 應能解析這個 target(對得上 resolve)---
        _assert_resolvable_by_desktop_locators(tgt)

        print("[OK] desktop 一次點擊:uia primary + image/coord fallback + anchor 寫出")


def _assert_resolvable_by_desktop_locators(target: dict):
    """用 desktop locators 的 mock controller 驗 primary(uia)能被解析。

    確認錄製產出的 target 格式與 engines/desktop/locators.resolve 對得上。
    """
    from engines.desktop import locators

    class _Wrap:
        def window_text(self):
            return "登入"

    class _Win:
        def wait(self, *a, **k):   # 新版 resolver 會先 win.wait("exists",...)
            return self

        def child_window(self, **kw):
            # 新版 resolver 逐條退讓,auto_id 單獨即應命中(最穩的一條);
            # 確認 uia spec 有被正確翻成 pywinauto kwargs。
            if "auto_id" in kw:
                assert kw["auto_id"] == "loginBtn"
                return self
            raise RuntimeError("此 mock 僅以 auto_id 命中")

        def wrapper_object(self):
            return _Wrap()

    class _Ctrl:
        backend = "uia"
        app = None
        win32_desktop = None

        class _Desktop:
            def window(self, **kw):
                return _Win()
        desktop = _Desktop()

    w = locators.resolve(_Ctrl(), target)
    assert w.window_text() == "登入"


def test_desktop_crop_anchor_boundary():
    """裁 anchor 在螢幕邊角時仍安全(不超界)。"""
    with tempfile.TemporaryDirectory() as tmp:
        shot = _fake_screenshot(400, 300)
        out = os.path.join(tmp, "edge.png")
        _crop_anchor(shot, 5, 5, out)  # 左上角附近,half=50 會被裁到邊界
        from PIL import Image
        with Image.open(out) as im:
            # 左上角:left=0,top=0,right=55,bottom=55 → 55x55
            assert im.size == (55, 55), f"邊界裁切尺寸不符:{im.size}"
    print("[OK] desktop anchor 邊界裁切安全")


def test_uia_spec_drops_empty():
    """uia_element_to_spec 只保留非空欄位。"""
    spec = uia_element_to_spec({
        "name": "X", "control_type": "", "auto_id": None,
        "class_name": "Edit", "window_title": "",
    })
    assert spec == {"name": "X", "class_name": "Edit"}, spec
    print("[OK] uia_element_to_spec 正確丟棄空欄位")


# ===================================================================== runner
def main() -> int:
    print("=" * 64)
    print("RPA Studio - recorder smoke test")
    print("=" * 64)
    tests = [
        test_web_codegen_parse,
        test_web_codegen_ignores_boilerplate,
        test_desktop_capture_multi_locator,
        test_desktop_crop_anchor_boundary,
        test_uia_spec_drops_empty,
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
    print("  - web record_web() 需 subprocess 跑 playwright codegen 開瀏覽器讓人操作")
    print("  - desktop DesktopRecorder.start() 需 pynput 全域監聽 + pywinauto.from_point + mss 即時截圖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
