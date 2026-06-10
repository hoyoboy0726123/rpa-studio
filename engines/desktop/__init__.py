# -*- coding: utf-8 -*-
"""Desktop 引擎(pywinauto UIA)。

實作 core.engine_api.EngineSession 契約:
  - DesktopSession.open()  -> 回傳「桌面控制器」(DesktopController),放進 ActionContext.engine
  - DesktopSession.close() -> 清理(關閉自己啟動的 app)

import 本套件時,session 模組會 `from . import actions` 觸發 desktop.* 動作註冊。
"""
from .session import DesktopSession, DesktopController  # noqa: F401
