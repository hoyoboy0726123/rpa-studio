# -*- coding: utf-8 -*-
"""變數倉庫 VarStore:{var} 字串替換 + 內建時間/財務 placeholder。
使用者顯式設值優先於內建 placeholder。
"""
from __future__ import annotations
import re
import datetime as dt

_PAT = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_:%\-./ ]*)\}")


def _builtin(name: str, now: dt.datetime) -> str | None:
    if name == "now" or name.startswith("now:"):
        fmt = name.split(":", 1)[1] if ":" in name else "%Y-%m-%d %H:%M:%S"
        return now.strftime(fmt)
    if name == "today":
        return now.strftime("%Y%m%d")
    if name == "yesterday":
        return (now - dt.timedelta(days=1)).strftime("%Y%m%d")
    if name == "data_yyyymm":            # 預設資料月 = 當月-1(對應關帳/報表慣例)
        first = now.replace(day=1)
        prev = first - dt.timedelta(days=1)
        return prev.strftime("%Y%m")
    if name == "prev_month_yyyymm":
        first = now.replace(day=1)
        return (first - dt.timedelta(days=1)).strftime("%Y%m")
    if name == "this_month_yyyymm":
        return now.strftime("%Y%m")
    return None


class VarStore:
    def __init__(self, initial: dict | None = None):
        self._v: dict[str, object] = dict(initial or {})

    def set(self, name: str, value) -> None:
        self._v[name] = value

    def get(self, name: str, default=None):
        return self._v.get(name, default)

    def all(self) -> dict:
        return dict(self._v)

    def substitute(self, text):
        """把字串中的 {var} 換掉。非字串原樣回傳。"""
        if not isinstance(text, str):
            return text
        now = dt.datetime.now()

        def repl(m):
            name = m.group(1)
            if name in self._v:                 # 使用者值優先
                return str(self._v[name])
            b = _builtin(name, now)
            return b if b is not None else m.group(0)

        return _PAT.sub(repl, text)

    def substitute_params(self, params: dict) -> dict:
        """遞迴替換 dict/list 內所有字串。"""
        def walk(o):
            if isinstance(o, str):
                return self.substitute(o)
            if isinstance(o, dict):
                return {k: walk(v) for k, v in o.items()}
            if isinstance(o, list):
                return [walk(x) for x in o]
            return o
        return walk(params or {})
