# -*- coding: utf-8 -*-
"""Headless wiring — 不依賴 ui.* / PySide6 的純執行路徑。

把「get_session → open → 建 ActionContext → run_flow → close → finish_run」這條
wiring 抽成模組級函式 `run_flow_headless()`,讓無人值守(unattended)機器
**不必安裝 PySide6** 也能跑流程。

與 ui.run_worker.run_flow_once 的差別:
- 本模組只 import core.* / engines.*,**完全不碰 PySide6 / Qt**。
- 不提供 MFA 人工暫停的 resume_event/on_pause/on_resume 控制(那是 attended 才有的)。
  unattended 模式靠 ctx.extra['unattended']=True 讓 flow.pause_for_human 不卡人。

attended(有人值守、UI)的路徑仍走 ui.run_worker.run_flow_once,行為照舊。
"""
from __future__ import annotations
import threading
import time
import traceback
from typing import Callable

from core.registry import ActionContext
from core.variables import VarStore
from core.runner import run_flow, RunResult

# DPI awareness:headless 執行路徑也會驅動 pyautogui / desktop 引擎,需在這些套件
# 真正抓螢幕前就把行程設為 per-monitor v2 aware。冪等且 graceful(失敗不崩)。
try:
    from core.dpi import setup_dpi_awareness as _setup_dpi_awareness
except Exception:  # noqa: BLE001
    _setup_dpi_awareness = None


def _resolve_flow_retry(flow, overrides: dict, options: dict) -> tuple[int, float]:
    """決定流程級重試設定(times, interval_sec)。

    優先序:execution options > 執行期 overrides(變數)> flow.variables > 預設 0。
    鍵名:flow_retry_times / flow_retry_interval(interval 單位為秒)。
    預設 (0, 0.0) → 完全不重試,行為與舊版一致。
    """
    def _pick(key, default):
        for src in (options, overrides, flow.variables or {}):
            if src and key in src and src[key] is not None:
                return src[key]
        return default

    def _to_int(x, default=0):
        try:
            return max(0, int(float(x)))
        except (TypeError, ValueError):
            return default

    def _to_float(x, default=0.0):
        try:
            return max(0.0, float(x))
        except (TypeError, ValueError):
            return default

    times = _to_int(_pick("flow_retry_times", 0))
    interval = _to_float(_pick("flow_retry_interval", 0))
    return times, interval


def _interruptible_sleep(seconds: float, stop_event: threading.Event) -> bool:
    """睡 seconds 秒,期間每 0.1 秒檢查一次 stop_event。
    回傳 True 表示「被 stop 中斷」,False 表示「正常睡完」。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return True
        time.sleep(min(0.1, max(0.0, deadline - time.time())))
    return stop_event is not None and stop_event.is_set()

# 引擎無關的 flow/data/comms 動作:集中註冊。缺席時不讓執行 crash。
try:
    from core.actions_bootstrap import register_builtin_actions
    register_builtin_actions()
except Exception:  # noqa: BLE001
    pass


def run_flow_headless(
    flow,
    store,
    vault,
    stop_event: threading.Event | None = None,
    overrides: dict | None = None,
    options: dict | None = None,
    on_progress: Callable | None = None,
    log: Callable[[str], None] | None = None,
    session_factory: Callable | None = None,
    extra: dict | None = None,
    unattended: bool = False,
    service_account: str | None = None,
) -> RunResult:
    """同步執行一條 flow 並回傳 RunResult(headless,無 Qt 相依)。

    參數:
      unattended       : True → 在 ctx.extra 設 unattended=True。flow.pause_for_human
                         偵測到後會「記一筆警告、立即繼續」,不等人(無人值守不能卡 MFA)。
      service_account  : 服務帳號 secret 名稱。給定時把對應 Vault secret 注入
                         ctx.extra['service_account'] = {'name':..., 'secret':...},
                         供登入步驟以 secret_ref / extra 取用(免 MFA 的服務帳號)。
      session_factory  : (engine, options) -> session;預設用 core.engine_api.get_session。
                         測試可注入假 factory(回傳有 open()/close() 的 dummy session),
                         完全不碰真實 web/desktop 引擎。

    引擎缺席 / 初始化失敗不會 crash:寫 log、finish_run('failed') 後回傳 failed。
    """
    log = log or (lambda *_a, **_k: None)
    overrides = overrides or {}
    options = options or {}
    stop_event = stop_event if stop_event is not None else threading.Event()

    # 在驅動任何引擎(可能 import pyautogui / 抓螢幕)之前先設 DPI awareness。
    if _setup_dpi_awareness is not None:
        try:
            _setup_dpi_awareness()
        except Exception:  # noqa: BLE001
            pass

    # 變數:flow 預設值 + 執行期覆寫(覆寫優先)
    var_init = dict(flow.variables or {})
    var_init.update(overrides)
    vars_ = VarStore(var_init)

    # ---- 組 extra:unattended 旗標 + 服務帳號 secret 注入 ---- #
    ctx_extra = dict(extra or {})
    if unattended:
        ctx_extra["unattended"] = True
    if service_account:
        secret = None
        if vault is not None:
            try:
                secret = vault.get_secret(service_account)
            except Exception as e:  # noqa: BLE001
                log(f"讀取服務帳號 secret 失敗(已忽略):{type(e).__name__}: {e}")
        if secret is None:
            log(f"警告:找不到服務帳號 secret '{service_account}';"
                f"登入步驟可能因缺憑證而失敗。")
        ctx_extra["service_account"] = {"name": service_account, "secret": secret}

    # ---- anchor_dir:image(CV)策略要找「錄製時存的 anchor 圖」才能比對。
    # 慣例:recordings/<flow_name>_anchors。未指定且該目錄存在時自動帶入,
    # 否則 image 層會因缺 anchor_dir 被跳過 → 退化成只有 UIA→coord(失去 CV 防護)。
    if "anchor_dir" not in ctx_extra:
        import os as _os
        _cand = _os.path.join("recordings", f"{flow.name}_anchors")
        if _os.path.isdir(_cand):
            ctx_extra["anchor_dir"] = _cand
            log(f"anchor 目錄:{_cand}(image/CV 比對可用)")
        else:
            log(f"提醒:找不到 anchor 目錄 {_cand},image(CV)層將被略過,"
                f"重播只靠 UIA→coord;座標會因視窗移位而不準。")

    if session_factory is None:
        from core.engine_api import get_session  # lazy:缺引擎時才在這裡爆
        session_factory = get_session

    # ---- 流程級重試設定(預設 0:不重試,行為與舊版完全一致)---- #
    retry_times, retry_interval = _resolve_flow_retry(flow, overrides, options)
    if retry_times > 0:
        log(f"流程級重試已啟用:整條 flow 失敗時最多重跑 {retry_times} 次,"
            f"間隔 {retry_interval} 秒(可被停止中斷)。")

    # ---- 全域 F9 停止熱鍵:整段執行(含重試)期間註冊,結束移除 ---- #
    # 預設啟用;可用 options['global_stop_hotkey']=False 關閉。缺 pynput 不崩。
    hotkey = None
    if options.get("global_stop_hotkey", True):
        try:
            from core.hotkey import GlobalHotkey
            hotkey = GlobalHotkey(on_trigger=stop_event.set, log=log)
            hotkey.register()
        except Exception as e:  # noqa: BLE001
            log(f"全域停止熱鍵初始化失敗(已忽略):{type(e).__name__}: {e}")
            hotkey = None

    result: RunResult | None = None
    # attempt 0 = 首次;1..retry_times = 重試。
    for attempt in range(retry_times + 1):
        if stop_event.is_set():
            result = RunResult(status="stopped", steps_total=len(flow.steps),
                               steps_ok=0, steps_failed=0, variables=vars_.all())
            break

        # 每次嘗試:全新 run_id(各自獨立的 run/step log)+ 重新開引擎會話。
        # 重要:每次重試都用乾淨的 VarStore(回到 flow 預設 + overrides),
        #       避免上一次失敗殘留的中間變數污染下一次重試。
        if attempt > 0:
            log(f"=== 流程級重試 {attempt}/{retry_times}(上次結果:"
                f"{result.status if result else 'n/a'})===")
            vars_ = VarStore(dict(var_init))
            # 重置子流程呼叫堆疊等執行期暫存(extra 中以 _ 開頭的執行期鍵)
            ctx_extra.pop("_flow_call_stack", None)

        run_id = store.start_run(flow.name)
        session = None
        try:
            session = session_factory(flow.engine, options)
            engine_obj = session.open()
            log(f"引擎 '{flow.engine}' 已啟動。")
        except Exception as e:  # noqa: BLE001
            msg = (
                f"無法啟動引擎 '{flow.engine}':{type(e).__name__}: {e}\n"
                f"(web/desktop 引擎可能尚未安裝;請確認 engines/ 模組與相依套件。)"
            )
            log(msg)
            log(traceback.format_exc())
            store.finish_run(run_id, "failed", vars_.all())
            result = RunResult(status="failed", steps_total=len(flow.steps),
                               steps_ok=0, steps_failed=0, variables=vars_.all())
            # 引擎開不起來通常重試也沒用,但仍尊重 retry 設定 → 進入重試判斷。
        else:
            ctx = ActionContext(
                engine=engine_obj,
                vars=vars_,
                vault=vault,
                store=store,
                run_id=run_id,
                stop_event=stop_event,
                log=log,
                extra=ctx_extra,
            )
            try:
                result = run_flow(flow, ctx, on_progress=on_progress)
            except Exception as e:  # noqa: BLE001
                log(f"執行期未預期錯誤:{type(e).__name__}: {e}")
                log(traceback.format_exc())
                result = RunResult(status="failed", steps_total=len(flow.steps),
                                   steps_ok=0, steps_failed=0, variables=vars_.all())
            finally:
                try:
                    session.close()
                    log("引擎已關閉。")
                except Exception as e:  # noqa: BLE001
                    log(f"關閉引擎時發生錯誤(已忽略):{type(e).__name__}: {e}")

        store.finish_run(run_id, result.status, result.variables)

        # ---- 重試判斷 ---- #
        if result.status == "completed":
            break
        if result.status == "stopped":
            break          # 被使用者停止 → 不重試
        if attempt >= retry_times:
            break           # 已用完重試次數 → 維持 failed
        # 仍有重試額度:等間隔(可被 stop 打斷)後重跑
        if retry_interval > 0:
            log(f"流程失敗,{retry_interval} 秒後重試…")
            if _interruptible_sleep(retry_interval, stop_event):
                log("重試等待期間被停止。")
                result = RunResult(status="stopped", steps_total=len(flow.steps),
                                   steps_ok=result.steps_ok,
                                   steps_failed=result.steps_failed,
                                   variables=result.variables)
                break

    # 執行結束(完成 / 失敗 / 停止)→ 移除全域停止熱鍵。
    if hotkey is not None:
        try:
            hotkey.unregister()
        except Exception:  # noqa: BLE001
            pass

    return result
