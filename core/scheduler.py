# -*- coding: utf-8 -*-
"""In-app 排程 (APScheduler)。

定位
----
與既有的 Windows schtasks(ui/schtasks_ops.py + schedule_page)**並存、互補**:

  - schtasks         : 作業系統層級。**RPA Studio 程式沒開也會跑**(由 Windows 工作排程
                       器在背景拉起 run_cli.py)。適合真正的無人值守機器。
  - APScheduler(本檔): 應用程式層級。**只在 RPA Studio 程式開著時**生效;程式關掉,排程
                       就停。適合「我整天開著工作站,想讓它每小時自動跑一次」這種 attended
                       情境,且不需要系統管理員權限去動工作排程器。

本模組用 APScheduler 的 BackgroundScheduler(背景 thread,不卡 UI),支援每日 / 每週 /
每月 cron。觸發時呼叫 core.headless.run_flow_headless 以 headless 方式跑指定 flow。

缺 APScheduler 不崩
-------------------
APScheduler 為選用相依。未安裝時 FlowScheduler 仍可建立,但 start()/add_*() 會回報
available=False 並記 log,不丟例外、不讓 app 崩。
"""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Callable

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    _APS_AVAILABLE = True
except Exception:  # noqa: BLE001
    BackgroundScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    _APS_AVAILABLE = False


# 週幾 → APScheduler day_of_week 縮寫
_WEEKDAY = {
    "MON": "mon", "TUE": "tue", "WED": "wed", "THU": "thu",
    "FRI": "fri", "SAT": "sat", "SUN": "sun",
}

FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
FREQ_MONTHLY = "monthly"


@dataclass
class ScheduleResult:
    ok: bool
    message: str = ""
    job_id: str = ""


def _parse_hhmm(time_s: str) -> tuple[int, int]:
    """'08:30' -> (8, 30)。容錯:壞字串退回 (8, 0)。"""
    try:
        hh, mm = time_s.strip().split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:  # noqa: BLE001
        return 8, 0


def build_cron_kwargs(freq: str, time_s: str = "08:00",
                      weekday: str | None = None, day: int | None = None) -> dict:
    """把 UI 的頻率設定轉成 CronTrigger 的關鍵字參數。純函式,方便測試。

    daily   -> {hour, minute}
    weekly  -> {day_of_week, hour, minute}
    monthly -> {day, hour, minute}
    未知頻率 → ValueError。
    """
    hour, minute = _parse_hhmm(time_s)
    if freq == FREQ_DAILY:
        return {"hour": hour, "minute": minute}
    if freq == FREQ_WEEKLY:
        dow = _WEEKDAY.get((weekday or "MON").upper(), "mon")
        return {"day_of_week": dow, "hour": hour, "minute": minute}
    if freq == FREQ_MONTHLY:
        d = int(day or 1)
        d = max(1, min(31, d))
        return {"day": d, "hour": hour, "minute": minute}
    raise ValueError(f"未知頻率:{freq!r}(可用:daily/weekly/monthly)")


class FlowScheduler:
    """APScheduler 包裝:在程式開著時定時跑 flow。

    參數:
      store, vault   : 傳給 run_flow_headless。
      run_func       : 選用,(flow_name, job_meta) -> None;預設用 _default_run 走
                       headless 執行。測試可注入 dummy run_func 直接計數,不碰真引擎。
      log            : (str) -> None。
    """

    def __init__(self, store=None, vault=None,
                 run_func: Callable[[str, dict], None] | None = None,
                 log: Callable[[str], None] | None = None):
        self.store = store
        self.vault = vault
        self.log = log or (lambda *_a, **_k: None)
        self._run_func = run_func or self._default_run
        self._scheduler = BackgroundScheduler() if _APS_AVAILABLE else None
        # 同時只跑一條:in-app 排程觸發時也尊重 busy lock(與 FileWatcher 相同精神)。
        self._busy = threading.Lock()
        self._started = False

    @property
    def available(self) -> bool:
        return _APS_AVAILABLE and self._scheduler is not None

    # ------------------------------------------------------------------ #
    def _default_run(self, flow_name: str, job_meta: dict):
        """預設執行:從 store 載入 flow 並以 headless 跑(unattended)。"""
        from core.schema import Flow
        from core.headless import run_flow_headless

        if self.store is None:
            self.log(f"[scheduler] 無 store,無法載入 flow '{flow_name}'。")
            return
        d = self.store.load_flow(flow_name)
        if not d:
            self.log(f"[scheduler] 找不到 flow '{flow_name}'。")
            return
        flow = Flow.from_dict(d)
        run_flow_headless(
            flow, store=self.store, vault=self.vault,
            stop_event=threading.Event(),
            log=self.log, unattended=True,
        )

    def _job_wrapper(self, flow_name: str, job_meta: dict):
        """APScheduler job 的實際進入點:套 busy lock(同時只跑一條)+ 包例外。"""
        if not self._busy.acquire(blocking=False):
            self.log(f"[scheduler] 已有流程在跑,跳過本次觸發:{flow_name}")
            return
        try:
            self.log(f"[scheduler] 觸發排程流程:{flow_name}")
            self._run_func(flow_name, job_meta)
        except Exception as e:  # noqa: BLE001
            self.log(f"[scheduler] 流程執行錯誤:{type(e).__name__}: {e}")
            self.log(traceback.format_exc())
        finally:
            self._busy.release()

    # ------------------------------------------------------------------ #
    def start(self) -> ScheduleResult:
        if not self.available:
            return ScheduleResult(False, "APScheduler 未安裝(pip install apscheduler)。")
        if not self._started:
            self._scheduler.start()
            self._started = True
        return ScheduleResult(True, "排程器已啟動。")

    def shutdown(self, wait: bool = False):
        if self.available and self._started:
            try:
                self._scheduler.shutdown(wait=wait)
            except Exception:  # noqa: BLE001
                pass
            self._started = False

    def add_flow_job(self, flow_name: str, freq: str, time_s: str = "08:00",
                     weekday: str | None = None, day: int | None = None,
                     job_id: str | None = None) -> ScheduleResult:
        """加一個 cron job(每日 / 每週 / 每月)跑指定 flow。"""
        if not self.available:
            return ScheduleResult(False, "APScheduler 未安裝(pip install apscheduler)。")
        try:
            cron_kwargs = build_cron_kwargs(freq, time_s, weekday, day)
        except ValueError as e:
            return ScheduleResult(False, str(e))
        jid = job_id or f"flow::{flow_name}::{freq}::{time_s}::{weekday or ''}::{day or ''}"
        try:
            self._scheduler.add_job(
                self._job_wrapper, trigger=CronTrigger(**cron_kwargs),
                args=[flow_name, {"flow_name": flow_name, "freq": freq}],
                id=jid, replace_existing=True, misfire_grace_time=300,
            )
        except Exception as e:  # noqa: BLE001
            return ScheduleResult(False, f"加排程失敗:{type(e).__name__}: {e}")
        return ScheduleResult(True, f"已排程 '{flow_name}'({freq} {time_s})。", jid)

    def add_interval_job(self, flow_name: str, seconds: float,
                         job_id: str | None = None) -> ScheduleResult:
        """加一個「每 N 秒」的 interval job(主要供測試 / 高頻情境用)。"""
        if not self.available:
            return ScheduleResult(False, "APScheduler 未安裝。")
        jid = job_id or f"flow_interval::{flow_name}::{seconds}"
        try:
            self._scheduler.add_job(
                self._job_wrapper, trigger="interval", seconds=float(seconds),
                args=[flow_name, {"flow_name": flow_name, "freq": "interval"}],
                id=jid, replace_existing=True,
            )
        except Exception as e:  # noqa: BLE001
            return ScheduleResult(False, f"加排程失敗:{type(e).__name__}: {e}")
        return ScheduleResult(True, f"已排程 '{flow_name}'(每 {seconds}s)。", jid)

    def trigger_now(self, flow_name: str, job_meta: dict | None = None):
        """同步立即執行一次(供 UI「立即執行」/ 測試直呼排程內部執行函式)。"""
        self._job_wrapper(flow_name, job_meta or {"flow_name": flow_name})

    def remove_job(self, job_id: str) -> ScheduleResult:
        if not self.available:
            return ScheduleResult(False, "APScheduler 未安裝。")
        try:
            self._scheduler.remove_job(job_id)
        except Exception as e:  # noqa: BLE001
            return ScheduleResult(False, f"移除排程失敗:{type(e).__name__}: {e}")
        return ScheduleResult(True, "已移除排程。", job_id)

    def list_jobs(self) -> list[dict]:
        if not self.available:
            return []
        out = []
        for j in self._scheduler.get_jobs():
            out.append({
                "id": j.id,
                "name": getattr(j, "name", ""),
                "next_run": str(getattr(j, "next_run_time", "")),
                "trigger": str(j.trigger),
            })
        return out
