# -*- coding: utf-8 -*-
"""Self-healing(自癒定位)smoke test。

涵蓋:
  1. core.heal 評分純函式:文字相似度 + role 命中 + 屬性命中 的加權挑選。
  2. desktop heal:用 mock UIA 候選樹測 score_candidates 挑選邏輯(不開真 GUI)。
  3. store.log_heal:寫入 / 讀出 heal_logs。
  4. web heal(若 Playwright + chromium 可用):本機 stdlib http server 提供一頁
     HTML,primary 用「故意錯」的 selector,fingerprint 給正確 text/role,
     斷言 heal 找回正確元素、score 合理、heal_logs 有寫入。
     Playwright 不可用時 graceful 標 SKIP,不讓整體變紅。

執行(系統 python,專案根會自動加進 sys.path):
  PYTHONIOENCODING=utf-8 python tests/test_heal_smoke.py
全綠回 exit 0;任一硬性測試失敗回 exit 1。
"""
from __future__ import annotations
import os
import sys
import json
import threading
import tempfile
import http.server
import socketserver

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ============================================================ 1) 評分純函式測試
def test_heal_scoring() -> bool:
    from core import heal

    ok = True

    # text 相似度:完全相同 -> 1.0;不相干 -> 低
    assert heal.text_similarity("登入", "登入") == 1.0
    assert heal.text_similarity("Submit", "submit") == 1.0  # 大小寫不敏感
    assert heal.text_similarity("登入", "登出") < 1.0

    # 三訊號齊全:完全命中 -> 接近 1.0
    fp = {"text": "送出表單", "role": "button",
          "attrs": {"testid": "submit-btn"}}
    perfect = {"text": "送出表單", "role": "button",
               "attrs": {"testid": "submit-btn"}}
    s_perfect, d = heal.score_candidate(fp, perfect)
    assert s_perfect > 0.99, f"完全命中應接近 1.0,實得 {s_perfect}"

    # 只有文字接近、role 不符、屬性不符 -> 應低於門檻 0.7
    partial = {"text": "送出表單", "role": "link", "attrs": {"testid": "other"}}
    s_partial, _ = heal.score_candidate(fp, partial)
    assert s_partial < 0.7, f"只有文字命中應低於門檻,實得 {s_partial}"
    assert s_partial > 0, "文字命中應有正分"

    # best_candidate:從一批裡挑最高且過門檻
    cands = [
        {"text": "取消", "role": "button", "attrs": {}},
        {"text": "送出表單", "role": "button", "attrs": {"testid": "submit-btn"}},
        {"text": "說明", "role": "link", "attrs": {}},
    ]
    idx, score, detail = heal.best_candidate(fp, cands, threshold=0.7)
    assert idx == 1, f"應挑中 index 1,實得 {idx}"
    assert score > 0.99, f"挑中分數應高,實得 {score}"

    # 全部都不像 -> 不過門檻,idx=None
    bad = [{"text": "abc", "role": "link", "attrs": {}},
           {"text": "xyz", "role": "link", "attrs": {}}]
    idx2, score2, _ = heal.best_candidate(fp, bad, threshold=0.7)
    assert idx2 is None, f"全不像應回 None,實得 idx={idx2} score={score2}"

    # fingerprint 只有 text(其他訊號缺)也能評分(權重正規化)
    fp_text_only = {"text": "登入"}
    s_only, _ = heal.score_candidate(fp_text_only, {"text": "登入", "role": "x"})
    assert s_only > 0.99, f"只有 text 的 fingerprint 完全命中應接近 1.0,實得 {s_only}"

    print("[OK] core.heal 評分:text 相似度 + role 命中 + 屬性命中 加權挑選正確")
    return ok


# ====================================== 2) desktop heal:mock UIA 候選樹挑選
def test_desktop_heal_selection() -> bool:
    from engines.desktop import locators

    # 模擬掃 UIA 樹得到的候選:(candidate_dict, payload)
    # payload 用字串代表「該 wrapper」,驗證挑中後回傳的是對應 payload。
    candidates = [
        (locators.wrapper_to_candidate(_FakeUIA("取消", "Button", "cancelBtn")),
         "WRAPPER_CANCEL"),
        (locators.wrapper_to_candidate(_FakeUIA("確定送出", "Button", "okBtn")),
         "WRAPPER_OK"),
        (locators.wrapper_to_candidate(_FakeUIA("檔案清單", "List", "fileList")),
         "WRAPPER_LIST"),
    ]
    # fingerprint(spec 的巢狀格式 -> resolve 內會攤平;這裡直接給扁平給 score)
    fp = {"text": "確定送出", "control_type": "Button",
          "auto_id": "okBtn", "role": "Button"}

    payload, score, detail = locators.score_candidates(fp, candidates, threshold=0.7)
    assert payload == "WRAPPER_OK", f"desktop heal 應挑中 OK wrapper,實得 {payload!r}"
    assert score > 0.9, f"完全命中分數應高,實得 {score}"
    print(f"[OK] desktop heal(mock UIA 樹):挑中正確候選 payload={payload} "
          f"score={score:.3f}")

    # 全不像 -> 不過門檻
    fp_miss = {"text": "完全不存在的按鈕XYZ", "control_type": "Hyperlink"}
    payload2, score2, _ = locators.score_candidates(fp_miss, candidates,
                                                    threshold=0.7)
    assert payload2 is None, f"全不像應回 None,實得 {payload2!r} score={score2}"
    print(f"[OK] desktop heal:全不像時不過門檻(best={score2:.3f})不誤抓")

    # _fingerprint_for_heal:spec 巢狀 fingerprint -> 扁平
    target = {"fingerprint": {"uia": {"name": "確定送出", "control_type": "Button",
                                      "auto_id": "okBtn"}}}
    flat = locators._fingerprint_for_heal(target)
    assert flat.get("text") == "確定送出" and flat.get("control_type") == "Button"
    assert flat.get("auto_id") == "okBtn"
    print("[OK] desktop _fingerprint_for_heal:巢狀 uia fingerprint 正確攤平")
    return True


class _FakeUIA:
    """mock pywinauto wrapper:提供 window_text() + element_info(含 control_type 等)。"""
    class _Elem:
        def __init__(self, control_type, auto_id, class_name):
            self.control_type = control_type
            self.automation_id = auto_id
            self.class_name = class_name

    def __init__(self, text, control_type, auto_id, class_name=""):
        self._text = text
        self.element_info = _FakeUIA._Elem(control_type, auto_id, class_name)

    def window_text(self):
        return self._text

    def class_name(self):
        return self.element_info.class_name


# ============================================ 3) store.log_heal 寫入 / 讀出
def test_store_log_heal() -> bool:
    from core.store import Store

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = os.path.join(tmp, "heal.db")
        store = Store(db)

        # 既有表不應被破壞:能正常 start_run / log_step
        rid = store.start_run("demo_flow")
        store.log_step(rid, "s1", "web.click", "ok", 12, 0)

        store.log_heal(rid, "s2", "heal(web)", 0.82,
                       {"score": 0.82, "candidate": {"text": "登入"}})
        store.log_heal(rid, "s3", "heal(desktop)", 0.91, "plain detail string")

        rows = store.list_heals(rid)
        assert len(rows) == 2, f"應有 2 筆 heal log,實得 {len(rows)}"
        # detail(dict)應被序列化成 JSON 字串並可解析回來
        r_web = [r for r in rows if r["step_id"] == "s2"][0]
        assert r_web["strategy_used"] == "heal(web)"
        assert abs(r_web["score"] - 0.82) < 1e-6
        parsed = json.loads(r_web["detail"])
        assert parsed["candidate"]["text"] == "登入"
        # 字串 detail 原樣保存
        r_dt = [r for r in rows if r["step_id"] == "s3"][0]
        assert r_dt["detail"] == "plain detail string"

        # run_report 仍可用(既有功能未壞)
        rep = store.run_report(rid)
        assert rep["run"]["flow"] == "demo_flow"
        assert len(rep["steps"]) == 1
    print("[OK] store.log_heal:寫入/讀出正確、detail dict 自動 JSON 化、既有表未壞")
    return True


# ==================================================== 4) web heal(Playwright)
_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>heal test</title>
</head><body>
  <h1>Heal Demo</h1>
  <button id="orig-login" data-testid="login-btn">登入系統</button>
  <button id="cancel">取消</button>
  <a href="#" id="help-link">說明文件</a>
  <div id="result">尚未點擊</div>
  <script>
    document.getElementById('orig-login').addEventListener('click', function(){
      document.getElementById('result').innerText = 'LOGIN_CLICKED';
    });
    document.getElementById('cancel').addEventListener('click', function(){
      document.getElementById('result').innerText = 'CANCEL_CLICKED';
    });
  </script>
</body></html>"""


def _start_html_server(html: str):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{port}/"


def test_web_heal() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] Playwright 不可用,略過 web heal e2e: {e}")
        return True

    from core.registry import ActionContext, ACTIONS
    from core.variables import VarStore
    from core.schema import Step
    from core.store import Store
    import engines.web.actions  # noqa: F401  觸發 web.* 動作註冊

    httpd, url = _start_html_server(_HTML)
    tmp = tempfile.mkdtemp(prefix="webheal_")
    db = os.path.join(tmp, "heal.db")
    store = Store(db)
    rid = store.start_run("web_heal_demo")

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:  # noqa: BLE001
                print(f"[SKIP] chromium 啟動失敗,略過 web heal e2e: {e}")
                return True
            page = browser.new_page()
            page.goto(url)

            ctx = ActionContext(
                engine=page, vars=VarStore(), vault=None, store=store,
                run_id=rid, stop_event=threading.Event(),
                log=lambda *a, **k: None,
                extra={"heal_enabled": True, "heal_threshold": 0.7},
            )

            # primary 故意錯(指向不存在的 selector),fallback 也錯,
            # fingerprint 給正確的 text + role + testid -> 應靠 heal 找回登入鈕。
            step = Step.from_dict({
                "id": "click_login",
                "action": "web.click",
                "target": {
                    "primary": {"strategy": "css", "value": "#does-not-exist-123"},
                    "fallbacks": [{"strategy": "testid", "value": "WRONG-TESTID"}],
                    "fingerprint": {
                        "text": "登入系統",
                        "role": "button",
                        "attrs": {"testid": "login-btn"},
                    },
                },
                "timeout_ms": 5000,
            })

            res = ACTIONS["web.click"](ctx, step)
            assert res.ok, f"web.click(heal) 應成功,實得 error={res.error}"

            # 斷言點到的是「登入鈕」而非取消鈕
            clicked = page.locator("#result").inner_text()
            assert clicked == "LOGIN_CLICKED", \
                f"heal 應點到登入鈕,result={clicked!r}"

            # heal_logs 有寫入、strategy / score 合理
            heals = store.list_heals(rid)
            assert len(heals) == 1, f"應寫入 1 筆 heal log,實得 {len(heals)}"
            h = heals[0]
            assert h["strategy_used"] == "heal(web)", h["strategy_used"]
            assert h["score"] >= 0.7, f"heal score 應 >= 門檻 0.7,實得 {h['score']}"
            assert h["step_id"] == "click_login"
            detail = json.loads(h["detail"])
            assert "parts" in detail, "heal detail 應含評分明細 parts"
            print(f"[OK] web heal e2e:故意錯 selector -> 靠 fingerprint 找回登入鈕"
                  f"(score={h['score']:.3f}),heal_logs 已寫入")

            # 對照組:heal 關閉時應失敗(證明確實是 heal 救回來的)
            ctx_off = ActionContext(
                engine=page, vars=VarStore(), vault=None, store=store,
                run_id=rid, stop_event=threading.Event(),
                log=lambda *a, **k: None,
                extra={"heal_enabled": False},
            )
            # web.click 在 resolve 失敗時會丟例外(由 runner 接;這裡直接捕捉)
            failed_without_heal = False
            try:
                r = ACTIONS["web.click"](ctx_off, step)
                failed_without_heal = (r is not None and not r.ok)
            except Exception:
                failed_without_heal = True
            assert failed_without_heal, "heal 關閉時故意錯的 selector 應失敗"
            print("[OK] web heal 對照組:heal 關閉 -> 同一 step 失敗(證明是 heal 救回)")

            browser.close()
        return True
    finally:
        httpd.shutdown()


# ========================================================================= main
def main() -> int:
    print("=" * 64)
    print("RPA Studio - self-healing smoke test")
    print("=" * 64)
    failures = []

    for name, fn in [
        ("heal_scoring", test_heal_scoring),
        ("desktop_heal_selection", test_desktop_heal_selection),
        ("store_log_heal", test_store_log_heal),
        ("web_heal", test_web_heal),
    ]:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"[FAIL] {name}: {e}")
            print(traceback.format_exc())
            failures.append(name)

    print("-" * 64)
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failed: {failures})")
        return 1
    print("ALL GREEN ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
