# -*- coding: utf-8 -*-
"""RunWorker — 在背景執行緒跑一條 flow,不卡 UI 主執行緒。

wiring 流程(對應核心契約):
  get_session(flow.engine, options)   # engine_api,lazy import
    -> session.open()                 # 取得引擎活物件 (Playwright Page / 桌面控制器)
    -> ActionContext(...)             # VarStore(預設變數+覆寫) / Vault / Store / stop_event / log
    -> store.start_run(flow.name)     # run_id
    -> run_flow(flow, ctx, on_progress=emit)
    -> session.close()
    -> store.finish_run(run_id, status, variables)

signals:
  progress(int i, int total, str label)   # 每步回報
  log(str)                                 # 文字日誌
  finished(object RunResult)               # 跑完(或失敗)回報

設計重點:
- engine import 可能失敗(web/desktop 引擎由別人並行開發),用 try/except 包起來,
  失敗時 log 友善訊息、發出 status='failed' 的 RunResult、不要 crash UI。
- 真正的 wiring 抽到模組級函式 run_flow_once(),可被測試直接呼叫,不必真的開執行緒。
- Stop 按鈕呼叫 worker.request_stop() → set stop_event,讓 runner 在可中斷點停下。
"""
from __future__ import annotations
import threading
from typing import Callable

from PySide6.QtCore import QThread, Signal

from core.runner import RunResult
from core.headless import run_flow_headless

# 引擎無關的 flow/data/comms 動作:集中註冊。缺席時 try 包好,不讓 UI / CLI crash。
try:
    from core.actions_bootstrap import register_builtin_actions
    register_builtin_actions()
except Exception:  # noqa: BLE001
    pass


def make_pause_controls(
    on_pause: Callable[[str], None] | None = None,
    on_resume: Callable[[], None] | None = None,
) -> dict:
    """建立 MFA「人工介入 → 繼續」所需的控制物件,放進 ActionContext.extra。

    flow.pause_for_human(engines/flow/actions.py)約定的 ctx.extra 鍵:
        resume_event : threading.Event,被 set 代表「人工已完成、繼續」。
        on_pause(message) : 進入暫停時呼叫(UI 顯示 PAUSED + 「繼續」鈕)。
        on_resume()       : 結束暫停時呼叫(UI 收掉 PAUSED)。

    本函式把 UI 的 on_pause / on_resume 用 no-op 包好(callback 為 None 或丟例外
    都不會 crash);resume_event 預設為非暫停狀態。
    回傳 {resume_event, on_pause, on_resume},直接 update 進 extra 即可。
    """
    resume_event = threading.Event()
    resume_event.set()   # 預設非暫停(flow.pause_for_human 會在進入時 clear)

    def _pause(message: str = ""):
        if on_pause is not None:
            try:
                on_pause(message)
            except Exception:  # noqa: BLE001
                pass

    def _resume():
        if on_resume is not None:
            try:
                on_resume()
            except Exception:  # noqa: BLE001
                pass

    return {
        "resume_event": resume_event,
        "on_pause": _pause,
        "on_resume": _resume,
    }


def run_flow_once(
    flow,
    store,
    vault,
    stop_event: threading.Event,
    overrides: dict | None = None,
    options: dict | None = None,
    on_progress: Callable | None = None,
    log: Callable[[str], None] | None = None,
    session_factory: Callable | None = None,
    extra: dict | None = None,
) -> RunResult:
    """同步執行一條 flow 並回傳 RunResult。UI 與測試共用的純函式。

    session_factory(engine, options) -> session;預設用 core.engine_api.get_session,
    測試可注入一個假的 factory(回傳有 open()/close() 的 dummy session),
    完全不碰真實 web/desktop 引擎。

    實際 wiring 抽到 core.headless.run_flow_headless(不依賴 Qt),UI 與 CLI 共用同一條;
    attended(UI)路徑透過 extra 帶入 MFA pause 控制,unattended=False(預設),
    flow.pause_for_human 仍會等人。
    """
    return run_flow_headless(
        flow,
        store=store,
        vault=vault,
        stop_event=stop_event,
        overrides=overrides,
        options=options,
        on_progress=on_progress,
        log=log,
        session_factory=session_factory,
        extra=extra,
    )


class RunWorker(QThread):
    """QThread 包裝 run_flow_once,把進度/日誌/結果以 Qt signal 丟回 UI。"""

    progress = Signal(int, int, str)     # (i, total, label)
    log = Signal(str)
    finished = Signal(object)            # RunResult
    paused = Signal(str)                 # MFA:進入暫停,帶提示訊息
    resumed = Signal()                   # MFA:已繼續

    def __init__(self, flow, store, vault, overrides=None, options=None,
                 session_factory=None, extra=None, parent=None):
        super().__init__(parent)
        self.flow = flow
        self.store = store
        self.vault = vault
        self.overrides = overrides or {}
        self.options = options or {}
        self.session_factory = session_factory
        self.stop_event = threading.Event()
        # MFA 暫停控制:on_pause/on_resume 把 worker 訊號丟回 UI 主執行緒。
        # 注意:這裡 emit 的 signal 預設為 queued connection(跨執行緒安全)。
        self._pause_controls = make_pause_controls(
            on_pause=self.paused.emit,
            on_resume=self.resumed.emit,
        )
        # 與外部傳入的 extra 合併(pause 控制鍵優先,確保 MFA 一定可用)。
        self.extra = dict(extra or {})
        self.extra.update(self._pause_controls)

    def request_stop(self):
        """Stop 按鈕呼叫:set stop_event,runner 會在可中斷點停下。
        若正卡在 MFA 暫停,也一併 set resume_event 讓等待迴圈退出。"""
        self.stop_event.set()
        ev = self._pause_controls.get("resume_event")
        if ev is not None:
            ev.set()
        self.log.emit("已要求停止…(將在目前步驟結束後中止)")

    def resume(self):
        """「繼續」按鈕呼叫:set resume_event,讓 flow.pause_for_human 的等待退出。"""
        ev = self._pause_controls.get("resume_event")
        if ev is not None:
            ev.set()
        self.log.emit("已按「繼續」,流程恢復執行。")

    def _on_progress(self, i, total, step, result):
        label = step.label or step.action
        self.progress.emit(i + 1, total, label)

    def run(self):  # QThread 進入點
        result = run_flow_once(
            self.flow,
            store=self.store,
            vault=self.vault,
            stop_event=self.stop_event,
            overrides=self.overrides,
            options=self.options,
            on_progress=self._on_progress,
            log=self.log.emit,
            session_factory=self.session_factory,
            extra=self.extra,
        )
        self.finished.emit(result)
