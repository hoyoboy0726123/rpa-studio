# -*- coding: utf-8 -*-
"""Offscreen UI smoke test。

驗證:
  1. 五個頁面都建得起來(MainWindow 完整初始化,不 exec 事件迴圈)。
  2. 用「假 action + 假 flow + 假 session」走一次 run_flow_once,驗證 RunWorker 的
     wiring 邏輯(get_session -> open -> ActionContext -> run_flow -> close -> finish_run),
     完全不碰真實 web/desktop 引擎。
  3. 引擎缺席時 run_flow_once 不會 crash,回傳 status='failed'(友善降級)。

執行:
  QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 python tests/test_ui_smoke.py
"""
from __future__ import annotations
import os
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication

from core.schema import Flow
from core.store import Store
from core.vault import Vault
from core.registry import action, ActionResult, ACTIONS
from ui.main_window import MainWindow
from ui.run_worker import run_flow_once


# ---- 假 action:記錄被呼叫次數 ---- #
_CALLS = {"test.noop": 0}


@action("test.noop")
def _noop(ctx, step):
    _CALLS["test.noop"] += 1
    ctx.log(f"noop ran: {step.id}")
    # 順便驗證 VarStore 可用
    ctx.vars.set("last_step", step.id)
    return ActionResult(ok=True, value=step.id)


# ---- 假 session:有 open()/close(),不碰真引擎 ---- #
class _FakeSession:
    def __init__(self, **opts):
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True
        return object()   # 假的 engine 活物件

    def close(self):
        self.closed = True


def _fake_factory(engine, options):
    return _FakeSession(**(options or {}))


def test_pages_build(app):
    tmpdb = os.path.join(tempfile.gettempdir(), "rpa_smoke_test.db")
    if os.path.exists(tmpdb):
        os.remove(tmpdb)
    store = Store(tmpdb)
    vault = Vault(tempfile.gettempdir())
    win = MainWindow(store=store, vault=vault)

    assert len(win.pages) == 9, f"expected 9 pages, got {len(win.pages)}"
    labels = [lbl for lbl, _ in win.pages]
    assert labels == ["流程清單", "編輯器", "流程圖", "錄製", "執行", "排程", "憑證", "日誌", "執行報表"], labels
    # stack 內每頁都實例化成功
    assert win.stack.count() == 9
    for _, page in win.pages:
        assert page is not None
    # 可切頁不爆
    for i in range(len(win.pages)):
        win._goto(i)
        assert win.stack.currentIndex() == i
    print("[OK] 九個頁面都建得起來:", labels)
    return store, vault


def test_runworker_wiring(store, vault):
    flow = Flow.from_dict({
        "name": "smoke_flow",
        "engine": "web",
        "variables": {"foo": "bar"},
        "steps": [
            {"id": "a", "action": "test.noop", "label": "step A"},
            {"id": "b", "action": "test.noop", "label": "step B"},
        ],
    })
    _CALLS["test.noop"] = 0
    progress_log = []

    result = run_flow_once(
        flow, store=store, vault=vault,
        stop_event=threading.Event(),
        overrides={"foo": "overridden"},
        on_progress=lambda i, t, s, r: progress_log.append((i, t, s.id)),
        log=lambda s: None,
        session_factory=_fake_factory,
    )

    assert _CALLS["test.noop"] == 2, f"action should run twice, ran {_CALLS['test.noop']}"
    assert result.status == "completed", result.status
    assert result.steps_ok == 2, result.steps_ok
    assert result.steps_failed == 0, result.steps_failed
    assert result.variables.get("foo") == "overridden", "覆寫變數應優先"
    assert len(progress_log) == 2, progress_log
    print("[OK] RunWorker wiring(fake session + fake action)走完兩步,覆寫變數生效。")


def test_engine_failure_graceful(store, vault):
    """引擎 import / 初始化失敗時不應 crash,應回傳 failed。"""
    def boom_factory(engine, options):
        raise ImportError("engines.web.session not installed (simulated)")

    flow = Flow.from_dict({
        "name": "smoke_fail",
        "engine": "web",
        "steps": [{"id": "a", "action": "test.noop"}],
    })
    logs = []
    result = run_flow_once(
        flow, store=store, vault=vault,
        stop_event=threading.Event(),
        log=logs.append,
        session_factory=boom_factory,
    )
    assert result.status == "failed", result.status
    assert any("無法啟動引擎" in m for m in logs), "should log friendly engine error"
    print("[OK] 引擎缺席時友善降級為 failed,不 crash。")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    store, vault = test_pages_build(app)
    test_runworker_wiring(store, vault)
    test_engine_failure_graceful(store, vault)
    print("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
