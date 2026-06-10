# -*- coding: utf-8 -*-
"""Action registry — Activepieces piece 模型的單機簡化版。
每個動作是一個 Python function,用 @action("web.click") 註冊;
runner 靠 step.action 字串查表分派。新增能力 = 新增一個註冊 function,零侵入。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable

# name -> function(ctx, step) -> ActionResult
ACTIONS: dict[str, Callable] = {}


def action(name: str):
    def deco(fn: Callable):
        ACTIONS[name] = fn
        fn._action_name = name  # type: ignore
        return fn
    return deco


def get_action(name: str) -> Callable | None:
    return ACTIONS.get(name)


@dataclass
class ActionResult:
    ok: bool = True
    value: object = None
    error: str = ""


@dataclass
class ActionContext:
    """執行期上下文,傳給每個 action。
    engine : 引擎活物件(web=Playwright Page;desktop=pywinauto app/desktop 控制器)
    vars   : VarStore(變數倉庫,支援 {var} 替換)
    vault  : Vault(取 secret)
    store  : Store(寫 run/step log)
    stop_event : threading.Event;action 在可中斷點檢查它
    """
    engine: object = None
    vars: object = None
    vault: object = None
    store: object = None
    run_id: str = ""
    screenshot_dir: str = "logs/screenshots"
    stop_event: object = None
    log: Callable = lambda *a, **k: None
    extra: dict = field(default_factory=dict)

    def should_stop(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()
