# -*- coding: utf-8 -*-
"""flow.* 動作 + runner 控制流 + MFA 人工暫停 冒煙測試。

不依賴 PySide6 / Playwright / pywinauto:直接組 ActionContext + run_flow,
engine 設 None(flow.* 與控制流都不需要引擎)。

執行(系統 python,專案根):
    python tests/test_flow_actions_smoke.py
全綠回 exit 0;任一失敗 raise AssertionError 並 exit 1。
"""
from __future__ import annotations
import os
import sys
import time
import json
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.registry import action, ActionContext, ActionResult, ACTIONS
from core.variables import VarStore
from core.schema import Flow, Step
from core.runner import run_flow
import engines.flow.actions  # noqa: F401  註冊 flow.*
from mock.server import MockServer


# --------------------------------------------------------------------------- #
# 測試輔助:假 store(收集 step log,不碰 SQLite)+ ctx 工廠 + 計數 action
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self):
        self.steps = []

    def log_step(self, run_id, step_id, action, status, ms=0, retries=0,
                 error="", screenshot=""):
        self.steps.append((step_id, action, status))


_COUNTER = {"n": 0}


@action("test.count")
def _test_count(ctx, step) -> ActionResult:
    _COUNTER["n"] += 1
    return ActionResult(ok=True, value=_COUNTER["n"])


def _make_ctx(vars_init=None, extra=None, stop_event=None, vault=None):
    return ActionContext(
        engine=None,
        vars=VarStore(vars_init or {}),
        vault=vault,
        store=_FakeStore(),
        run_id="t",
        stop_event=stop_event,
        log=lambda *_a, **_k: None,
        extra=extra or {},
    )


def _flow(steps):
    f = Flow(name="t", engine="web")
    f.steps = [Step.from_dict(s) for s in steps]
    return f


# --------------------------------------------------------------------------- #
# flow.set_var
# --------------------------------------------------------------------------- #
def test_set_var():
    ctx = _make_ctx()
    flow = _flow([{"action": "flow.set_var", "params": {"name": "foo", "value": "bar"}}])
    run_flow(flow, ctx)
    assert ctx.vars.get("foo") == "bar", ctx.vars.all()
    print("[OK] flow.set_var")


# --------------------------------------------------------------------------- #
# flow.prompt_user(無 cb 用 default)
# --------------------------------------------------------------------------- #
def test_prompt_user_default():
    ctx = _make_ctx()
    flow = _flow([{"action": "flow.prompt_user",
                   "params": {"var": "name", "message": "?", "default": "預設值"}}])
    run_flow(flow, ctx)
    assert ctx.vars.get("name") == "預設值", ctx.vars.all()
    print("[OK] flow.prompt_user 無 cb -> default")


def test_prompt_user_cb_and_secret():
    """有 prompt_cb 用回傳值;is_secret 同時寫進 vault。"""
    class _Vault:
        def __init__(self):
            self.store = {}

        def set_secret(self, name, value):
            self.store[name] = value

    v = _Vault()
    ctx = _make_ctx(extra={"prompt_cb": lambda msg, default, secret: "USER-OTP"},
                    vault=v)
    flow = _flow([{"action": "flow.prompt_user",
                   "params": {"var": "otp", "is_secret": True, "default": "x"}}])
    run_flow(flow, ctx)
    assert ctx.vars.get("otp") == "USER-OTP"
    assert v.store.get("otp") == "USER-OTP", v.store
    print("[OK] flow.prompt_user cb + is_secret -> vault")


# --------------------------------------------------------------------------- #
# flow.wait_file(建檔測)
# --------------------------------------------------------------------------- #
def test_wait_file():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "download.bin")
        # 另一執行緒 0.2s 後寫檔
        def _writer():
            time.sleep(0.2)
            with open(target, "wb") as f:
                f.write(b"hello world")
        threading.Thread(target=_writer, daemon=True).start()

        ctx = _make_ctx()
        flow = _flow([{"action": "flow.wait_file",
                       "params": {"path": target, "timeout_sec": 5,
                                  "stable_sec": 0.2, "var": "dl"}}])
        res = run_flow(flow, ctx)
        assert res.status == "completed", res
        assert os.path.exists(ctx.vars.get("dl")), ctx.vars.all()
    print("[OK] flow.wait_file 偵測到檔案出現且穩定")


def test_wait_file_timeout():
    with tempfile.TemporaryDirectory() as d:
        ctx = _make_ctx()
        flow = _flow([{"action": "flow.wait_file",
                       "params": {"path": os.path.join(d, "never"),
                                  "timeout_sec": 0.3, "stable_sec": 0.1}}])
        res = run_flow(flow, ctx)
        assert res.status == "failed", res
    print("[OK] flow.wait_file 逾時 -> failed")


# --------------------------------------------------------------------------- #
# flow.http(對本機 mock server)
# --------------------------------------------------------------------------- #
def test_http():
    server = MockServer(port=0)
    server.start()
    try:
        ctx = _make_ctx()
        flow = _flow([{"action": "flow.http",
                       "params": {"method": "GET",
                                  "url": server.base_url + "/login",
                                  "var": "page"}}])
        res = run_flow(flow, ctx)
        assert res.status == "completed", res
        assert ctx.vars.get("page_status") == 200, ctx.vars.get("page_status")
        assert "登入" in str(ctx.vars.get("page")), "回應內容不含預期字串"
    finally:
        server.stop()
    print("[OK] flow.http GET mock -> 200 + 內容存變數")


# --------------------------------------------------------------------------- #
# flow.pause_for_human
# --------------------------------------------------------------------------- #
def test_pause_headless_immediate():
    ctx = _make_ctx()
    flow = _flow([{"action": "flow.pause_for_human", "params": {"timeout_sec": 0}}])
    t0 = time.time()
    res = run_flow(flow, ctx)
    assert res.status == "completed", res
    assert (time.time() - t0) < 0.5, "headless timeout_sec=0 應立即繼續"
    print("[OK] flow.pause_for_human headless timeout=0 立即繼續")


def test_pause_resume_event():
    """另一執行緒 0.3s 後 set resume_event,pause 應被喚醒繼續。"""
    ev = threading.Event()
    on_pause_called = {"v": False}
    on_resume_called = {"v": False}
    ctx = _make_ctx(extra={
        "resume_event": ev,
        "on_pause": lambda msg: on_pause_called.__setitem__("v", True),
        "on_resume": lambda: on_resume_called.__setitem__("v", True),
    })
    flow = _flow([{"action": "flow.pause_for_human",
                   "params": {"message": "請完成 MFA", "timeout_sec": 5}}])

    def _resumer():
        time.sleep(0.3)
        ev.set()
    threading.Thread(target=_resumer, daemon=True).start()

    t0 = time.time()
    res = run_flow(flow, ctx)
    dt = time.time() - t0
    assert res.status == "completed", res
    assert 0.25 <= dt < 2.0, f"應在約 0.3s 被喚醒,實際 {dt:.2f}s"
    assert on_pause_called["v"] and on_resume_called["v"], "on_pause/on_resume 應被呼叫"
    print(f"[OK] flow.pause_for_human resume_event 喚醒(約 {dt:.2f}s)")


def test_pause_stop():
    """should_stop 期間暫停應回 stopped。"""
    ev = threading.Event()
    stop = threading.Event()
    ctx = _make_ctx(extra={"resume_event": ev}, stop_event=stop)
    flow = _flow([{"action": "flow.pause_for_human", "params": {"timeout_sec": 5}}])

    def _stopper():
        time.sleep(0.3)
        stop.set()
    threading.Thread(target=_stopper, daemon=True).start()

    res = run_flow(flow, ctx)
    assert res.status == "stopped", res
    print("[OK] flow.pause_for_human should_stop -> stopped")


# --------------------------------------------------------------------------- #
# runner 控制流:flow.if + flow.loop
# --------------------------------------------------------------------------- #
def test_if_skips_when_false():
    """條件不成立:跳過接下來 skip_count 個 step(計數器不應遞增)。"""
    _COUNTER["n"] = 0
    ctx = _make_ctx(vars_init={"mode": "B"})
    flow = _flow([
        {"action": "flow.if", "params": {"var": "mode", "op": "eq",
                                         "value": "A", "skip_count": 2}},
        {"action": "test.count"},   # 應被跳過
        {"action": "test.count"},   # 應被跳過
        {"action": "test.count"},   # 應執行
    ])
    run_flow(flow, ctx)
    assert _COUNTER["n"] == 1, f"條件 false 應只跑跳過區段之後的 1 個,實得 {_COUNTER['n']}"
    print("[OK] flow.if 條件不成立 -> 跳過 skip_count 個 step")


def test_if_runs_when_true():
    _COUNTER["n"] = 0
    ctx = _make_ctx(vars_init={"mode": "A"})
    flow = _flow([
        {"action": "flow.if", "params": {"var": "mode", "op": "eq",
                                         "value": "A", "skip_count": 2}},
        {"action": "test.count"},
        {"action": "test.count"},
        {"action": "test.count"},
    ])
    run_flow(flow, ctx)
    assert _COUNTER["n"] == 3, f"條件 true 應全跑,實得 {_COUNTER['n']}"
    print("[OK] flow.if 條件成立 -> 不跳過")


def test_loop_count():
    """flow.loop count=N:把接下來 body_count 個 step 重複 N 次。"""
    _COUNTER["n"] = 0
    ctx = _make_ctx()
    flow = _flow([
        {"action": "flow.loop", "params": {"count": 3, "body_count": 2}},
        {"action": "test.count"},
        {"action": "test.count"},
        {"action": "test.count"},   # body 外,只跑 1 次
    ])
    run_flow(flow, ctx)
    # body(2 個)x 3 次 = 6,加 body 外 1 個 = 7
    assert _COUNTER["n"] == 7, f"應為 2x3 + 1 = 7,實得 {_COUNTER['n']}"
    print("[OK] flow.loop count -> body 重複 N 次")


def test_loop_for_each():
    """flow.loop for_each_var:對 list 每個元素跑一次,元素寫進迴圈變數。"""
    _COUNTER["n"] = 0
    seen = []

    @action("test.collect")
    def _collect(ctx, step):
        seen.append(ctx.vars.get("item"))
        return ActionResult(ok=True)

    ctx = _make_ctx(vars_init={"rows": ["a", "b", "c"]})
    flow = _flow([
        {"action": "flow.loop", "params": {"for_each_var": "rows",
                                           "body_count": 1, "var": "item"}},
        {"action": "test.collect"},
    ])
    run_flow(flow, ctx)
    assert seen == ["a", "b", "c"], seen
    print("[OK] flow.loop for_each -> 逐元素跑、元素入迴圈變數")


def test_nested_loop():
    """巢狀 loop:外 2 次、內 3 次 -> body 共跑 6 次。"""
    _COUNTER["n"] = 0
    ctx = _make_ctx()
    flow = _flow([
        {"action": "flow.loop", "params": {"count": 2, "body_count": 3}},  # 外圈,body=內loop+body
        {"action": "flow.loop", "params": {"count": 3, "body_count": 1}},  # 內圈
        {"action": "test.count"},                                          # 內 body
        {"action": "test.count"},                                          # 外圈內、內圈外
    ])
    # 外圈 body = [內loop, 內body(test.count), 額外 test.count] 共 3 個 step
    # 內圈 body(1 個 test.count)x3 = 3,再加外圈那 1 個 test.count,= 4;外圈 x2 = 8
    run_flow(flow, ctx)
    assert _COUNTER["n"] == 8, f"巢狀應為 (3+1)x2 = 8,實得 {_COUNTER['n']}"
    print("[OK] flow.loop 巢狀(loop_stack)")


# --------------------------------------------------------------------------- #
def main():
    test_set_var()
    test_prompt_user_default()
    test_prompt_user_cb_and_secret()
    test_wait_file()
    test_wait_file_timeout()
    test_http()
    test_pause_headless_immediate()
    test_pause_resume_event()
    test_pause_stop()
    test_if_skips_when_false()
    test_if_runs_when_true()
    test_loop_count()
    test_loop_for_each()
    test_nested_loop()
    print("\nALL GREEN")


if __name__ == "__main__":
    main()
