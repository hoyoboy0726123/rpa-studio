# -*- coding: utf-8 -*-
"""RPA Studio PySide6 UI 層。

不被 core 反向依賴;透過 core.engine_api.get_session() 的 lazy import 與引擎解耦,
因此 web/desktop 引擎缺席或 import 失敗時,UI 仍可正常啟動與瀏覽。
"""
