# -*- coding: utf-8 -*-
"""web 引擎對外契約 (public contracts) + 專案2 adapter。

對外只暴露三個入口,內部負責全部 wiring:
    get_session(web) -> open() -> 建 ActionContext(VarStore/Vault/Store/stop_event/log)
    -> run_flow -> close()。

  run_download_flow(flow, variables, options) -> list[str]
      跑下載型 flow,回收所有 web.download 產生的檔案路徑。

  run_query_flow(flow, keys, key_var="serial", options) -> dict[key, dict|None]
      對每個 key 設 {key_var}=key 跑一次查詢 flow,把擷取到的欄位變數收成 dict;
      查無資料(flow 失敗或無欄位)回 None。

  RpaSerialSource(flow_path, options).query(serial) -> dict|None
      對應專案2(序號批次查詢)既有的 SerialSource 介面。
"""
from __future__ import annotations
import threading

from core.schema import Flow
from core.registry import ActionContext
from core.variables import VarStore
from core.vault import Vault
from core.store import Store
from core.runner import run_flow
from core.engine_api import get_session


def _as_flow(flow) -> Flow:
    """接受 Flow 物件 / dict / 檔案路徑,統一轉成 Flow。"""
    if isinstance(flow, Flow):
        return flow
    if isinstance(flow, dict):
        return Flow.from_dict(flow)
    if isinstance(flow, str):
        return Flow.load(flow)
    raise TypeError(f"unsupported flow type: {type(flow)!r}")


def _make_ctx(page, variables: dict | None, options: dict | None):
    """組一個可執行的 ActionContext(共用 wiring)。

    options 額外鍵:
      db_path        : Store 的 sqlite 路徑(預設記憶體外的暫存,避免污染正式 DB)
      vault_dir      : Vault 的基底目錄
      screenshot_dir : 截圖目錄
      verbose        : True 時把 runner log 印到 stdout
    """
    options = options or {}
    store = Store(db_path=options.get("db_path", "rpa_studio.db"))
    vault = Vault(base_dir=options.get("vault_dir", "."))
    vars_store = VarStore(variables or {})
    stop_event = options.get("stop_event") or threading.Event()

    verbose = options.get("verbose", False)
    log = (lambda *a, **k: print(*a)) if verbose else (lambda *a, **k: None)

    run_id = store.start_run(options.get("flow_name", "web_contract"))
    ctx = ActionContext(
        engine=page,
        vars=vars_store,
        vault=vault,
        store=store,
        run_id=run_id,
        screenshot_dir=options.get("screenshot_dir", "logs/screenshots"),
        stop_event=stop_event,
        log=log,
    )
    return ctx, store, run_id


def run_download_flow(flow, variables: dict | None = None,
                      options: dict | None = None) -> list[str]:
    """跑下載型 flow,回傳所有下載檔案路徑(依執行順序)。"""
    options = options or {}
    f = _as_flow(flow)
    session = get_session("web", options.get("session") or {})
    page = session.open()

    downloads: list[str] = []

    def on_progress(index, total, step, result):
        # web.download 成功時 result.value = 落地路徑
        if step.action == "web.download" and result.ok and result.value:
            downloads.append(result.value)

    # flow 預設變數 + 呼叫端覆寫(base_url / month 等需在替換時可見)
    merged = dict(f.variables or {})
    merged.update(variables or {})
    try:
        ctx, store, run_id = _make_ctx(page, merged, options)
        result = run_flow(f, ctx, on_progress=on_progress)
        store.finish_run(run_id, result.status, ctx.vars.all())
    finally:
        session.close()
    return downloads


def run_query_flow(flow, keys, key_var: str = "serial",
                   options: dict | None = None) -> dict:
    """對每個 key 跑一次查詢 flow,回傳 {key: 欄位 dict | None}。

    同一個瀏覽器會話內逐 key 執行(若 flow 有登入段,只在第一輪登入後續仍有效,
    但本契約為求隔離,每個 key 都重跑完整 flow;登入動作通常可重入/冪等)。
    擷取結果 = 該輪結束後,VarStore 內「非 key_var、非保留」的變數。
    """
    options = options or {}
    template = _as_flow(flow)
    base_vars = dict(template.variables or {})
    # 保留原始 dict 以便每輪重建乾淨 Flow(runner 會就地替換 step.params/target,
    # 同一組 Step 物件不可跨 key 重用,否則 {serial} 會被前一輪的值寫死)
    template_dict = template.to_dict()

    session = get_session("web", options.get("session") or {})
    page = session.open()

    results: dict = {}
    try:
        for key in keys:
            f = Flow.from_dict(template_dict)  # 每個 key 一份全新 Flow
            # flow 預設變數 + 本輪 key;flow.variables 的鍵不算擷取結果
            run_vars = dict(base_vars)
            run_vars[key_var] = key
            ctx, store, run_id = _make_ctx(page, run_vars, options)
            run_result = run_flow(f, ctx, on_progress=None)
            store.finish_run(run_id, run_result.status, ctx.vars.all())

            reserved = set(base_vars) | {key_var}
            fields = {k: v for k, v in ctx.vars.all().items() if k not in reserved}
            # 判定查無:flow 失敗,或沒有抓到任何欄位,或欄位值含「查無/Not Found」
            if run_result.status != "completed" or not fields:
                results[key] = None
            elif _looks_not_found(fields):
                results[key] = None
            else:
                results[key] = fields
    finally:
        session.close()
    return results


_NOT_FOUND_TOKENS = ("查無", "查無資料", "not found", "no data", "查無此")


def _looks_not_found(fields: dict) -> bool:
    """欄位值若明顯是「查無」字樣,視為查無資料。"""
    for v in fields.values():
        if isinstance(v, str) and any(t in v.lower() for t in
                                      (s.lower() for s in _NOT_FOUND_TOKENS)):
            return True
    return False


class RpaSerialSource:
    """專案2 序號查詢資料源 adapter。

    用法:
        src = RpaSerialSource("flows/web_query_demo.json")
        info = src.query("SN12345")   # -> dict | None

    每次 query() 跑一次完整 flow(含開瀏覽器/登入/查詢),確保隔離與冪等;
    若要批次高效查詢,改用 run_query_flow(共用一個會話)。
    """

    def __init__(self, flow_path: str, options: dict | None = None):
        self.flow_path = flow_path
        self.options = options or {}
        self.key_var = self.options.get("key_var", "serial")

    def query(self, serial: str):
        result = run_query_flow(self.flow_path, [serial],
                                key_var=self.key_var, options=self.options)
        return result.get(serial)
