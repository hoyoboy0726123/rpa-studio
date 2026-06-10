# -*- coding: utf-8 -*-
"""集中註冊所有「引擎無關」的內建動作(flow / data / comms)。
在執行路徑(headless / run_worker / run_cli)呼叫一次即可確保動作都註冊到 registry。
每個模組 import 都包 try/except —— 缺選用相依(如 msal/pywin32)也不影響其他動作註冊。
(web.* / desktop.* 由各自的 engine session 在啟動時 import 註冊,不在此處。)
"""
from __future__ import annotations
import logging

_log = logging.getLogger(__name__)
_DONE = False

_MODULES = (
    "engines.flow.actions",
    "engines.data.actions",
    "engines.comms.actions",
)


def register_builtin_actions(force: bool = False) -> None:
    global _DONE
    if _DONE and not force:
        return
    for mod in _MODULES:
        try:
            __import__(mod)
        except Exception as e:  # noqa: BLE001
            _log.warning("註冊內建動作失敗 %s: %s", mod, e)
    _DONE = True
