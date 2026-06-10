# -*- coding: utf-8 -*-
"""無人值守(unattended)+ 服務帳號 + headless 解耦 冒煙測試。

驗證四件事:
  1. headless wiring(core.headless.run_flow_headless)能跑不需瀏覽器的流程
     (no-op dummy session + flow.set_var),變數正確、status=completed。
  2. unattended:含 flow.pause_for_human 的流程在 unattended=True 下不阻塞、立即繼續,
     對比 attended(resume_event 在場且未 set)會卡住的行為。
  3. --var k=v 覆寫變數;--service-account 把 Vault secret 放進 ctx.extra。
  4. run_cli 模組在「假裝沒有 PySide6」時仍可 import 並跑(monkeypatch sys.modules
     讓 import PySide6 失敗,證明 headless 路徑完全不碰 ui.* / Qt)。

執行(系統 python,專案根):
    set PYTHONIOENCODING=utf-8
    python tests/test_unattended_smoke.py
全綠回 exit 0;任一失敗 raise AssertionError 並 exit 1。
"""
from __future__ import annotations
import os
import sys
import time
import threading
import builtins
import importlib
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.registry import action, ActionResult, ACTIONS  # noqa: E402
from core.schema import Flow, Step  # noqa: E402
from core.headless import run_flow_headless  # noqa: E402
import engines.flow.actions  # noqa: F401,E402  註冊 flow.*


# --------------------------------------------------------------------------- #
# 測試輔助
# --------------------------------------------------------------------------- #
class _FakeStore:
    """收集 run/step,不碰 SQLite。"""
    def __init__(self):
        self.runs = {}
        self.steps = []
        self._n = 0

    def start_run(self, flow_name):
        self._n += 1
        self.runs[self._n] = {"flow": flow_name, "status": "running", "vars": None}
        return self._n

    def finish_run(self, run_id, status, variables=None):
        self.runs[run_id]["status"] = status
        self.runs[run_id]["vars"] = variables

    def log_step(self, run_id, step_id, action, status, ms=0, retries=0,
                 error="", screenshot=""):
        self.steps.append((step_id, action, status))


class _NoopSession:
    """不需瀏覽器的假 session:open 回 None、close no-op。"""
    def open(self):
        return None

    def close(self):
        return None


def _noop_factory(engine, options):
    return _NoopSession()


def _flow(steps, variables=None, engine="web"):
    f = Flow(name="t", engine=engine, variables=variables or {})
    f.steps = [Step.from_dict(s) for s in steps]
    return f


@action("test.mark")
def _test_mark(ctx, step) -> ActionResult:
    """把 ctx.extra 的某個鍵存進變數,供斷言 extra 注入。"""
    key = step.params.get("extra_key")
    ctx.vars.set("marked", (ctx.extra or {}).get(key))
    return ActionResult(ok=True)


# --------------------------------------------------------------------------- #
# 1. headless wiring(dummy session + flow.set_var)
# --------------------------------------------------------------------------- #
def test_headless_wiring():
    store = _FakeStore()
    flow = _flow([
        {"action": "flow.set_var", "params": {"name": "foo", "value": "bar"}},
    ])
    res = run_flow_headless(flow, store=store, vault=None,
                            session_factory=_noop_factory)
    assert res.status == "completed", res
    assert res.variables.get("foo") == "bar", res.variables
    # run 有正常開關
    assert list(store.runs.values())[0]["status"] == "completed", store.runs
    print("[OK] headless wiring:dummy session + flow.set_var -> completed,變數正確")


# --------------------------------------------------------------------------- #
# 2. unattended:含 pause_for_human 不阻塞
# --------------------------------------------------------------------------- #
def test_unattended_pause_does_not_block():
    """unattended=True:即使 resume_event 在場且未 set(attended 會卡),也立即繼續。"""
    store = _FakeStore()
    ev = threading.Event()   # 未 set:attended 會卡在這
    flow = _flow([
        {"action": "flow.pause_for_human",
         "params": {"message": "請完成 MFA", "timeout_sec": 30}},
        {"action": "flow.set_var", "params": {"name": "after_pause", "value": "ok"}},
    ])
    t0 = time.time()
    res = run_flow_headless(
        flow, store=store, vault=None, session_factory=_noop_factory,
        extra={"resume_event": ev},   # 模擬 UI 留下的控制鍵
        unattended=True,
    )
    dt = time.time() - t0
    assert res.status == "completed", res
    assert dt < 1.0, f"unattended 應立即繼續(不等人),實際耗時 {dt:.2f}s"
    assert res.variables.get("after_pause") == "ok", res.variables
    print(f"[OK] unattended:pause_for_human 不阻塞、立即繼續(約 {dt:.2f}s)")


def test_attended_pause_blocks_then_resumes():
    """對比組:attended(unattended=False)有 resume_event 時會等到被 set 才繼續。"""
    store = _FakeStore()
    ev = threading.Event()
    flow = _flow([
        {"action": "flow.pause_for_human", "params": {"timeout_sec": 5}},
    ])

    def _resumer():
        time.sleep(0.3)
        ev.set()
    threading.Thread(target=_resumer, daemon=True).start()

    t0 = time.time()
    res = run_flow_headless(
        flow, store=store, vault=None, session_factory=_noop_factory,
        extra={"resume_event": ev}, unattended=False,
    )
    dt = time.time() - t0
    assert res.status == "completed", res
    assert dt >= 0.25, f"attended 應等到 resume_event 被 set(約 0.3s),實際 {dt:.2f}s"
    print(f"[OK] attended 對比:有 resume_event 時等人(約 {dt:.2f}s)")


# --------------------------------------------------------------------------- #
# 3. --var 覆寫 + --service-account 注入 ctx.extra
# --------------------------------------------------------------------------- #
def test_var_override():
    store = _FakeStore()
    # flow 預設 foo=default;overrides 覆寫成 OVERRIDDEN
    flow = _flow([{"action": "test.mark", "params": {"extra_key": "nope"}}],
                 variables={"foo": "default"})
    res = run_flow_headless(flow, store=store, vault=None,
                            session_factory=_noop_factory,
                            overrides={"foo": "OVERRIDDEN", "extra": "x"})
    assert res.variables.get("foo") == "OVERRIDDEN", res.variables
    print("[OK] --var 覆寫:overrides 優先於 flow 預設變數")


def test_service_account_injected():
    """--service-account:把 Vault secret 放進 ctx.extra['service_account']。"""
    class _Vault:
        def get_secret(self, name):
            return "P@ssw0rd!" if name == "svc_login" else None

    store = _FakeStore()
    flow = _flow([{"action": "test.mark",
                   "params": {"extra_key": "service_account"}}])
    res = run_flow_headless(
        flow, store=store, vault=_Vault(), session_factory=_noop_factory,
        service_account="svc_login",
    )
    marked = res.variables.get("marked")
    assert isinstance(marked, dict), f"service_account 應為 dict,實得 {marked!r}"
    assert marked.get("name") == "svc_login", marked
    assert marked.get("secret") == "P@ssw0rd!", "secret 應從 Vault 取出注入 ctx.extra"
    print("[OK] --service-account:Vault secret 注入 ctx.extra['service_account']")


# --------------------------------------------------------------------------- #
# 4. 假裝沒有 PySide6:run_cli 仍可 import + 跑
# --------------------------------------------------------------------------- #
def test_run_cli_without_pyside6():
    """monkeypatch import:讓 `import PySide6.*` 失敗,證明 headless 路徑不碰 ui/Qt。"""
    # 先清掉可能已載入的相關模組,確保重新 import 會走被攔截的路徑
    saved_mods = {}
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6.") \
                or name == "run_cli" or name.startswith("ui.") or name == "ui" \
                or name == "core.headless":
            saved_mods[name] = sys.modules.pop(name)

    real_import = builtins.__import__

    def _blocked_import(name, *a, **k):
        if name == "PySide6" or name.startswith("PySide6."):
            raise ModuleNotFoundError("No module named 'PySide6' (simulated)")
        return real_import(name, *a, **k)

    builtins.__import__ = _blocked_import
    try:
        # 確認 PySide6 真的 import 不到
        try:
            import PySide6  # noqa: F401
            raise AssertionError("PySide6 應該被攔截成 import 失敗")
        except ModuleNotFoundError:
            pass

        # run_cli 應可在無 PySide6 下 import
        run_cli = importlib.import_module("run_cli")

        # 跑一條不需瀏覽器的流程(注入 dummy session),走 main() 的覆寫變數 + unattended。
        # 用臨時 DB + 臨時 Vault 目錄,避免污染專案根。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            from core.store import Store
            from core.vault import Vault
            from core.schema import Flow as _Flow

            from core.schema import Step as _Step

            store = Store(os.path.join(d, "t.db"))
            vault = Vault(d)
            vault.set_secret("svc_acc", "secret-123")
            f = _Flow(name="cli_t", engine="web", variables={"k": "old"})
            f.steps = [
                _Step.from_dict({"action": "flow.pause_for_human",
                                 "params": {"timeout_sec": 30}}),
                _Step.from_dict({"action": "flow.set_var",
                                 "params": {"name": "k2", "value": "{k}"}}),
            ]
            store.save_flow(f.to_dict())

            # 注入 dummy session,讓 run_cli 不真的開瀏覽器:patch run_flow_headless 的 factory。
            # run_cli.main 不接受 session_factory,故直接驗證 run_flow_headless 行為等價,
            # 並用 main() 驗證參數解析(--var / --unattended / --service-account)不炸。
            rc = run_cli.run_flow_headless

            captured = {}

            def _patched(flow, **kw):
                captured.update(kw)
                kw["session_factory"] = _noop_factory
                result = rc(flow, **kw)
                captured["result"] = result
                return result

            run_cli.run_flow_headless = _patched
            try:
                # 把 DB / Vault 指向臨時目錄:patch Store/Vault 建構
                orig_store, orig_vault = run_cli.Store, run_cli.Vault
                run_cli.Store = lambda _p: store
                run_cli.Vault = lambda _p: vault
                try:
                    rc_code = run_cli.main([
                        "--flow", "cli_t",
                        "--var", "k=NEW",
                        "--unattended",
                        "--service-account", "svc_acc",
                    ])
                finally:
                    run_cli.Store, run_cli.Vault = orig_store, orig_vault
            finally:
                run_cli.run_flow_headless = rc

            assert rc_code == 0, f"run_cli.main 應回 0(completed),實得 {rc_code}"
            assert captured.get("unattended") is True, captured
            assert captured.get("overrides", {}).get("k") == "NEW", captured
            assert captured.get("service_account") == "svc_acc", captured
            result = captured.get("result")
            assert result is not None and result.status == "completed", result
            # k2 = {k} 替換後應為 NEW(--var 覆寫值)
            assert result.variables.get("k2") == "NEW", result.variables
        print("[OK] 假裝無 PySide6:run_cli 可 import + main() 跑完(headless 不碰 ui/Qt)")
    finally:
        builtins.__import__ = real_import
        # 還原被移除的模組(避免影響其他測試)
        for name in list(sys.modules):
            if name == "run_cli" or name == "core.headless" \
                    or name.startswith("ui.") or name == "ui":
                sys.modules.pop(name, None)
        sys.modules.update(saved_mods)


# --------------------------------------------------------------------------- #
def main():
    test_headless_wiring()
    test_unattended_pause_does_not_block()
    test_attended_pause_blocks_then_resumes()
    test_var_override()
    test_service_account_injected()
    test_run_cli_without_pyside6()
    print("\nALL GREEN")


if __name__ == "__main__":
    main()
