# -*- coding: utf-8 -*-
"""WebSession — Playwright chromium 引擎會話 (EngineSession 實作)。

open()  : 啟動 Playwright chromium、建 BrowserContext(允許下載 + 指定下載目錄)、
          回傳 Page(放進 ActionContext.engine 給 web.* action 使用)。
close() : 依序收掉 page / context / browser / playwright。

頂部 `from . import actions` 觸發 @action 註冊(import session 即註冊 web.* 動作)。
"""
from __future__ import annotations
import os

# import 即註冊 web.* actions(務必保留,勿移除)
from . import actions  # noqa: F401


class WebSession:
    """Playwright chromium 會話。

    headless     : 是否無頭模式(預設 True,適合 CI/測試)。
    channel      : 瀏覽器 channel,可給 'msedge' / 'chrome' 使用系統瀏覽器;
                   None 則用 Playwright 內建 chromium。
    download_dir : 下載落地目錄(會自動建立)。
    其餘 kw 透傳(預留,如 slow_mo / args 等)。
    """

    def __init__(self, headless: bool = True, channel: str | None = None,
                 download_dir: str = "logs/downloads", **kw):
        self.headless = headless
        self.channel = channel
        self.download_dir = download_dir
        self._kw = kw
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def open(self):
        """啟動引擎,回傳 Playwright Page。"""
        from playwright.sync_api import sync_playwright

        os.makedirs(self.download_dir, exist_ok=True)

        self._pw = sync_playwright().start()
        launch_kw: dict = {"headless": self.headless}
        if self.channel:
            launch_kw["channel"] = self.channel
        # slow_mo / args 等選項透傳
        for k in ("slow_mo", "args"):
            if k in self._kw:
                launch_kw[k] = self._kw[k]

        self._browser = self._pw.chromium.launch(**launch_kw)
        self._context = self._browser.new_context(accept_downloads=True)
        self._page = self._context.new_page()
        # 把下載目錄掛在 page 上,讓 web.download action 取得落地路徑
        self._page._rpa_download_dir = self.download_dir  # type: ignore[attr-defined]
        return self._page

    def close(self) -> None:
        """關閉/清理(逐層 best-effort,任何一層失敗不阻斷後續)。"""
        for closer in (
            lambda: self._page and self._page.close(),
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:
                pass
        self._page = self._context = self._browser = self._pw = None
