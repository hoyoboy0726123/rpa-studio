# -*- coding: utf-8 -*-
"""執行引擎 (runner) — 引擎無關。
逐 step 經 action registry 分派,負責:變數替換、step 級 retry/timeout、
失敗截圖、寫 step log、on_error 處理(abort/continue/goto)、可中止。
定位 fallback 與實際操作在各 action 內(web/desktop),runner 不需要知道。
"""
from __future__ import annotations
import time
import os
import traceback
from dataclasses import dataclass

from .registry import get_action, ActionResult, ActionContext


@dataclass
class RunResult:
    status: str           # completed | stopped | failed
    steps_total: int
    steps_ok: int
    steps_failed: int
    variables: dict


def _screenshot(ctx: ActionContext, step) -> str:
    """失敗時截圖。web 引擎用 page.screenshot;desktop 用全螢幕。回傳檔案路徑或空字串。"""
    try:
        os.makedirs(ctx.screenshot_dir, exist_ok=True)
        path = os.path.join(ctx.screenshot_dir, f"run{ctx.run_id}_{step.id}.png")
        eng = ctx.engine
        # web: Playwright Page 有 screenshot();desktop: 用 pyautogui 全螢幕
        if hasattr(eng, "screenshot"):
            eng.screenshot(path=path)
        else:
            try:
                import pyautogui
                pyautogui.screenshot(path)
            except Exception:
                return ""
        return path
    except Exception:
        return ""


def _exec_step(step, ctx: ActionContext):
    """執行單一 step,含 retry。回傳 (ActionResult, retries_used, ms, screenshot)."""
    fn = get_action(step.action)
    if fn is None:
        return ActionResult(ok=False, error=f"unknown action: {step.action}"), 0, 0, ""

    times = int((step.retry or {}).get("times", 0))
    interval = int((step.retry or {}).get("interval_ms", 1000)) / 1000.0
    last = ActionResult(ok=False, error="not run")
    start = time.time()
    used = 0
    for attempt in range(times + 1):
        if ctx.should_stop():
            return ActionResult(ok=False, error="stopped"), used, int((time.time() - start) * 1000), ""
        used = attempt
        try:
            last = fn(ctx, step)
            if last is None:
                last = ActionResult(ok=True)
            if last.ok:
                break
        except Exception as e:
            last = ActionResult(ok=False, error=f"{type(e).__name__}: {e}")
            ctx.log(f"[step {step.id}] exception:\n{traceback.format_exc()}")
        if attempt < times:
            time.sleep(interval)
    ms = int((time.time() - start) * 1000)
    shot = ""
    if not last.ok:
        shot = _screenshot(ctx, step)
    return last, used, ms, shot


def _coerce_num(x):
    """盡量把值轉成 float 供 gt/lt 比較;失敗回 None。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _eval_condition(var_val, op: str, value) -> bool:
    """flow.if 條件判斷。op: eq|ne|contains|empty|not_empty|gt|lt。"""
    op = (op or "eq").lower()
    if op == "empty":
        return var_val is None or str(var_val) == ""
    if op == "not_empty":
        return var_val is not None and str(var_val) != ""
    if op == "eq":
        return str(var_val) == str(value)
    if op == "ne":
        return str(var_val) != str(value)
    if op == "contains":
        try:
            return str(value) in str(var_val)
        except Exception:
            return False
    if op in ("gt", "lt"):
        a, b = _coerce_num(var_val), _coerce_num(value)
        if a is None or b is None:
            return False
        return a > b if op == "gt" else a < b
    return False


def run_flow(flow, ctx: ActionContext, on_progress=None) -> RunResult:
    """執行整條 flow。on_progress(index, total, step, result) 供 UI 即時更新。

    控制流(在主迴圈直接處理,不走 registry):
      flow.if   : params {var, op, value, skip_count}
                  條件不成立 → 跳過接下來 skip_count 個 step。
      flow.loop : params {count} 或 {for_each_var, body_count, var?}
                  把接下來 body_count 個 step 重複跑 count 次,
                  或對 for_each_var(list)每個元素跑一次(元素寫進 loop 變數)。
                  以 loop_stack 支援巢狀。
    其餘 step 照常經 action registry 分派。
    """
    steps = flow.steps
    idx_by_id = {s.id: i for i, s in enumerate(steps)}
    total = len(steps)
    ok_n = fail_n = 0
    status = "completed"
    i = 0
    # loop_stack:每筆 {"return_to","body_end","remaining","items","var"}
    loop_stack: list[dict] = []

    def _subst_params(step):
        """對單一 step 做 {var} 替換 + secret 注入(與一般 step 同邏輯)。"""
        try:
            step.params = ctx.vars.substitute_params(step.params)
            if step.target:
                step.target = ctx.vars.substitute_params(step.target)
        except Exception:
            pass

    while i < total:
        if ctx.should_stop():
            status = "stopped"
            break
        step = steps[i]

        # ---- 控制流:flow.if ---- #
        if step.action == "flow.if":
            _subst_params(step)
            p = step.params or {}
            var_val = ctx.vars.get(p.get("var"))
            cond = _eval_condition(var_val, p.get("op", "eq"), p.get("value"))
            skip = 0 if cond else int(p.get("skip_count", 0) or 0)
            ctx.store.log_step(ctx.run_id, step.id, step.action,
                               "ok", 0, 0, "", "")
            ctx.log(f"[{i+1}/{total}] flow.if {p.get('var')} {p.get('op','eq')} "
                    f"-> {cond} (skip {skip})")
            if on_progress:
                try:
                    on_progress(i, total, step, ActionResult(ok=True, value=cond))
                except Exception:
                    pass
            ok_n += 1
            i += 1 + skip
            continue

        # ---- 控制流:flow.loop ---- #
        if step.action == "flow.loop":
            _subst_params(step)
            p = step.params or {}
            for_each = p.get("for_each_var")
            if for_each is not None:
                body_count = int(p.get("body_count", 0) or 0)
                items = ctx.vars.get(for_each)
                items = list(items) if isinstance(items, (list, tuple)) else []
                loop_var = p.get("var", "item")
            else:
                body_count = int(p.get("body_count", p.get("count_body", 0)) or 0)
                # 注意:沒給 body_count 時退而用「接下來全部」不安全,故預設 0。
                count = int(p.get("count", 0) or 0)
                body_count = body_count or 0
                items = None
            body_start = i + 1
            body_end = body_start + body_count    # exclusive

            ctx.store.log_step(ctx.run_id, step.id, step.action, "ok", 0, 0, "", "")
            if on_progress:
                try:
                    on_progress(i, total, step, ActionResult(ok=True))
                except Exception:
                    pass
            ok_n += 1

            if for_each is not None:
                if not items or body_count <= 0:
                    ctx.log(f"[{i+1}/{total}] flow.loop for_each {for_each}: 0 次")
                    i = body_end
                    continue
                first = items[0]
                ctx.vars.set(loop_var, first)
                loop_stack.append({
                    "body_start": body_start, "body_end": body_end,
                    "items": items, "index": 0, "var": loop_var, "count": None,
                })
                ctx.log(f"[{i+1}/{total}] flow.loop for_each {for_each}: {len(items)} 次")
            else:
                if count <= 0 or body_count <= 0:
                    ctx.log(f"[{i+1}/{total}] flow.loop count={count}: 不執行")
                    i = body_end
                    continue
                loop_stack.append({
                    "body_start": body_start, "body_end": body_end,
                    "items": None, "index": 0, "var": None, "count": count,
                })
                ctx.log(f"[{i+1}/{total}] flow.loop count={count}")
            i = body_start
            continue

        # ---- 一般 step:變數替換(params 內所有字串) ---- #
        try:
            step.params = ctx.vars.substitute_params(step.params)
            if step.target:
                step.target = ctx.vars.substitute_params(step.target)
        except Exception:
            pass
        # secret 注入(只在執行期帶入,不落地)
        if step.secret_ref and ctx.vault is not None:
            secret = ctx.vault.get_secret(step.secret_ref)
            if secret is not None:
                step.params = dict(step.params)
                step.params["_secret"] = secret

        res, retries, ms, shot = _exec_step(step, ctx)
        st = "ok" if res.ok else ("stopped" if res.error == "stopped" else "failed")
        ctx.store.log_step(ctx.run_id, step.id, step.action, st, ms, retries,
                           res.error or "", shot)
        ctx.log(f"[{i+1}/{total}] {step.action} -> {st}"
                + (f" ({res.error})" if not res.ok else ""))
        if on_progress:
            try:
                on_progress(i, total, step, res)
            except Exception:
                pass

        if res.error == "stopped":
            status = "stopped"
            break
        if res.ok:
            ok_n += 1
        else:
            fail_n += 1
            oe = step.on_error or "abort"
            if oe == "abort":
                status = "failed"
                break
            elif oe == "continue":
                pass
            elif oe.startswith("goto:"):
                target = oe.split(":", 1)[1]
                if target in idx_by_id:
                    i = idx_by_id[target]
                    continue
        i += 1
        # ---- 迴圈邊界:若走到最內層 loop 的 body_end,決定重跑或退出 ---- #
        while loop_stack and i >= loop_stack[-1]["body_end"]:
            top = loop_stack[-1]
            top["index"] += 1
            if top["items"] is not None:
                if top["index"] < len(top["items"]):
                    ctx.vars.set(top["var"], top["items"][top["index"]])
                    i = top["body_start"]
                    break
                loop_stack.pop()                # for_each 跑完
            else:
                if top["index"] < top["count"]:
                    i = top["body_start"]
                    break
                loop_stack.pop()                # count 迴圈跑完

    return RunResult(status=status, steps_total=total, steps_ok=ok_n,
                     steps_failed=fail_n, variables=ctx.vars.all())
