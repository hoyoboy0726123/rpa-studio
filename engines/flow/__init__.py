# -*- coding: utf-8 -*-
"""flow engine — 引擎無關的控制流 / 互動 / IO 動作集。

flow.* 動作不依賴任何瀏覽器或桌面引擎(ctx.engine 可為 None),
適用於任何 flow,也是 MFA 人工暫停(flow.pause_for_human)的所在。

import 本 package 即觸發 flow.* action 註冊(side-effect)。
"""
from . import actions  # noqa: F401  (registration side-effect)
