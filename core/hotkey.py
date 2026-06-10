# -*- coding: utf-8 -*-
"""全域熱鍵 (global hotkey) — 播放中按 F9 立即停止。

用途
----
流程播放時,自動化可能正在搶滑鼠 / 鍵盤(desktop 引擎)或前景視窗在別的 app,
使用者沒辦法回到 RPA Studio 視窗按「停止」鈕。一個**全域**熱鍵(不論前景視窗為何
都能攔到)是 RPA 工具的標準安全閥(「Esc/F9 緊急煞車」)。

實作
----
用 pynput 的 GlobalHotKeys 在背景 thread 監聽 F9。執行開始時 register() →
按下 F9 時呼叫 on_trigger(通常 = stop_event.set);執行結束時 unregister()。

缺 pynput 不崩
-------------
pynput 為選用相依(無頭 / CI 環境可能沒有或沒有顯示伺服器)。import 失敗或
GlobalHotKeys 啟動失敗時,GlobalHotkey 仍可建立與呼叫,只是 available=False、
register() 無實際效果——不丟例外、不讓執行流程崩。

可程式化觸發
-----------
提供 fire():不依賴實體鍵盤,直接呼叫 on_trigger。供測試模擬「按下 F9」,
也供其他程式路徑(例如 UI 的停止鈕)走同一條停止邏輯。
"""
from __future__ import annotations

import threading
from typing import Callable

try:
    from pynput import keyboard as _kb
    _PYNPUT_AVAILABLE = True
except Exception:  # noqa: BLE001
    _kb = None  # type: ignore
    _PYNPUT_AVAILABLE = False


class GlobalHotkey:
    """監聽單一全域熱鍵(預設 F9),觸發時呼叫 on_trigger。

    參數:
      on_trigger : () -> None;熱鍵按下(或 fire())時呼叫。
      hotkey     : pynput GlobalHotKeys 格式的鍵字串,預設 '<f9>'。
      log        : (str) -> None。
    """

    def __init__(self, on_trigger: Callable[[], None],
                 hotkey: str = "<f9>",
                 log: Callable[[str], None] | None = None):
        self._on_trigger = on_trigger
        self.hotkey = hotkey
        self.log = log or (lambda *_a, **_k: None)
        self._listener = None
        self._lock = threading.Lock()
        self._fired = False

    @property
    def available(self) -> bool:
        return _PYNPUT_AVAILABLE

    @property
    def registered(self) -> bool:
        return self._listener is not None

    # ------------------------------------------------------------------ #
    def _handle(self):
        """內部:熱鍵被觸發。去重(同一次執行只觸發一次)後呼叫 on_trigger。"""
        with self._lock:
            if self._fired:
                return
            self._fired = True
        self.log(f"[hotkey] {self.hotkey} 被按下 → 觸發停止。")
        try:
            self._on_trigger()
        except Exception as e:  # noqa: BLE001
            self.log(f"[hotkey] on_trigger 失敗(已忽略):{type(e).__name__}: {e}")

    def fire(self):
        """程式化觸發(等同按下熱鍵)。供測試 / 其他停止路徑共用。"""
        self._handle()

    def register(self) -> bool:
        """開始監聽全域熱鍵。回傳是否成功掛上實體監聽。

        缺 pynput → 記 log 回 False(不崩);此時仍可用 fire() 程式化觸發。
        """
        with self._lock:
            self._fired = False
        if not _PYNPUT_AVAILABLE:
            self.log("[hotkey] pynput 未安裝,全域熱鍵停用(仍可用程式化 fire())。")
            return False
        if self._listener is not None:
            return True
        try:
            self._listener = _kb.GlobalHotKeys({self.hotkey: self._handle})
            self._listener.start()
            self.log(f"[hotkey] 已註冊全域停止熱鍵:{self.hotkey}")
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"[hotkey] 註冊失敗(已忽略,仍可程式化 fire()):"
                     f"{type(e).__name__}: {e}")
            self._listener = None
            return False

    def unregister(self):
        """停止監聽(執行結束時呼叫)。"""
        lst = self._listener
        self._listener = None
        if lst is not None:
            try:
                lst.stop()
            except Exception:  # noqa: BLE001
                pass

    # context manager 糖:with GlobalHotkey(...) as hk: ...
    def __enter__(self):
        self.register()
        return self

    def __exit__(self, *exc):
        self.unregister()
        return False
