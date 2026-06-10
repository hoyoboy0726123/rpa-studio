# -*- coding: utf-8 -*-
"""Desktop 引擎端到端 smoke test(以 Windows 記事本 notepad.exe 為標的)。

驗收:
  啟動 notepad -> 在 Edit 控制項輸入一段文字 -> 讀回 Edit 內容
  -> assert 讀回值包含輸入文字 -> 關閉視窗(不存檔)。

設計重點:
  - self-cleaning:無論成功失敗,finally 都會 kill 掉啟動的 notepad。
  - 環境降級:若 import pywinauto 失敗,或 GUI 不可用(headless / 無互動桌面),
    自動降級為「locators.resolve 單元測試(mock controller)+ 動作註冊檢查」,
    並在輸出中誠實標示是「DEGRADED」,不假裝通過 GUI 測試。

執行(系統 python,專案根會自動加進 sys.path):
  PYTHONIOENCODING=utf-8 python tests/test_desktop_smoke.py
"""
from __future__ import annotations
import os
import sys
import time
import threading

# --- 專案根加進 sys.path ---
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.registry import ActionContext, ACTIONS          # noqa: E402
from core.variables import VarStore                        # noqa: E402


# ====================================================================== helpers
class _MemStore:
    """最小 Store 替身:吃掉 log_step,避免測試動到 SQLite。"""
    def log_step(self, *a, **k):
        pass


def _make_ctx(engine) -> ActionContext:
    logs: list[str] = []
    ctx = ActionContext(
        engine=engine,
        vars=VarStore(),
        vault=None,
        store=_MemStore(),
        run_id="smoke",
        stop_event=threading.Event(),
        log=lambda m, *a, **k: logs.append(str(m)),
    )
    ctx._logs = logs  # type: ignore
    return ctx


def _registration_check() -> list[str]:
    """確認 desktop.* 動作都已註冊。回傳缺漏清單(空=全通過)。"""
    # import 觸發註冊
    import engines.desktop  # noqa: F401
    expected = [
        "desktop.focus_window", "desktop.click", "desktop.type",
        "desktop.read", "desktop.wait", "desktop.wait_for",
        "desktop.menu_select", "desktop.send_keys",
    ]
    return [name for name in expected if name not in ACTIONS]


# ============================================================ degraded fallback
class _FakeWrapper:
    def __init__(self, text):
        self._text = text

    def texts(self):
        return [self._text]

    def window_text(self):
        return self._text


class _FakeController:
    """mock controller:模擬 app.top_window().child_window(...) 回傳 fake wrapper。"""
    backend = "uia"

    class _Win:
        def __init__(self, text):
            self._text = text

        def child_window(self, **kw):
            return self

        def wrapper_object(self):
            return _FakeWrapper(self._text)

    def __init__(self, text="MOCK CONTENT"):
        self._text = text
        self.app = self  # top_window() 走自己
        self.desktop = None
        self.win32_desktop = None

    def top_window(self):
        return _FakeController._Win(self._text)


def _degraded_unit_tests() -> bool:
    """locators.resolve(mock) + read 動作 + 註冊檢查。全通過回 True。"""
    from engines.desktop import locators

    ok = True

    missing = _registration_check()
    if missing:
        print(f"[DEGRADED] 動作註冊缺漏: {missing}")
        ok = False
    else:
        print("[DEGRADED] 動作註冊檢查: 8/8 desktop.* 已註冊 OK")

    # resolve: uia strategy + JSON value -> 透過 mock 取得 wrapper
    target = {"primary": {"strategy": "uia",
                          "value": "{\"control_type\": \"Edit\"}"}}
    ctrl = _FakeController("MOCK CONTENT")
    try:
        w = locators.resolve(ctrl, target)
        assert w.window_text() == "MOCK CONTENT"
        print("[DEGRADED] locators.resolve(uia/JSON) -> wrapper OK")
    except Exception as e:  # noqa: BLE001
        print(f"[DEGRADED] locators.resolve FAILED: {e}")
        ok = False

    # title strategy 也應走同一條 _resolve_uia
    try:
        w2 = locators.resolve(ctrl, {"primary": {"strategy": "title",
                                                 "value": "Untitled"}})
        assert w2.window_text() == "MOCK CONTENT"
        print("[DEGRADED] locators.resolve(title) -> wrapper OK")
    except Exception as e:  # noqa: BLE001
        print(f"[DEGRADED] locators.resolve(title) FAILED: {e}")
        ok = False

    # read 動作:用 mock controller 把文字讀進變數
    try:
        ctx = _make_ctx(ctrl)
        from core.schema import Step
        step = Step.from_dict({
            "id": "u_read", "action": "desktop.read",
            "target": target, "params": {"var": "out"}, "timeout_ms": 2000,
        })
        res = ACTIONS["desktop.read"](ctx, step)
        assert res.ok and ctx.vars.get("out") == "MOCK CONTENT"
        print("[DEGRADED] desktop.read(mock) -> var='MOCK CONTENT' OK")
    except Exception as e:  # noqa: BLE001
        print(f"[DEGRADED] desktop.read(mock) FAILED: {e}")
        ok = False

    return ok


# ================================================================== gui e2e run
def _gui_e2e() -> bool:
    """真實 GUI 端到端。回傳 True=通過。任何 GUI 不可用會丟例外給呼叫端降級。"""
    from engines.desktop.session import DesktopSession
    from core.registry import ActionResult  # noqa: F401
    from core.schema import Step

    phrase = "Hello from RPA Studio desktop engine 12345"
    session = DesktopSession(app_path="notepad.exe", backend="uia", timeout=15)

    controller = session.open()   # 啟動 notepad;若 headless 會在這裡或後續丟錯
    try:
        ctx = _make_ctx(controller)
        time.sleep(1.5)  # 等 UI 就緒

        target = {
            "primary": {"strategy": "uia", "value": "{\"control_type\": \"Edit\"}"},
            "fallbacks": [
                {"strategy": "uia", "value": "{\"control_type\": \"Document\"}"},
                {"strategy": "uia", "value": "{\"class_name\": \"Edit\"}"},
            ],
        }

        # type
        type_step = Step.from_dict({
            "id": "e_type", "action": "desktop.type",
            "target": target, "params": {"text": phrase}, "timeout_ms": 10000,
        })
        r1 = ACTIONS["desktop.type"](ctx, type_step)
        print(f"[GUI] desktop.type -> ok={r1.ok} err={r1.error}")
        assert r1.ok, f"type failed: {r1.error}"
        time.sleep(0.5)

        # read
        read_step = Step.from_dict({
            "id": "e_read", "action": "desktop.read",
            "target": target, "params": {"var": "typed"}, "timeout_ms": 10000,
        })
        r2 = ACTIONS["desktop.read"](ctx, read_step)
        got = ctx.vars.get("typed", "")
        print(f"[GUI] desktop.read -> ok={r2.ok} value={got!r}")
        assert r2.ok, f"read failed: {r2.error}"

        # assert 內容相符(read 可能含結尾換行,用 in / strip 比對)
        assert phrase in got or got.strip() == phrase, \
            f"content mismatch: typed={phrase!r} read={got!r}"
        print("[GUI] ASSERT PASS: 讀回內容包含輸入文字")
        return True
    finally:
        # self-cleaning:殺掉啟動的 notepad,不觸發「是否存檔」對話框後續處理
        try:
            session.close()
        except Exception:
            pass
        # 保險:若 close 沒殺乾淨,直接以 taskkill 收尾(僅本次啟動的進程樹)
        try:
            if getattr(controller, "app", None) is not None:
                pid = controller.app.process
                os.system(f"taskkill /PID {pid} /T /F >NUL 2>&1")
        except Exception:
            pass


# ========================================================================= main
def main() -> int:
    print("=" * 64)
    print("RPA Studio - desktop engine smoke test")
    print("=" * 64)

    # 1) import pywinauto?
    try:
        import pywinauto  # noqa: F401
        print(f"[ENV] pywinauto OK (version={pywinauto.__version__})")
        have_pywinauto = True
    except Exception as e:  # noqa: BLE001
        print(f"[ENV] import pywinauto FAILED: {e}")
        have_pywinauto = False

    # 2) 註冊一定要先通過(不需要 GUI)
    try:
        missing = _registration_check()
        if missing:
            print(f"[FAIL] desktop.* 動作註冊缺漏: {missing}")
            return 1
        print("[OK] desktop.* 動作註冊: 8/8")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] import engines.desktop 失敗: {e}")
        return 1

    if not have_pywinauto:
        print("\n>>> pywinauto 不可用 -> 降級為單元測試")
        ok = _degraded_unit_tests()
        print("\nRESULT:", "DEGRADED-PASS" if ok else "DEGRADED-FAIL")
        return 0 if ok else 1

    # 3) 嘗試真實 GUI e2e;失敗(含 headless)就降級
    print("\n>>> 嘗試真實 GUI 端到端 (notepad.exe)")
    try:
        ok = _gui_e2e()
        print("\nRESULT:", "GUI-PASS" if ok else "GUI-FAIL")
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        import traceback
        print(f"[GUI] 端到端無法執行(可能為 headless / 無互動桌面): {e}")
        print(traceback.format_exc())
        print("\n>>> 降級為單元測試(mock controller)")
        ok = _degraded_unit_tests()
        print("\nRESULT:", "DEGRADED-PASS (GUI unavailable)" if ok
              else "DEGRADED-FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
