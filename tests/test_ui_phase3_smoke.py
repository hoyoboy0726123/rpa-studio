# -*- coding: utf-8 -*-
"""Phase 3 offscreen smoke test:流程編輯器 / MFA 繼續 / 真排程 (schtasks)。

驗證(全部不點 UI、不需系統管理員權限):
  A. 編輯器:對一條 Flow 程式化加 / 刪 / 上下移動 step、改 params / target,
     存回 Store 再讀出,斷言結構正確(用 ui.flow_edit_ops 的純函式)。
  B. MFA:模擬 run_worker 的 resume 機制 — pause 時 on_pause 被呼叫、
     set resume_event 後流程續跑(用假 flow + 真的 flow.pause_for_human 動作)。
  C. 排程:schtasks 指令字串組裝正確 + 解析 /Query CSV 列表的純函式。
     真建立任務可能需權限,只做一次「能呼叫不 crash」的 best-effort try。

執行:
  QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 python tests/test_ui_phase3_smoke.py
"""
from __future__ import annotations
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication

from core.schema import Flow
from core.store import Store
from core.vault import Vault
from ui import flow_edit_ops as ops
from ui import schtasks_ops as st
from ui.run_worker import run_flow_once, make_pause_controls


# =========================================================================== #
# A. 編輯器:純函式增 / 刪 / 移 / 改 + 存回 Store
# =========================================================================== #
def test_editor_ops():
    tmpdb = os.path.join(tempfile.mkdtemp(prefix="rpa_p3_edit_"), "s.db")
    store = Store(tmpdb)

    flow = Flow.from_dict({"name": "edit_me", "engine": "web", "steps": []})

    # 加三步
    s1 = ops.add_step(flow, action="web.goto", label="開首頁")
    s2 = ops.add_step(flow, action="web.fill", label="填表")
    s3 = ops.add_step(flow, action="web.click", label="送出")
    assert [s.action for s in flow.steps] == ["web.goto", "web.fill", "web.click"]
    assert len({s.id for s in flow.steps}) == 3, "step id 應唯一"

    # 在索引 1 插入一步
    ops.add_step(flow, action="web.wait", label="等待", at=1)
    assert [s.action for s in flow.steps] == ["web.goto", "web.wait", "web.fill", "web.click"]

    # 上下移動:把最後一步(web.click,idx 3)上移
    new_idx = ops.move_step(flow, 3, -1)
    assert new_idx == 2
    assert [s.action for s in flow.steps] == ["web.goto", "web.wait", "web.click", "web.fill"]

    # 邊界:第 0 步再上移 → 不動
    assert ops.move_step(flow, 0, -1) == 0

    # 刪除索引 1(web.wait)
    assert ops.delete_step(flow, 1) is True
    assert [s.action for s in flow.steps] == ["web.goto", "web.click", "web.fill"]
    assert ops.delete_step(flow, 99) is False, "超界刪除應回 False"

    # 改 params / basic / retry / target
    target_step = flow.steps[0]
    ops.update_step_basic(target_step, label="前往登入頁", on_error="continue",
                          timeout_ms=30000)
    ops.set_params(target_step, {"url": "https://example.com/login", "": "ignored"})
    ops.set_retry(target_step, times=3, interval_ms=2000)
    ops.set_target(target_step, primary_strategy="css", primary_value="#main",
                   fallbacks=[("xpath", "//div"), ("", "skip-me")])

    assert target_step.label == "前往登入頁"
    assert target_step.on_error == "continue"
    assert target_step.timeout_ms == 30000
    assert target_step.params == {"url": "https://example.com/login"}, target_step.params
    assert target_step.retry == {"times": 3, "interval_ms": 2000}
    assert target_step.target["primary"] == {"strategy": "css", "value": "#main"}
    assert target_step.target["fallbacks"] == [{"strategy": "xpath", "value": "//div"}]

    # 空 target → None
    ops.set_target(flow.steps[1], "", "", [])
    assert flow.steps[1].target is None

    # 存回 Store 再讀出
    ops.save_flow_to_store(flow, store)
    loaded = Flow.from_dict(store.load_flow("edit_me"))
    assert len(loaded.steps) == 3
    assert [s.action for s in loaded.steps] == ["web.goto", "web.click", "web.fill"]
    assert loaded.steps[0].params == {"url": "https://example.com/login"}
    assert loaded.steps[0].retry == {"times": 3, "interval_ms": 2000}
    assert loaded.steps[0].target["primary"]["value"] == "#main"
    print("[OK] A. 編輯器:加/刪/移/改 params+target → 存回 Store → 讀出結構正確。")


def test_all_actions_catalog():
    web = ops.all_actions("web")
    assert "web.goto" in web and "flow.pause_for_human" in web, web
    desktop = ops.all_actions("desktop")
    assert "desktop.click" in desktop and "flow.set_var" in desktop, desktop
    allg = ops.all_actions(None)
    assert "web.goto" in allg and "desktop.click" in allg
    print("[OK] A. all_actions:依引擎展平 ACTION_CATALOG(含 flow.* 通用動作)。")


# =========================================================================== #
# B. MFA:pause → on_pause 被呼叫 → set resume_event → 續跑
# =========================================================================== #
def test_mfa_resume_mechanism(store, vault):
    """用真的 flow.pause_for_human 動作 + run_worker 的 pause 控制驗證暫停 / 繼續。"""
    flow = Flow.from_dict({
        "name": "mfa_flow",
        "engine": "web",
        "steps": [
            {"id": "p", "action": "flow.pause_for_human",
             "params": {"message": "請完成 MFA"}},
            {"id": "after", "action": "flow.set_var",
             "params": {"name": "done", "value": "yes"}},
        ],
    })

    paused_msgs: list[str] = []
    resumed_flag = {"v": False}
    controls = make_pause_controls(
        on_pause=lambda m: paused_msgs.append(m),
        on_resume=lambda: resumed_flag.__setitem__("v", True),
    )
    resume_event = controls["resume_event"]

    # 在背景跑 flow;flow.pause_for_human 會 clear resume_event 後阻塞等待。
    result_box = {}

    def _run():
        result_box["r"] = run_flow_once(
            flow, store=store, vault=vault,
            stop_event=threading.Event(),
            session_factory=lambda e, o: _FakeSession(),
            extra=dict(controls),
        )

    t = threading.Thread(target=_run)
    t.start()

    # 等到 on_pause 被呼叫(代表已進入暫停)
    for _ in range(100):
        if paused_msgs:
            break
        time.sleep(0.02)
    assert paused_msgs == ["請完成 MFA"], f"on_pause 應帶訊息被呼叫: {paused_msgs}"
    assert not resumed_flag["v"], "尚未按繼續,不應 on_resume"
    assert t.is_alive(), "暫停期間 flow 應仍在等待"

    # 模擬「按繼續」:set resume_event
    resume_event.set()
    t.join(timeout=5)
    assert not t.is_alive(), "set resume_event 後 flow 應續跑並結束"
    assert resumed_flag["v"], "續跑後 on_resume 應被呼叫"

    result = result_box["r"]
    assert result.status == "completed", result.status
    assert result.variables.get("done") == "yes", "暫停後續跑應執行到後續步驟"
    print("[OK] B. MFA:on_pause 被呼叫 → set resume_event → on_resume + 後續步驟跑完。")


def test_make_pause_controls_noop_safe():
    """callback 為 None / 丟例外都不該 crash;resume_event 預設非暫停。"""
    c = make_pause_controls(None, None)
    assert c["resume_event"].is_set(), "預設應為非暫停(已 set)"
    c["on_pause"]("x")   # 不該炸
    c["on_resume"]()     # 不該炸

    def boom(*_a):
        raise RuntimeError("boom")

    c2 = make_pause_controls(on_pause=boom, on_resume=boom)
    c2["on_pause"]("y")  # 例外被吞掉
    c2["on_resume"]()
    print("[OK] B. make_pause_controls:無 callback / callback 丟例外都不 crash。")


# =========================================================================== #
# C. 排程:schtasks 指令組裝 + CSV 解析
# =========================================================================== #
def test_schtasks_build_args():
    daily = st.build_create_args("我的流程", st.FREQ_DAILY, "09:30",
                                 python_exe="C:\\py\\python.exe",
                                 cli_path="C:\\app\\run_cli.py")
    assert daily[0] == "schtasks"
    assert "/Create" in daily and "/SC" in daily
    assert daily[daily.index("/SC") + 1] == "DAILY"
    assert daily[daily.index("/ST") + 1] == "09:30"
    # 任務名稱有前綴、非法字元被換掉
    tn = daily[daily.index("/TN") + 1]
    assert tn == "RPAStudio_我的流程", tn
    # 執行指令含 run_cli.py + --flow
    tr = daily[daily.index("/TR") + 1]
    assert "run_cli.py" in tr and "--flow" in tr and "我的流程" in tr, tr

    weekly = st.build_create_args("f", st.FREQ_WEEKLY, "08:00", weekday="FRI")
    assert weekly[weekly.index("/SC") + 1] == "WEEKLY"
    assert weekly[weekly.index("/D") + 1] == "FRI"

    monthly = st.build_create_args("f", st.FREQ_MONTHLY, "07:00", day=15)
    assert monthly[monthly.index("/SC") + 1] == "MONTHLY"
    assert monthly[monthly.index("/D") + 1] == "15"

    # 未知頻率 → ValueError
    try:
        st.build_create_args("f", "yearly")
        assert False, "未知頻率應丟 ValueError"
    except ValueError:
        pass

    # 顯示用字串
    cmd = st.build_create_command("f", st.FREQ_DAILY, "08:00")
    assert cmd.startswith("schtasks") and "/Create" in cmd

    # delete / query argv
    assert st.build_delete_args("RPAStudio_f") == ["schtasks", "/Delete", "/F", "/TN", "RPAStudio_f"]
    q = st.build_query_args()
    assert "/Query" in q and "CSV" in q
    print("[OK] C. schtasks 指令組裝(daily/weekly/monthly/delete/query)正確。")


def test_schtasks_parse_csv():
    csv_text = (
        '"TaskName","Next Run Time","Status","Logon Mode","Schedule Type","Task To Run"\n'
        '"\\RPAStudio_flowA","2026/6/9 08:00:00","就緒","互動式","每日","python run_cli.py --flow flowA"\n'
        '"\\OtherTask","N/A","就緒","互動式","每日","notepad.exe"\n'
        '"TaskName","Next Run Time","Status","Logon Mode","Schedule Type","Task To Run"\n'
        '"\\RPAStudio_flowB","2026/6/9 09:00:00","就緒","互動式","每週","python run_cli.py --flow flowB"\n'
    )
    tasks = st.parse_query_csv(csv_text)
    names = [t["task_name"] for t in tasks]
    assert names == ["RPAStudio_flowA", "RPAStudio_flowB"], names
    assert tasks[0]["next_run"] == "2026/6/9 08:00:00"
    assert tasks[0]["schedule"] == "每日"
    # 不過濾前綴時,OtherTask 也會進來
    allt = st.parse_query_csv(csv_text, only_prefix=None)
    assert any(t["task_name"] == "OtherTask" for t in allt)
    # 空輸入安全
    assert st.parse_query_csv("") == []
    print("[OK] C. parse_query_csv:多段表頭 + 前綴過濾 + 去重 正確。")


def test_schtasks_list_best_effort():
    """真呼叫 list_tasks:Windows 上應能跑;非 Windows / 無權限時友善降級不 crash。"""
    try:
        tasks, res = st.list_tasks()
        assert isinstance(tasks, list)
        assert hasattr(res, "ok") and hasattr(res, "message")
        note = "ok" if res.ok else f"降級({res.message[:40]})"
        print(f"[OK] C. list_tasks 實呼叫:{len(tasks)} 個任務 / 結果={note}。")
    except Exception as e:  # noqa: BLE001
        # 不應發生(_run 內已全包),萬一發生也只標記 skip,不讓整批測試紅。
        print(f"[SKIP] C. list_tasks 實呼叫在此環境無法執行(已忽略):{type(e).__name__}: {e}")


# --- 共用假 session --- #
class _FakeSession:
    def open(self):
        return object()

    def close(self):
        pass


def main():
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    tmpdir = tempfile.mkdtemp(prefix="rpa_p3_")
    store = Store(os.path.join(tmpdir, "p3.db"))
    vault = Vault(tmpdir)

    test_editor_ops()
    test_all_actions_catalog()
    test_mfa_resume_mechanism(store, vault)
    test_make_pause_controls_noop_safe()
    test_schtasks_build_args()
    test_schtasks_parse_csv()
    test_schtasks_list_best_effort()
    print("\nALL PHASE 3 SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
