# -*- coding: utf-8 -*-
"""觸發器子系統 (triggers)。

目前提供:
  - FileWatcher    : 輪詢監看資料夾,偵測「新檔且大小穩定」後觸發 callback。
  - TriggerManager : 管理多個 watcher,並以全域 busy lock 確保同時只跑一條流程。
"""
from .file_watcher import FileWatcher, TriggerManager  # noqa: F401
