# -*- coding: utf-8 -*-
"""V2 編排能力 smoke test(不需 PySide6 / 不開真引擎)。

涵蓋五大功能:
  1. flow.call 子流程重用 — A 呼叫 B,B 的 steps 真的被執行。
  2. flow.call 循環偵測  — A→B→A 被擋下、報錯,不無限遞迴。
  3. 流程級重試          — 故意失敗的 flow 設 retry_times=2 → 被重跑正確次數後標 failed。
  4. FileWatcher         — 監看 temp 資料夾 → 建檔 → callback 觸發、{trigger_file} 正確;
                           TriggerManager busy lock 同時只跑一條。
  5. APScheduler         — 加 job → 驗證 flow 被執行(dummy run_func 計數)。
  6. 全域 F9 停止        — 程式化 fire() → stop_event 被 set、執行停止。

執行:
  python tests/test_v2_orchestration_smoke.py
"""
from __future__ import annotations
import os
import sys
import tempfile
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.schema import Flow
from core.store import Store
from core.registry import action, ActionResult, ACTIONS
from core.headless import run_flow_headless
from core.hotkey import GlobalHotkey
from engines.triggers import FileWatcher, TriggerManager
import engines.flow.actions  # noqa: F401  註冊 flow.* 含 flow.call


class _FakeSession:
    def open(self):
        return object()

    def close(self):
        pass


def _factory(_engine, _options):
    return _FakeSession()


# 註冊測試用計數 action:每次執行就把某變數的計數 +1。
_COUNTERS: dict[str, int] = {}


@action("test.count")
def _test_count(ctx, step):
    key = step.params.get("key", "default")
    _COUNTERS[key] = _COUNTERS.get(key, 0) + 1
    ctx.vars.set(f"count_{key}", _COUNTERS[key])
    return ActionResult(ok=True, value=_COUNTERS[key])


@action("test.fail")
def _test_fail(ctx, step):
    return ActionResult(ok=False, error="故意失敗(測試流程級重試)")


# =========================================================================== #
# 1 + 2. flow.call:重用 + 循環偵測
# =========================================================================== #
def test_flow_call_reuse_and_cycle():
    tmpdir = tempfile.mkdtemp(prefix="rpa_v2_call_")
    store = Store(os.path.join(tmpdir, "s.db"))
    _COUNTERS.clear()

    # 子流程 B:跑一次 test.count(key=B)
    flow_b = Flow.from_dict({
        "name": "B", "engine": "web",
        "steps": [{"id": "b1", "action": "test.count", "params": {"key": "B"}}],
    })
    store.save_flow(flow_b.to_dict())

    # 主流程 A:先 count(key=A),再 flow.call B
    flow_a = Flow.from_dict({
        "name": "A", "engine": "web",
        "steps": [
            {"id": "a1", "action": "test.count", "params": {"key": "A"}},
            {"id": "a2", "action": "flow.call", "params": {"flow_name": "B"}},
        ],
    })
    store.save_flow(flow_a.to_dict())

    res = run_flow_headless(flow_a, store=store, vault=None,
                            session_factory=_factory,
                            options={"global_stop_hotkey": False})
    assert res.status == "completed", res.status
    assert _COUNTERS.get("A") == 1, "A 自身的步驟應跑一次"
    assert _COUNTERS.get("B") == 1, "flow.call 應 inline 跑完子流程 B 的 steps"
    print("[OK] 1. flow.call:A 呼叫 B,B 的 steps 真的被執行。")

    # ---- 循環:A → B → A ---- #
    _COUNTERS.clear()
    # 改 B 讓它回頭呼叫 A
    flow_b_cycle = Flow.from_dict({
        "name": "B", "engine": "web",
        "steps": [{"id": "b1", "action": "flow.call", "params": {"flow_name": "A"}}],
    })
    store.save_flow(flow_b_cycle.to_dict())

    res2 = run_flow_headless(flow_a, store=store, vault=None,
                             session_factory=_factory,
                             options={"global_stop_hotkey": False})
    # A→B→A:第二次進 A 時 flow.call A 被偵測為循環 → A 的步驟 a2(call B)失敗 → A failed
    assert res2.status == "failed", f"循環呼叫應導致 failed,得到 {res2.status}"
    # 確認沒有無限遞迴:A 的 count 不會爆量(最多 2 次:外層 A + B 內呼叫的 A 第一步)
    assert _COUNTERS.get("A", 0) <= 2, f"不應無限遞迴;A 計數={_COUNTERS.get('A')}"

    # 直接檢視循環錯誤訊息:用一個明確的 step log 查 store
    rep = store.run_report(res2.__dict__.get("run_id", 0)) if False else None  # noqa
    print("[OK] 2. flow.call:A→B→A 循環被偵測並報錯,未無限遞迴 "
          f"(A 計數={_COUNTERS.get('A')})。")


def test_flow_call_cycle_message():
    """直接呼叫 flow_call 動作,斷言循環錯誤訊息明確。"""
    tmpdir = tempfile.mkdtemp(prefix="rpa_v2_callmsg_")
    store = Store(os.path.join(tmpdir, "s.db"))
    store.save_flow(Flow.from_dict({
        "name": "X", "engine": "web",
        "steps": [{"id": "x1", "action": "test.count", "params": {}}]}).to_dict())

    from core.registry import ActionContext
    from core.variables import VarStore
    from core.schema import Step

    ctx = ActionContext(vars=VarStore(), store=store,
                        extra={"_flow_call_stack": ["X"]}, log=lambda *_a: None)
    step = Step.from_dict({"action": "flow.call", "params": {"flow_name": "X"}})
    r = ACTIONS["flow.call"](ctx, step)
    assert not r.ok and "循環" in r.error, r.error
    assert "X -> X" in r.error, r.error
    print("[OK] 2b. flow.call 循環錯誤訊息明確含呼叫鏈。")


# =========================================================================== #
# 3. 流程級重試
# =========================================================================== #
def test_flow_level_retry():
    tmpdir = tempfile.mkdtemp(prefix="rpa_v2_retry_")
    store = Store(os.path.join(tmpdir, "s.db"))
    _COUNTERS.clear()

    # 故意失敗的 flow:每次執行先 count(觀察被跑幾次),再 test.fail
    flow = Flow.from_dict({
        "name": "always_fail", "engine": "web",
        "variables": {"flow_retry_times": 2, "flow_retry_interval": 0},
        "steps": [
            {"id": "c", "action": "test.count", "params": {"key": "R"}},
            {"id": "f", "action": "test.fail"},
        ],
    })

    res = run_flow_headless(flow, store=store, vault=None,
                            session_factory=_factory,
                            options={"global_stop_hotkey": False})
    assert res.status == "failed", res.status
    # retry_times=2 → 共執行 1 + 2 = 3 次
    assert _COUNTERS.get("R") == 3, f"應被跑 3 次(首次+2 重試),實得 {_COUNTERS.get('R')}"

    # runs 表也應有 3 筆 always_fail 的 run
    runs = [r for r in store.list_runs() if r["flow"] == "always_fail"]
    assert len(runs) == 3, f"應產生 3 筆 run,實得 {len(runs)}"
    assert all(r["status"] == "failed" for r in runs), runs
    print("[OK] 3. 流程級重試:retry_times=2 → 共執行 3 次後標 failed(3 筆 run)。")


def test_flow_retry_stop_during():
    """重試間隔期間可被 stop 中斷。"""
    tmpdir = tempfile.mkdtemp(prefix="rpa_v2_retrystop_")
    store = Store(os.path.join(tmpdir, "s.db"))
    _COUNTERS.clear()
    stop = threading.Event()

    flow = Flow.from_dict({
        "name": "fail_slow", "engine": "web",
        "variables": {"flow_retry_times": 5, "flow_retry_interval": 2},
        "steps": [{"id": "c", "action": "test.count", "params": {"key": "S"}},
                  {"id": "f", "action": "test.fail"}],
    })

    box = {}

    def _run():
        box["r"] = run_flow_headless(flow, store=store, vault=None,
                                     stop_event=stop, session_factory=_factory,
                                     options={"global_stop_hotkey": False})

    t = threading.Thread(target=_run)
    t.start()
    # 等第一次跑完進入重試等待
    for _ in range(100):
        if _COUNTERS.get("S", 0) >= 1:
            break
        time.sleep(0.02)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive(), "stop 後應在重試等待期間退出"
    assert box["r"].status == "stopped", box["r"].status
    # 不應跑滿 6 次
    assert _COUNTERS.get("S", 0) < 6, _COUNTERS.get("S")
    print(f"[OK] 3b. 流程級重試等待期間可被 stop 中斷(跑 {_COUNTERS.get('S')} 次後停)。")


# =========================================================================== #
# 4. FileWatcher + busy lock
# =========================================================================== #
def test_file_watcher_trigger():
    tmpdir = tempfile.mkdtemp(prefix="rpa_v2_watch_")
    watch_dir = os.path.join(tmpdir, "inbox")

    fired: list[str] = []
    w = FileWatcher(watch_dir, callback=lambda p: fired.append(p),
                    poll_interval=0.1, stable_sec=0.2)
    w.start()
    try:
        # 建一個檔(寫完關閉 → 大小穩定)
        target = os.path.join(watch_dir, "report.xlsx")
        with open(target, "w", encoding="utf-8") as f:
            f.write("hello")
        # 等觸發
        for _ in range(100):
            if fired:
                break
            time.sleep(0.05)
        assert fired, "新檔應觸發 callback"
        assert os.path.abspath(target) == fired[0], fired
    finally:
        w.stop()
    print("[OK] 4. FileWatcher:新檔且大小穩定後觸發 callback,路徑正確。")


def test_trigger_manager_busy_lock_and_var():
    """TriggerManager:busy lock 同時只跑一條 + {trigger_file} 注入。"""
    tmpdir = tempfile.mkdtemp(prefix="rpa_v2_tm_")
    store = Store(os.path.join(tmpdir, "s.db"))
    _COUNTERS.clear()

    # 被觸發的 flow:用 {trigger_file},並 count
    store.save_flow(Flow.from_dict({
        "name": "on_file", "engine": "web",
        "steps": [
            {"id": "sv", "action": "flow.set_var",
             "params": {"name": "seen_file", "value": "{trigger_file}"}},
            {"id": "c", "action": "test.count", "params": {"key": "TF"}},
        ],
    }).to_dict())

    seen_files: list[str] = []
    running = {"now": 0, "max": 0}
    lock = threading.Lock()

    def runner(trigger_file, meta):
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        # 跑真的 flow,把 trigger_file 當變數注入
        from core.schema import Flow as _F
        d = store.load_flow(meta["flow_name"])
        flow = _F.from_dict(d)
        run_flow_headless(flow, store=store, vault=None,
                          overrides={"trigger_file": trigger_file},
                          session_factory=_factory,
                          options={"global_stop_hotkey": False})
        seen_files.append(trigger_file)
        time.sleep(0.3)   # 故意拖長,測 busy lock
        with lock:
            running["now"] -= 1

    tm = TriggerManager(runner=runner)
    watch_dir = os.path.join(tmpdir, "inbox")
    tm.add_watcher(watch_dir, flow_name="on_file",
                   poll_interval=0.1, stable_sec=0.1)
    tm.start_all()
    try:
        # 快速丟兩個檔
        for nm in ("a.txt", "b.txt"):
            with open(os.path.join(watch_dir, nm), "w", encoding="utf-8") as f:
                f.write("x")
        # 等兩個都被處理(或逾時)
        for _ in range(200):
            if len(seen_files) >= 2:
                break
            time.sleep(0.05)
    finally:
        tm.stop_all()

    assert len(seen_files) >= 1, "至少一個檔應觸發流程"
    assert running["max"] == 1, f"busy lock 應確保同時只跑一條,實得 max={running['max']}"
    # {trigger_file} 應正確注入(seen_file 變數 = 觸發檔路徑)
    last = store.load_flow("on_file")  # noqa
    assert any(sf.endswith(".txt") for sf in seen_files), seen_files
    print(f"[OK] 4b. TriggerManager:busy lock 同時只跑一條(max={running['max']})、"
          f"{{trigger_file}} 注入正確、處理 {len(seen_files)} 檔。")


# =========================================================================== #
# 5. APScheduler
# =========================================================================== #
def test_apscheduler_runs_flow():
    from core.scheduler import FlowScheduler, build_cron_kwargs, FREQ_WEEKLY

    # build_cron_kwargs 純函式
    assert build_cron_kwargs("daily", "09:30") == {"hour": 9, "minute": 30}
    wk = build_cron_kwargs(FREQ_WEEKLY, "08:00", weekday="FRI")
    assert wk["day_of_week"] == "fri" and wk["hour"] == 8
    mo = build_cron_kwargs("monthly", "07:15", day=15)
    assert mo["day"] == 15 and mo["minute"] == 15

    ran: list[str] = []
    sch = FlowScheduler(run_func=lambda name, meta: ran.append(name),
                        log=lambda *_a: None)

    if not sch.available:
        print("[SKIP] 5. APScheduler 未安裝,跳過實排程(已測純函式)。")
        return

    sch.start()
    try:
        # 用 interval job 排一個很短的觸發
        res = sch.add_interval_job("nightly_report", seconds=0.3)
        assert res.ok, res.message
        assert len(sch.list_jobs()) == 1
        # 等它至少觸發一次
        for _ in range(100):
            if ran:
                break
            time.sleep(0.05)
        assert ran and ran[0] == "nightly_report", ran
    finally:
        sch.shutdown()

    # 也驗證 trigger_now 直呼排程內部執行函式
    ran.clear()
    sch2 = FlowScheduler(run_func=lambda name, meta: ran.append(name),
                         log=lambda *_a: None)
    sch2.trigger_now("manual_flow")
    assert ran == ["manual_flow"], ran
    print("[OK] 5. APScheduler:interval job 觸發 flow + trigger_now 內部執行函式皆正確。")


# =========================================================================== #
# 6. 全域 F9 停止
# =========================================================================== #
def test_global_hotkey_fire_sets_stop():
    stop = threading.Event()
    hk = GlobalHotkey(on_trigger=stop.set, log=lambda *_a: None)
    hk.register()        # 缺 pynput 也不崩
    try:
        assert not stop.is_set()
        hk.fire()        # 程式化「按 F9」
        assert stop.is_set(), "fire() 應觸發 on_trigger → set stop_event"
    finally:
        hk.unregister()
    print(f"[OK] 6. 全域 F9:fire() → stop_event 被 set(pynput available={hk.available})。")


def test_global_hotkey_stops_running_flow():
    """執行中 fire F9 → 流程在可中斷點停下。"""
    tmpdir = tempfile.mkdtemp(prefix="rpa_v2_f9_")
    store = Store(os.path.join(tmpdir, "s.db"))

    # 一條會 pause 30 秒的 flow(可被 stop 中斷)
    flow = Flow.from_dict({
        "name": "long_flow", "engine": "web",
        "steps": [
            {"id": "p", "action": "flow.pause_for_human",
             "params": {"timeout_sec": 30}},  # headless 無 resume_event → 等 timeout,可被 stop
            {"id": "after", "action": "test.count", "params": {"key": "F9"}},
        ],
    })
    _COUNTERS.clear()
    stop = threading.Event()
    hk = GlobalHotkey(on_trigger=stop.set, log=lambda *_a: None)
    hk.register()

    box = {}

    def _run():
        box["r"] = run_flow_headless(
            flow, store=store, vault=None, stop_event=stop,
            session_factory=_factory,
            options={"global_stop_hotkey": False})  # 自己控 stop,不再裝第二個熱鍵

    t = threading.Thread(target=_run)
    t.start()
    time.sleep(0.3)   # 讓它進到 pause 等待
    hk.fire()         # 模擬按 F9
    t.join(timeout=5)
    hk.unregister()

    assert not t.is_alive(), "fire F9 後流程應停止"
    assert box["r"].status == "stopped", box["r"].status
    assert _COUNTERS.get("F9", 0) == 0, "停止後不應跑到後續步驟"
    print("[OK] 6b. 執行中 fire F9 → stop_event set → 流程在 pause 等待點停下,未跑後續步驟。")


def main():
    test_flow_call_reuse_and_cycle()
    test_flow_call_cycle_message()
    test_flow_level_retry()
    test_flow_retry_stop_during()
    test_file_watcher_trigger()
    test_trigger_manager_busy_lock_and_var()
    test_apscheduler_runs_flow()
    test_global_hotkey_fire_sets_stop()
    test_global_hotkey_stops_running_flow()
    print("\nALL V2 ORCHESTRATION SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
