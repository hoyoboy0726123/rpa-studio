# -*- coding: utf-8 -*-
"""Self-healing(自癒定位)共用評分。

當 primary + 所有 fallback 定位器都失敗,引擎層(web / desktop)會在「當前畫面」
掃描出一批候選元素,用此模組的純函式對每個候選與 step.target.fingerprint 算分,
取最高且過門檻者替換使用。

設計重點:
  * 純函式、零 GUI / 瀏覽器相依 —— 方便單元測試評分邏輯。
  * 分數落在 0~1;由三個訊號加權:
      - 文字相似度(difflib.SequenceMatcher,佔比最高,因文字最能識別元素)
      - role / control_type 命中(完全相符給滿分,否則 0)
      - 屬性命中(name / auto_id / testid / placeholder ... 命中比例)
  * 三訊號各自可缺(fingerprint 沒提供就不計、權重重新正規化),
    確保「只有 text」或「只有 uia 屬性」的 fingerprint 也能評分。

回傳 (score, detail) 供呼叫端記 heal log(detail 是可序列化 dict)。
"""
from __future__ import annotations
import difflib

# 三個訊號的基礎權重(會依「該候選實際能比對到哪些訊號」重新正規化)
W_TEXT = 0.5
W_ROLE = 0.2
W_ATTRS = 0.3


def text_similarity(a: str, b: str) -> float:
    """兩段文字的相似度(0~1)。大小寫不敏感、去頭尾空白。"""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a and not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _norm(s) -> str:
    return ("" if s is None else str(s)).strip().lower()


def score_candidate(fingerprint: dict, candidate: dict) -> tuple[float, dict]:
    """對單一候選算分。

    fingerprint(來自 step.target.fingerprint,web/desktop 共用扁平鍵):
        text         : 期望可見文字
        role         : web role 或 desktop control_type(任一)
        control_type : desktop control_type(等同 role 用途)
        attrs        : dict,期望屬性(name / auto_id / testid / placeholder ...)
    candidate(引擎層掃描當前畫面後整理):同上鍵,描述現場某個元素實況。

    回傳 (score, detail);detail 含各分項與權重,方便 heal log 審核。
    """
    fingerprint = fingerprint or {}
    candidate = candidate or {}

    parts = {}      # 訊號名 -> (子分數, 權重)

    # ---- 文字 ----
    fp_text = fingerprint.get("text")
    if fp_text:
        sub = text_similarity(fp_text, candidate.get("text", ""))
        parts["text"] = (sub, W_TEXT)

    # ---- role / control_type(視為同一訊號)----
    fp_role = _norm(fingerprint.get("role") or fingerprint.get("control_type"))
    if fp_role:
        cand_role = _norm(candidate.get("role") or candidate.get("control_type"))
        sub = 1.0 if (cand_role and cand_role == fp_role) else 0.0
        parts["role"] = (sub, W_ROLE)

    # ---- 屬性命中(命中比例)----
    fp_attrs = fingerprint.get("attrs") or {}
    # fingerprint 頂層常見的 uia 欄位也納入屬性比對
    for k in ("name", "auto_id", "class_name", "testid", "placeholder"):
        if fingerprint.get(k) and k not in fp_attrs:
            fp_attrs[k] = fingerprint.get(k)
    if fp_attrs:
        cand_attrs = dict(candidate.get("attrs") or {})
        for k in ("name", "auto_id", "class_name", "testid", "placeholder"):
            if candidate.get(k) and k not in cand_attrs:
                cand_attrs[k] = candidate.get(k)
        hit = 0
        for k, v in fp_attrs.items():
            if _norm(cand_attrs.get(k)) and _norm(cand_attrs.get(k)) == _norm(v):
                hit += 1
        sub = hit / len(fp_attrs) if fp_attrs else 0.0
        parts["attrs"] = (sub, W_ATTRS)

    if not parts:
        return 0.0, {"reason": "fingerprint 無可比對訊號", "parts": {}}

    # 依實際參與的訊號重新正規化權重 -> 分數仍落在 0~1
    total_w = sum(w for _, w in parts.values())
    score = sum(sub * w for sub, w in parts.values()) / total_w

    detail = {
        "score": round(score, 4),
        "parts": {k: {"sub": round(sub, 4), "weight": round(w / total_w, 4)}
                  for k, (sub, w) in parts.items()},
        "candidate": _candidate_brief(candidate),
    }
    return score, detail


def _candidate_brief(candidate: dict) -> dict:
    """候選的精簡描述(寫進 heal log,別塞整個物件)。"""
    out = {}
    for k in ("text", "role", "control_type", "name", "auto_id",
              "class_name", "testid"):
        v = candidate.get(k)
        if v:
            out[k] = (str(v)[:80])
    attrs = candidate.get("attrs")
    if attrs:
        out["attrs"] = {k: str(v)[:60] for k, v in dict(attrs).items()}
    return out


def best_candidate(fingerprint: dict, candidates, threshold: float = 0.7):
    """對一批候選算分,回傳 (best_index, best_score, best_detail)。

    candidates: 可迭代的候選 dict 序列。
    回傳的 best_index 為 None 表示沒有任何候選過門檻。
    """
    best_i = None
    best_score = -1.0
    best_detail = {}
    for i, cand in enumerate(candidates):
        s, d = score_candidate(fingerprint, cand)
        if s > best_score:
            best_i, best_score, best_detail = i, s, d
    if best_i is None or best_score < threshold:
        return None, (best_score if best_score >= 0 else 0.0), best_detail
    return best_i, best_score, best_detail
