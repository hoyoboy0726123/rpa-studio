# -*- coding: utf-8 -*-
"""流程編輯器的純邏輯 (flow editing operations)。

把「對一條 Flow 的 step 做增 / 刪 / 上下移動 / 改欄位」抽成不依賴 Qt 的純函式,
讓 editor_page 的 UI 只負責把表單值丟進來、把結果畫出去,真正的結構操作可被
tests 直接呼叫驗證(不必點 UI)。所有函式都「就地修改」傳入的 Flow 並回傳它。

對應 core.schema.Step 欄位:
    id / action / label / target / params / secret_ref / retry / timeout_ms / on_error
target schema:{"primary": {"strategy", "value"},
               "fallbacks": [{"strategy", "value"}, ...]}
"""
from __future__ import annotations

from core.schema import Flow, Step, new_id


# --------------------------------------------------------------------------- #
# step 增 / 刪 / 移動
# --------------------------------------------------------------------------- #
def add_step(flow: Flow, action: str = "flow.set_var", label: str = "",
             at: int | None = None) -> Step:
    """新增一個 step;at=None 時加到最後,否則插在索引 at。回傳新 Step。"""
    step = Step(id=new_id(), action=action, label=label)
    if at is None or at < 0 or at > len(flow.steps):
        flow.steps.append(step)
    else:
        flow.steps.insert(at, step)
    return step


def delete_step(flow: Flow, index: int) -> bool:
    """刪除索引 index 的 step。成功回傳 True,索引超界回傳 False。"""
    if 0 <= index < len(flow.steps):
        flow.steps.pop(index)
        return True
    return False


def move_step(flow: Flow, index: int, delta: int) -> int:
    """把 step 上移 (delta=-1) 或下移 (delta=+1)。回傳移動後的新索引。
    無法移動(到頂 / 到底 / 索引超界)時回傳原索引。"""
    n = len(flow.steps)
    if not (0 <= index < n):
        return index
    new_index = index + delta
    if not (0 <= new_index < n):
        return index
    flow.steps[index], flow.steps[new_index] = flow.steps[new_index], flow.steps[index]
    return new_index


# --------------------------------------------------------------------------- #
# 編輯單一 step 的欄位
# --------------------------------------------------------------------------- #
def update_step_basic(step: Step, *, action: str | None = None,
                      label: str | None = None,
                      secret_ref: str | None = None,
                      on_error: str | None = None,
                      timeout_ms: int | None = None) -> Step:
    """更新 step 的基本欄位(只改有傳進來的)。"""
    if action is not None:
        step.action = action
    if label is not None:
        step.label = label
    if secret_ref is not None:
        step.secret_ref = secret_ref or None
    if on_error is not None:
        step.on_error = on_error or "abort"
    if timeout_ms is not None:
        step.timeout_ms = int(timeout_ms)
    return step


def set_params(step: Step, params: dict) -> Step:
    """整批覆寫 params(來自 key-value 表格)。空字串 key 會被忽略。"""
    clean = {}
    for k, v in (params or {}).items():
        k = (k or "").strip()
        if k:
            clean[k] = v
    step.params = clean
    return step


def set_retry(step: Step, times: int, interval_ms: int) -> Step:
    step.retry = {"times": max(0, int(times)),
                  "interval_ms": max(0, int(interval_ms))}
    return step


def set_target(step: Step,
               primary_strategy: str = "",
               primary_value: str = "",
               fallbacks: list[tuple[str, str]] | None = None) -> Step:
    """設定 step.target。primary 為空且無 fallbacks 時 target 設為 None。

    fallbacks: [(strategy, value), ...];strategy 為空者跳過。
    """
    fb = []
    for strat, val in (fallbacks or []):
        strat = (strat or "").strip()
        if strat:
            fb.append({"strategy": strat, "value": val})
    primary_strategy = (primary_strategy or "").strip()
    if not primary_strategy and not fb:
        step.target = None
        return step
    target: dict = {}
    if primary_strategy:
        target["primary"] = {"strategy": primary_strategy, "value": primary_value}
    if fb:
        target["fallbacks"] = fb
    step.target = target
    return step


# --------------------------------------------------------------------------- #
# 把編輯結果存回 Store
# --------------------------------------------------------------------------- #
def save_flow_to_store(flow: Flow, store) -> dict:
    """正規化並存回 Store(store.save_flow)。回傳寫入的 dict。"""
    d = flow.to_dict()
    store.save_flow(d)
    return d


# --------------------------------------------------------------------------- #
# UI 下拉用:展平 ACTION_CATALOG
# --------------------------------------------------------------------------- #
def all_actions(engine: str | None = None) -> list[str]:
    """回傳可選 action 清單。engine 指定時優先列該引擎 + flow.* 通用動作;
    None 時回傳全部。"""
    from core.schema import ACTION_CATALOG
    if engine and engine in ("web", "desktop"):
        actions = list(ACTION_CATALOG.get(engine, []))
        # flow / data / comms 為引擎無關的通用動作,任何引擎都可用
        for grp in ("flow", "data", "comms"):
            actions += list(ACTION_CATALOG.get(grp, []))
        return actions
    if engine and engine in ACTION_CATALOG:
        return list(ACTION_CATALOG[engine])
    out: list[str] = []
    for group in ACTION_CATALOG.values():
        out.extend(group)
    return out
