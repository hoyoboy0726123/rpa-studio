# -*- coding: utf-8 -*-
"""Desktop 引擎會話 (pywinauto)。

DesktopSession 實作 core.engine_api.EngineSession:
  open()  -> DesktopController(包 Application + Desktop(backend="uia"))
  close() -> 若 app 是本 session 啟動的,嘗試關閉

ActionContext.engine 會被設為 open() 回傳的 DesktopController,
desktop.* 動作即透過 ctx.engine 操作視窗。
"""
from __future__ import annotations

# import actions 觸發 @action 註冊(即使 GUI 環境不可用,註冊仍應成功)。
from . import actions  # noqa: F401  (registration side-effect)


class DesktopController:
    """桌面控制器:封裝 pywinauto 的 Application 與 Desktop。

    屬性:
      app      : pywinauto.Application(backend=uia),start/connect 的目標
      desktop  : pywinauto.Desktop(backend="uia"),用於跨 process 找視窗
      backend  : "uia" | "win32"
      win32_desktop : 同名的 win32 backend Desktop,供 fallback 定位

    供 runner._screenshot 用:沒有 .screenshot 屬性,runner 會自動退到 pyautogui 全螢幕。
    """

    def __init__(self, app=None, desktop=None, backend: str = "uia",
                 win32_desktop=None):
        self.app = app
        self.desktop = desktop
        self.backend = backend
        self.win32_desktop = win32_desktop

    def top_window(self):
        """取目前 app 的最上層視窗(若有 app)。"""
        if self.app is not None:
            return self.app.top_window()
        return None


class DesktopSession:
    """pywinauto 桌面引擎會話。

    參數:
      app_path     : 要啟動的執行檔(例 "notepad.exe");給了就 start
      attach_title : 要附掛的既有視窗標題(regex);給了就 connect
      backend      : "uia"(預設,推薦)| "win32"
      timeout      : connect / 等視窗出現的逾時秒數
      其餘 **kw 透傳給 Application.start/connect(例 work_dir)
    """

    def __init__(self, app_path: str | None = None,
                 attach_title: str | None = None,
                 backend: str = "uia", timeout: float = 20.0, **kw):
        self.app_path = app_path
        self.attach_title = attach_title
        self.backend = backend or "uia"
        self.timeout = timeout
        self.kw = kw
        self._controller: DesktopController | None = None
        self._owns_app = False   # 是否由本 session 啟動(決定 close 要不要關)

    def open(self) -> DesktopController:
        """啟動或附掛目標,回傳 DesktopController。"""
        import pywinauto
        from pywinauto import Application, Desktop

        app = None
        if self.app_path:
            app = Application(backend=self.backend).start(self.app_path, **self.kw)
            self._owns_app = True
            # 等主視窗就緒(忽略找不到,動作層再做 wait_for)
            try:
                app.top_window().wait("ready", timeout=self.timeout)
            except Exception:
                pass
        elif self.attach_title:
            try:
                app = Application(backend=self.backend).connect(
                    title_re=self.attach_title, timeout=self.timeout, **self.kw)
            except Exception:
                # 退一步用 best_match
                app = Application(backend=self.backend).connect(
                    best_match=self.attach_title, timeout=self.timeout, **self.kw)
            self._owns_app = False

        desktop = Desktop(backend=self.backend)
        # 另備一個 win32 桌面供 fallback 定位
        try:
            win32_desktop = Desktop(backend="win32") if self.backend != "win32" else desktop
        except Exception:
            win32_desktop = None

        self._controller = DesktopController(
            app=app, desktop=desktop, backend=self.backend,
            win32_desktop=win32_desktop)
        return self._controller

    def close(self) -> None:
        """清理:只關閉本 session 啟動的 app(附掛既有 app 時不關)。"""
        ctrl = self._controller
        if ctrl is None:
            return
        if self._owns_app and ctrl.app is not None:
            try:
                ctrl.app.kill(soft=True)
            except Exception:
                try:
                    ctrl.app.kill()
                except Exception:
                    pass
        self._controller = None
