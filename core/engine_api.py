# -*- coding: utf-8 -*-
"""引擎會話契約 (engine session contract) — 讓 UI 與引擎解耦。
web 引擎(Playwright)與 desktop 引擎(pywinauto)各自實作 EngineSession,
UI/contracts 只透過 get_session(engine) 取得,lazy import 避免硬相依。
"""
from __future__ import annotations
from typing import Protocol, runtime_checkable


@runtime_checkable
class EngineSession(Protocol):
    """一次執行的引擎會話。
    open()  : 啟動引擎,回傳要放進 ActionContext.engine 的活物件
              (web = Playwright Page;desktop = 桌面控制器)。
    close() : 關閉/清理。
    註冊 actions:引擎模組被 import 時,應同時用 @action 註冊自己的 web.* / desktop.* 動作。
    """
    def open(self) -> object: ...
    def close(self) -> None: ...


def get_session(engine: str, options: dict | None = None) -> "EngineSession":
    """工廠:依 flow.engine 取得引擎會話(lazy import,缺引擎時給清楚錯誤)。"""
    options = options or {}
    if engine == "web":
        from engines.web.session import WebSession  # noqa: WPS433
        return WebSession(**options)
    if engine == "desktop":
        from engines.desktop.session import DesktopSession  # noqa: WPS433
        return DesktopSession(**options)
    raise ValueError(f"unknown engine: {engine!r}")
