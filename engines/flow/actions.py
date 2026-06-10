# -*- coding: utf-8 -*-
"""flow.* actions — 引擎無關的控制流輔助 / 互動 / IO 動作集。

每個動作簽名 `def fn(ctx, step) -> ActionResult`,不需要 ctx.engine。
與 UI 的互動(彈窗、暫停 overlay)透過 ctx.extra 的回呼掛勾,
headless(CLI / 工作排程器)無回呼時走安全的預設行為,確保無人值守也能跑完。

ctx.extra 約定的鍵(全部選用,缺了就退化成 headless 行為):
  prompt_cb(message, default, is_secret) -> str | None
      flow.prompt_user 用:回傳使用者輸入;回 None 視為取消 → 用 default。
  on_pause(message)   -> None    flow.pause_for_human 進入暫停時呼叫(UI 顯示 PAUSED)。
  on_resume()         -> None    flow.pause_for_human 結束時呼叫(UI 收掉 PAUSED)。
  resume_event        threading.Event   被 set 代表「人工已完成、繼續」。
"""
from __future__ import annotations
import os
import time

from core.registry import action, ActionResult


# --------------------------------------------------------------------------- #
# 變數
# --------------------------------------------------------------------------- #
@action("flow.set_var")
def flow_set_var(ctx, step) -> ActionResult:
    """設一個變數:params {name, value}。value 已在 runner 做過 {var} 替換。"""
    name = step.params.get("name")
    if not name:
        return ActionResult(ok=False, error="flow.set_var 缺少 params.name")
    value = step.params.get("value")
    ctx.vars.set(name, value)
    return ActionResult(ok=True, value=value)


# --------------------------------------------------------------------------- #
# 子流程重用:flow.call(把另一條 flow 當積木 inline 執行)
# --------------------------------------------------------------------------- #
@action("flow.call")
def flow_call(ctx, step) -> ActionResult:
    """呼叫(inline 執行)另一條 flow,讓「流程當積木重用」。

    params:
      flow_name   : 要呼叫的子流程名稱(必填;從 ctx.store 載入)。
      vars        : dict,選用。把當前變數對應 / 注入子流程,如
                    {"sub_in": "{outer_var}"}(value 已由 runner 做過 {var} 替換)。
                    這些值在進入子流程前 set 進共用的 VarStore。
      export      : list[str],選用。子流程跑完後,只有這些變數名會「明確保留」
                    (目前共用同一個 ctx.store/VarStore,變數天然共享;export 僅作文件意圖,
                    保留參數以利日後切換成子 ctx 隔離模式)。

    執行模型:
      - **共用同一個 ctx**(同一 VarStore / engine / store / stop_event),
        子流程的 steps 透過 core.runner.run_flow inline 跑;因此子流程的變數
        改動會反映回主流程(刻意:讓子流程能回傳結果)。
      - **遞迴呼叫堆疊 + 循環偵測**:ctx.extra['_flow_call_stack'] 記錄目前的呼叫鏈。
        若 flow_name 已在鏈上(A→B→A)→ 立即報錯,不進入無限遞迴。
      - 子流程失敗(status != completed)→ 本 action 回 ok=False,交給 runner 的
        on_error 處理(預設 abort 會讓主流程也停)。
    """
    flow_name = step.params.get("flow_name") or step.params.get("flow")
    if not flow_name:
        return ActionResult(ok=False, error="flow.call 缺少 params.flow_name")

    store = ctx.store
    if store is None or not hasattr(store, "load_flow"):
        return ActionResult(ok=False, error="flow.call 需要 ctx.store 才能載入子流程")

    # ---- 循環偵測:用 ctx.extra 維護呼叫堆疊 ---- #
    extra = ctx.extra if ctx.extra is not None else {}
    stack = extra.get("_flow_call_stack")
    if stack is None:
        stack = []
        extra["_flow_call_stack"] = stack
    if flow_name in stack:
        chain = " -> ".join(stack + [flow_name])
        return ActionResult(
            ok=False,
            error=f"flow.call 偵測到循環呼叫(已擋下,不遞迴):{chain}")

    # 防呆:限制最大深度(即使無循環,也不該無限深)
    if len(stack) >= 32:
        return ActionResult(
            ok=False,
            error=f"flow.call 呼叫深度超過上限(32):{' -> '.join(stack)}")

    d = None
    try:
        d = store.load_flow(flow_name)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"flow.call 載入子流程失敗: {type(e).__name__}: {e}")
    if not d:
        return ActionResult(ok=False, error=f"flow.call 找不到子流程:{flow_name}")

    # lazy import 避免 core.runner <-> engines.flow.actions 的 import 循環
    from core.schema import Flow
    from core.runner import run_flow

    sub_flow = Flow.from_dict(d)

    # ---- 變數注入:把對應的值 set 進共用 VarStore ---- #
    var_map = step.params.get("vars") or step.params.get("var") or {}
    if isinstance(var_map, dict) and ctx.vars is not None:
        for k, v in var_map.items():
            ctx.vars.set(k, v)
    # 子流程的預設變數:只在主流程尚未設過時補上(不覆蓋主流程現值)
    if ctx.vars is not None and sub_flow.variables:
        for k, v in sub_flow.variables.items():
            if ctx.vars.get(k) is None:
                ctx.vars.set(k, v)

    ctx.log(f"[flow.call] → 進入子流程 '{flow_name}'({len(sub_flow.steps)} steps);"
            f"呼叫鏈:{' -> '.join(stack + [flow_name])}")

    stack.append(flow_name)
    try:
        sub_result = run_flow(sub_flow, ctx)
    finally:
        stack.pop()

    ctx.log(f"[flow.call] ← 子流程 '{flow_name}' 結束:{sub_result.status} "
            f"(ok={sub_result.steps_ok} failed={sub_result.steps_failed})")

    if sub_result.status == "stopped":
        return ActionResult(ok=False, error="stopped")
    if sub_result.status != "completed":
        return ActionResult(
            ok=False,
            error=f"子流程 '{flow_name}' 未成功完成:{sub_result.status} "
                  f"(failed={sub_result.steps_failed})")
    return ActionResult(ok=True, value=sub_result.status)


# --------------------------------------------------------------------------- #
# 互動:向使用者要值
# --------------------------------------------------------------------------- #
@action("flow.prompt_user")
def flow_prompt_user(ctx, step) -> ActionResult:
    """向使用者要一個值,存進變數。

    params {var, message, default, is_secret}
      - 有 ctx.extra['prompt_cb'] → 呼叫它(UI 彈窗);回 None 視為取消 → 用 default。
      - 無 prompt_cb(headless)→ 直接用 default。
      - is_secret 為真:同時把值存進 vault(secret 名稱 = var),變數只放回填用的值。
    """
    var = step.params.get("var")
    if not var:
        return ActionResult(ok=False, error="flow.prompt_user 缺少 params.var")
    message = step.params.get("message", f"請輸入 {var}")
    default = step.params.get("default", "")
    is_secret = bool(step.params.get("is_secret", False))

    prompt_cb = (ctx.extra or {}).get("prompt_cb")
    value = None
    if callable(prompt_cb):
        try:
            value = prompt_cb(message, default, is_secret)
        except Exception as e:  # noqa: BLE001 — UI 回呼失敗不該炸掉整條 flow
            ctx.log(f"flow.prompt_user: prompt_cb 失敗,改用 default。{type(e).__name__}: {e}")
            value = None
    if value is None:
        value = default

    if is_secret and ctx.vault is not None:
        try:
            ctx.vault.set_secret(var, str(value))
        except Exception as e:  # noqa: BLE001
            ctx.log(f"flow.prompt_user: 存 vault 失敗(已忽略)。{type(e).__name__}: {e}")

    ctx.vars.set(var, value)
    # 機密值不回填進 ActionResult.value(避免落入 step log)
    return ActionResult(ok=True, value=None if is_secret else value)


# --------------------------------------------------------------------------- #
# MFA 人工暫停核心
# --------------------------------------------------------------------------- #
@action("flow.pause_for_human")
def flow_pause_for_human(ctx, step) -> ActionResult:
    """暫停讓人工介入(MFA / OTP / 手動驗證),完成後再繼續。

    params {message, timeout_sec}

    有 ctx.extra['resume_event'](threading.Event,通常由 UI 提供):
      1. 呼叫 on_pause(message) 讓 UI/overlay 顯示 PAUSED。
      2. 輪詢等待,直到下列任一:
           - resume_event 被 set(人工按「繼續」)         → ok
           - ctx.should_stop()(使用者按停止)             → stopped
           - 逾時(timeout_sec > 0 且超過)                → 逾時失敗
      3. 不論如何結束都呼叫 on_resume() 收掉 PAUSED。

    headless(無 resume_event):
      - timeout_sec <= 0 → 立即繼續(無人值守不卡住)。
      - timeout_sec > 0  → 等該秒數後繼續(給下載 / 外部流程一點時間),
                           等待期間仍可被 should_stop() 中斷。

    unattended(ctx.extra['unattended'] 為真):
      - 無論有無 resume_event / timeout_sec,**一律不等人**:記一筆警告 log 後立即繼續。
        (無人值守機器沒有人能完成 MFA;卡在這裡只會讓排程任務逾時掛死。前提是該系統
         已改用服務帳號 / 免 MFA — 見 README。)
    """
    message = step.params.get("message", "已暫停,請完成人工步驟後繼續。")
    try:
        timeout_sec = float(step.params.get("timeout_sec", 0) or 0)
    except (TypeError, ValueError):
        timeout_sec = 0.0

    extra = ctx.extra or {}

    # ---- unattended:不等人,記警告後立即繼續 ---- #
    if extra.get("unattended"):
        ctx.log(f"[UNATTENDED] 跳過人工暫停(不等人):{message}")
        return ActionResult(ok=True, value="resumed:unattended")

    resume_event = extra.get("resume_event")
    on_pause = extra.get("on_pause")
    on_resume = extra.get("on_resume")

    # ---- headless:無 resume_event ---- #
    if resume_event is None:
        if timeout_sec <= 0:
            return ActionResult(ok=True, value="resumed:immediate")
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if ctx.should_stop():
                return ActionResult(ok=False, error="stopped")
            time.sleep(min(0.1, max(0.0, deadline - time.time())))
        return ActionResult(ok=True, value="resumed:timeout")

    # ---- 有 resume_event:互動式暫停 ---- #
    resume_event.clear()
    if callable(on_pause):
        try:
            on_pause(message)
        except Exception as e:  # noqa: BLE001
            ctx.log(f"flow.pause_for_human: on_pause 失敗(已忽略)。{type(e).__name__}: {e}")
    ctx.log(f"[PAUSED] {message}")

    deadline = (time.time() + timeout_sec) if timeout_sec > 0 else None
    result = ActionResult(ok=True, value="resumed")
    try:
        while True:
            if resume_event.wait(0.1):
                result = ActionResult(ok=True, value="resumed")
                break
            if ctx.should_stop():
                result = ActionResult(ok=False, error="stopped")
                break
            if deadline is not None and time.time() >= deadline:
                result = ActionResult(ok=False, error="pause timeout")
                break
    finally:
        if callable(on_resume):
            try:
                on_resume()
            except Exception as e:  # noqa: BLE001
                ctx.log(f"flow.pause_for_human: on_resume 失敗(已忽略)。{type(e).__name__}: {e}")
        ctx.log("[RESUMED]")
    return result


# --------------------------------------------------------------------------- #
# 等檔案(下載完成偵測)
# --------------------------------------------------------------------------- #
@action("flow.wait_file")
def flow_wait_file(ctx, step) -> ActionResult:
    """等檔案出現且大小穩定(下載完成偵測)。

    params {path, timeout_sec, stable_sec}
      - path        : 目標檔案路徑(支援 {var} 替換,已由 runner 處理)。
      - timeout_sec : 總等待上限(預設 30;<=0 視為不限,直到 should_stop)。
      - stable_sec  : 大小需連續穩定的秒數(預設 1.0)才算下載完成。
    成功把絕對路徑寫進 params.var(若有)並回傳。
    """
    path = step.params.get("path")
    if not path:
        return ActionResult(ok=False, error="flow.wait_file 缺少 params.path")
    try:
        timeout_sec = float(step.params.get("timeout_sec", 30) or 0)
    except (TypeError, ValueError):
        timeout_sec = 30.0
    try:
        stable_sec = float(step.params.get("stable_sec", 1.0) or 0)
    except (TypeError, ValueError):
        stable_sec = 1.0

    deadline = (time.time() + timeout_sec) if timeout_sec > 0 else None
    last_size = -1
    stable_since = None

    while True:
        if ctx.should_stop():
            return ActionResult(ok=False, error="stopped")
        if deadline is not None and time.time() > deadline:
            return ActionResult(ok=False, error=f"flow.wait_file 逾時: {path}")

        try:
            size = os.path.getsize(path) if os.path.exists(path) else -1
        except OSError:
            size = -1

        if size >= 0:
            if size == last_size:
                if stable_since is None:
                    stable_since = time.time()
                elif (time.time() - stable_since) >= stable_sec:
                    abspath = os.path.abspath(path)
                    var = step.params.get("var")
                    if var:
                        ctx.vars.set(var, abspath)
                    return ActionResult(ok=True, value=abspath)
            else:
                stable_since = None
                last_size = size
        time.sleep(0.1)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@action("flow.http")
def flow_http(ctx, step) -> ActionResult:
    """發 HTTP 請求,回應存進變數。

    params {method, url, headers, body, timeout, var}
      - method  : GET/POST/...(預設 GET)
      - url     : 目標 URL(必填)
      - headers : dict(選用)
      - body    : str 或 dict;dict 自動以 JSON 送出
      - timeout : 秒(預設 30)
      - var     : 變數名;回應文字存進 {var},狀態碼存進 {var}_status
    """
    import requests

    url = step.params.get("url")
    if not url:
        return ActionResult(ok=False, error="flow.http 缺少 params.url")
    method = str(step.params.get("method", "GET")).upper()
    headers = step.params.get("headers") or {}
    body = step.params.get("body")
    try:
        timeout = float(step.params.get("timeout", 30) or 30)
    except (TypeError, ValueError):
        timeout = 30.0

    kwargs = {"headers": headers, "timeout": timeout}
    if body is not None:
        if isinstance(body, (dict, list)):
            kwargs["json"] = body
        else:
            kwargs["data"] = body

    try:
        resp = requests.request(method, url, **kwargs)
    except Exception as e:  # noqa: BLE001 — 網路錯誤回成 action 失敗,交給 runner on_error
        return ActionResult(ok=False, error=f"flow.http 失敗: {type(e).__name__}: {e}")

    var = step.params.get("var")
    if var:
        ctx.vars.set(var, resp.text)
        ctx.vars.set(f"{var}_status", resp.status_code)
    ok = 200 <= resp.status_code < 400
    return ActionResult(ok=ok, value=resp.status_code,
                        error="" if ok else f"HTTP {resp.status_code}")
