# -*- coding: utf-8 -*-
"""多定位器解析 (locator resolution)。

target = {
  "primary":   {"strategy": <str>, "value": <str>},
  "fallbacks": [ {"strategy": ..., "value": ...}, ... ]   # 選填
}

strategy 支援:
  role   : value 為 "button:登入" 或 JSON {"role":"button","name":"登入"}
  text   : 以可見文字定位
  testid : data-testid
  css    : CSS selector
  xpath  : XPath(自動補 "xpath=" 前綴)
  coord  : "x,y" 像素座標(回傳一個能 click 的合成 locator)

策略:依序試 primary -> fallbacks,回傳第一個「count()>0」的 Playwright Locator。
靠 Playwright 內建 auto-wait,呼叫端(action)實際操作時才會等到元素 ready。

Self-healing(自癒):primary + 所有 fallback 都失敗時,若 target 帶 fingerprint
且 heal 開啟(預設開),會在「當前 page」掃描候選元素,用 core.heal 的評分
(文字相似度 + role 命中 + 屬性命中)挑最像且過門檻者回傳,並把 heal 資訊
(strategy='heal', score, detail)寫進 resolve(report=...)。**不修改 flow 檔**,
替換只在本次執行生效;由 action 層把 heal 記進 store.log_heal 供人審核。
"""
from __future__ import annotations
import json

from core import heal as _heal


class _CoordLocator:
    """coord 策略的合成 locator:沒有 DOM 元素,改用滑鼠座標點擊。

    僅實作 web.click 會用到的 click(),以及定位流程需要的 count()。
    其餘 Playwright Locator 方法不支援(coord 僅供點擊用途)。
    """

    def __init__(self, page, x: int, y: int):
        self._page = page
        self.x = x
        self.y = y

    def count(self) -> int:
        return 1

    def click(self, **kw):
        self._page.mouse.click(self.x, self.y)

    def wait_for(self, **kw):
        # coord 無 DOM 元素可等,直接通過
        return None


def _build(page, strategy: str, value: str):
    """把單一 (strategy, value) 轉成 Playwright Locator(或 _CoordLocator)。"""
    strategy = (strategy or "").lower().strip()

    if strategy == "role":
        role, name = _parse_role(value)
        if name:
            return page.get_by_role(role, name=name)
        return page.get_by_role(role)

    if strategy == "text":
        return page.get_by_text(value)

    if strategy == "testid":
        return page.get_by_test_id(value)

    if strategy == "css":
        return page.locator(value)

    if strategy == "xpath":
        v = value if value.startswith("xpath=") else f"xpath={value}"
        return page.locator(v)

    if strategy == "coord":
        x, y = (int(float(p.strip())) for p in value.split(","))
        return _CoordLocator(page, x, y)

    raise ValueError(f"unknown locator strategy: {strategy!r}")


def _parse_role(value: str):
    """解析 role value。
    支援 "button:登入"(role:name)或 JSON {"role":...,"name":...} 或純 "button"。
    回傳 (role, name|None)。
    """
    value = value.strip()
    if value.startswith("{"):
        d = json.loads(value)
        return d.get("role", "button"), d.get("name")
    if ":" in value:
        role, name = value.split(":", 1)
        return role.strip(), name.strip()
    return value, None


def _candidates(target: dict):
    """攤平 primary + fallbacks 成 (strategy, value) 序列。"""
    if not target:
        return
    primary = target.get("primary")
    if primary:
        yield primary.get("strategy"), primary.get("value")
    for fb in target.get("fallbacks", []) or []:
        yield fb.get("strategy"), fb.get("value")


# --------------------------------------------------------------------------- #
# Self-healing(自癒)
# --------------------------------------------------------------------------- #
# 掃描當前 page 可點擊 / 可互動元素時,涵蓋的 role 清單(get_by_role)。
_HEAL_ROLES = (
    "button", "link", "textbox", "checkbox", "radio", "tab", "menuitem",
    "combobox", "option", "switch", "searchbox", "spinbutton",
)
_HEAL_SCAN_CAP = 60  # 候選掃描上限,避免大頁面爆量


def _scan_candidates(page, fingerprint: dict):
    """在當前 page 掃描候選元素,整理成 core.heal 可評分的 dict。

    每個候選同時帶回 "_locator"(對應 Playwright Locator)供命中後回傳。
    候選來源:
      1) 若 fingerprint 有 role -> 優先掃該 role 的元素(最精準)。
      2) 退而求其次:掃常見可互動 role 全集。
    """
    fingerprint = fingerprint or {}
    seen = []
    fp_role = (fingerprint.get("role") or "").strip().lower()
    roles = [fp_role] if fp_role else list(_HEAL_ROLES)

    for role in roles:
        try:
            loc = page.get_by_role(role)
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, _HEAL_SCAN_CAP)):
            el = loc.nth(i)
            try:
                text = (el.inner_text(timeout=200) or "").strip()
            except Exception:
                try:
                    text = (el.text_content(timeout=200) or "").strip()
                except Exception:
                    text = ""
            cand = {
                "role": role,
                "text": text,
                "attrs": _read_attrs(el),
                "_locator": el,
            }
            seen.append(cand)
            if len(seen) >= _HEAL_SCAN_CAP:
                return seen
    return seen


def _read_attrs(el) -> dict:
    """讀候選元素的識別屬性(失敗就略過,不可讓掃描中斷)。"""
    out = {}
    for attr, key in (("data-testid", "testid"), ("name", "name"),
                      ("id", "auto_id"), ("class", "class_name"),
                      ("placeholder", "placeholder"),
                      ("aria-label", "name")):
        try:
            v = el.get_attribute(attr, timeout=200)
        except Exception:
            v = None
        if v and key not in out:
            out[key] = v
    return out


def heal(page, target: dict, threshold: float = 0.7):
    """web 自癒:掃當前 page 候選,挑與 fingerprint 最像且過門檻者。

    回傳 (locator, score, detail);找不到回 (None, score, detail)。
    """
    fingerprint = (target or {}).get("fingerprint") or {}
    if not fingerprint:
        return None, 0.0, {"reason": "target 無 fingerprint,無法自癒"}
    candidates = _scan_candidates(page, fingerprint)
    if not candidates:
        return None, 0.0, {"reason": "當前畫面掃不到候選元素"}
    idx, score, detail = _heal.best_candidate(fingerprint, candidates, threshold)
    if idx is None:
        detail = dict(detail or {})
        detail["reason"] = f"最佳候選 score={score:.3f} 未過門檻 {threshold}"
        return None, score, detail
    return candidates[idx]["_locator"], score, detail


def resolve(page, target: dict, report: dict | None = None,
            heal_enabled: bool = True, heal_threshold: float = 0.7):
    """依序試 primary -> fallbacks,回傳第一個能定位到(count>0)的 Locator。

    全部失敗且 heal_enabled 時,啟動 self-healing:用 fingerprint 在當前 page
    掃描候選並挑最像者替換回傳。

    report:選填的可變 dict。命中時填 {"strategy", "score", "detail"};
      - 正常命中(primary/fallback):strategy=該策略字串、score=1.0。
      - 自癒命中:strategy="heal"、score=分數、detail=評分明細。
      呼叫端(action)可據此 ctx.store.log_heal(...)。不傳 report 則行為與舊版相同。

    自癒仍失敗才丟出最後一個錯誤,交由 runner 處理 retry/截圖。
    """
    last_err = None
    tried = 0
    for strategy, value in _candidates(target):
        tried += 1
        try:
            loc = _build(page, strategy, value)
            # coord 永遠回傳;DOM 類則確認至少有一個候選
            if isinstance(loc, _CoordLocator) or loc.count() > 0:
                if report is not None:
                    report["strategy"] = strategy
                    report["score"] = 1.0
                    report["detail"] = {"value": value}
                return loc
            last_err = f"locator matched 0 elements: {strategy}={value!r}"
        except Exception as e:  # 該策略本身爆掉(語法錯/逾時)→ 試下一個
            last_err = f"{strategy}={value!r}: {type(e).__name__}: {e}"
            continue

    # ---- 全部失敗 -> 嘗試 self-healing ----
    if heal_enabled and target and target.get("fingerprint"):
        try:
            loc, score, detail = heal(page, target, threshold=heal_threshold)
        except Exception as e:  # noqa: BLE001
            loc, score, detail = None, 0.0, {"reason": f"heal 例外: {e}"}
        if loc is not None:
            if report is not None:
                report["strategy"] = "heal"
                report["score"] = score
                report["detail"] = detail
            return loc
        last_err = (f"heal failed (best score={score:.3f}); "
                    f"{detail.get('reason', '')}; prior: {last_err}")

    if tried == 0 and not (target or {}).get("fingerprint"):
        raise ValueError("empty target: no primary/fallbacks to resolve")
    raise RuntimeError(f"locator resolve failed ({tried} tried); last: {last_err}")
