# -*- coding: utf-8 -*-
"""檔案觸發 FileWatcher + TriggerManager。

設計目標
--------
讓「新檔丟進某資料夾」自動觸發一條 flow。為了零額外相依(不裝 watchdog)且跨平台,
採**輪詢**(polling)而非 OS 事件:每隔 poll_interval 秒掃一次目標資料夾。

「新檔且大小穩定」偵測
----------------------
偵測到一個未處理過的檔後,**不立即觸發**——先連續觀察它的大小,直到大小在
stable_sec 秒內不再變動,才視為「寫入完成」並觸發 callback。這可避免在檔案還在
被複製 / 下載到一半時就搶著處理(常見的 race condition)。

觸發時 callback 收到絕對檔路徑;TriggerManager 會把它放進執行變數 {trigger_file}。

全域 busy lock
--------------
TriggerManager 持有一個全域鎖:同一時間只允許一條觸發流程在跑。若某 watcher 觸發時
另一條流程仍在執行,該檔會被「延後」——留在 pending 佇列,下一輪輪詢再嘗試(此時檔仍
存在且穩定,會再次被當成候選)。這對單機 RPA 很關鍵:多條流程同時搶 UI / 瀏覽器會互相
干擾。

執行緒模型
----------
每個 FileWatcher 跑在自己的 daemon thread,迴圈中睡 poll_interval。stop() 設旗標 +
join。所有 callback 例外都被吞掉並走 on_error(預設記 log),不讓 watcher thread 死掉。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable


class FileWatcher:
    """輪詢監看單一資料夾的檔案觸發器。

    參數:
      folder         : 監看的資料夾(會自動建立)。
      callback       : (abspath:str) -> None;偵測到穩定新檔時呼叫。
      patterns       : 副檔名過濾,如 ['.xlsx', '.csv'];None / 空 = 不過濾。
      poll_interval  : 輪詢間隔秒(預設 1.0)。
      stable_sec     : 檔案大小需連續穩定的秒數(預設 1.0)才算寫入完成。
      process_existing: True → 連啟動當下「已存在」的檔也算新檔觸發;
                        False(預設)→ 啟動時先把現有檔記成「已知」,只觸發之後新增的。
      gate           : 選用 callable() -> bool;回 False 時暫不觸發(busy lock 用),
                       候選檔留待下一輪。由 TriggerManager 注入。
      on_error       : (exc) -> None;callback 丟例外時呼叫(預設 no-op)。
    """

    def __init__(
        self,
        folder: str,
        callback: Callable[[str], None],
        patterns: list[str] | None = None,
        poll_interval: float = 1.0,
        stable_sec: float = 1.0,
        process_existing: bool = False,
        gate: Callable[[], bool] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ):
        self.folder = os.path.abspath(folder)
        self.callback = callback
        self.patterns = [p.lower() for p in (patterns or [])]
        self.poll_interval = max(0.05, float(poll_interval))
        self.stable_sec = max(0.0, float(stable_sec))
        self.process_existing = bool(process_existing)
        self.gate = gate
        self.on_error = on_error or (lambda _e: None)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 已成功觸發 / 啟動前既有(process_existing=False 時)的檔 → 不重複觸發
        self._handled: set[str] = set()
        # 候選檔的大小觀察:path -> (last_size, first_seen_stable_ts)
        self._observing: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------ #
    def _matches(self, name: str) -> bool:
        if not self.patterns:
            return True
        low = name.lower()
        return any(low.endswith(p) for p in self.patterns)

    def _list_candidates(self) -> list[str]:
        try:
            names = os.listdir(self.folder)
        except OSError:
            return []
        out = []
        for n in names:
            p = os.path.join(self.folder, n)
            if os.path.isfile(p) and self._matches(n):
                out.append(p)
        return out

    def _gate_open(self) -> bool:
        if self.gate is None:
            return True
        try:
            return bool(self.gate())
        except Exception:  # noqa: BLE001
            return True

    def scan_once(self) -> list[str]:
        """掃一輪。回傳本輪「實際觸發」的檔案路徑清單(供測試 / 同步使用)。

        流程:對每個候選檔,觀察其大小是否已穩定 stable_sec 秒;穩定且 gate 開 →
        觸發 callback 並標記 handled。gate 關(busy)→ 維持觀察、留待下一輪。
        """
        fired: list[str] = []
        now = time.time()
        candidates = self._list_candidates()
        current = set(candidates)

        # 清掉已消失的觀察項
        for gone in [p for p in self._observing if p not in current]:
            self._observing.pop(gone, None)

        for path in candidates:
            if path in self._handled:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue

            prev = self._observing.get(path)
            if prev is None or prev[0] != size:
                # 第一次看到 / 大小仍在變 → (重新)開始穩定計時
                self._observing[path] = (size, now)
                continue

            stable_for = now - prev[1]
            if stable_for < self.stable_sec:
                continue   # 還沒穩定夠久

            # 已穩定:看 gate(busy lock)
            if not self._gate_open():
                continue   # 忙碌中,下一輪再試(仍保留觀察狀態)

            # 觸發
            self._handled.add(path)
            self._observing.pop(path, None)
            try:
                self.callback(path)
                fired.append(path)
            except Exception as e:  # noqa: BLE001
                try:
                    self.on_error(e)
                except Exception:  # noqa: BLE001
                    pass
        return fired

    # ------------------------------------------------------------------ #
    def _loop(self):
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(self.poll_interval)

    def start(self):
        """建立資料夾、(視 process_existing)記下既有檔,啟動輪詢 thread。"""
        os.makedirs(self.folder, exist_ok=True)
        if not self.process_existing:
            for p in self._list_candidates():
                self._handled.add(p)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"FileWatcher({os.path.basename(self.folder)})",
            daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class TriggerManager:
    """管理多個 FileWatcher,並以全域 busy lock 確保同時只跑一條流程。

    runner 約定:把觸發到的檔路徑放進變數 {trigger_file}。本管理員不直接跑 flow,而是
    在 callback 中:取得 busy lock → 呼叫使用者提供的 runner(帶 trigger_file)→ 釋放。
    若取不到 lock(已有流程在跑)→ 不執行、回傳 False,讓該檔留待下一輪輪詢再觸發。

    參數:
      runner : (trigger_file:str, watcher_meta:dict) -> None;真正執行流程的回呼。
               TriggerManager 只負責「同時只跑一條 + 注入 trigger_file」。
    """

    def __init__(self, runner: Callable[[str, dict], None] | None = None):
        self._runner = runner
        self._watchers: list[FileWatcher] = []
        self._busy = threading.Lock()
        self._lock = threading.Lock()      # 保護 _watchers 清單
        # 觀測用:已執行的觸發數 / 被 busy 擋下的次數
        self.fired_count = 0
        self.skipped_busy = 0

    def is_busy(self) -> bool:
        # Lock 沒有公開「是否被持有」的可靠 API;用 acquire(non-block) 探測再釋放。
        if self._busy.acquire(blocking=False):
            self._busy.release()
            return False
        return True

    def _gate(self) -> bool:
        """gate:目前沒有流程在跑(busy lock 可取得)才開。"""
        return not self.is_busy()

    def _make_callback(self, meta: dict) -> Callable[[str], None]:
        def _cb(path: str):
            # 全域 busy lock:同時只跑一條。取不到 → 計數後直接返回(該檔下一輪再試)。
            if not self._busy.acquire(blocking=False):
                self.skipped_busy += 1
                return
            try:
                self.fired_count += 1
                if self._runner is not None:
                    self._runner(path, meta)
            finally:
                self._busy.release()
        return _cb

    def add_watcher(
        self,
        folder: str,
        flow_name: str | None = None,
        patterns: list[str] | None = None,
        poll_interval: float = 1.0,
        stable_sec: float = 1.0,
        process_existing: bool = False,
        callback: Callable[[str], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> FileWatcher:
        """新增一個 watcher。

        - 預設 callback = 取 busy lock → 呼叫 self._runner(path, meta);meta 含 flow_name。
        - 也可注入自訂 callback(測試 / 特殊用途);此時仍會套用 gate(busy lock 檢查),
          但實際的「同時只跑一條」保證落在預設 callback 的 acquire 上。為了讓自訂 callback
          也享有 busy lock,我們把它包進同一個 acquire 流程。
        """
        meta = {"flow_name": flow_name, "folder": os.path.abspath(folder)}

        if callback is None:
            cb = self._make_callback(meta)
        else:
            user_cb = callback

            def cb(path: str, _user_cb=user_cb):
                if not self._busy.acquire(blocking=False):
                    self.skipped_busy += 1
                    return
                try:
                    self.fired_count += 1
                    _user_cb(path)
                finally:
                    self._busy.release()

        w = FileWatcher(
            folder=folder,
            callback=cb,
            patterns=patterns,
            poll_interval=poll_interval,
            stable_sec=stable_sec,
            process_existing=process_existing,
            gate=self._gate,
            on_error=on_error,
        )
        with self._lock:
            self._watchers.append(w)
        return w

    def start_all(self):
        with self._lock:
            watchers = list(self._watchers)
        for w in watchers:
            w.start()

    def stop_all(self, timeout: float = 5.0):
        with self._lock:
            watchers = list(self._watchers)
        for w in watchers:
            w.stop(timeout=timeout)

    @property
    def watchers(self) -> list[FileWatcher]:
        with self._lock:
            return list(self._watchers)
