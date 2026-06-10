# -*- coding: utf-8 -*-
"""Desktop 定位器:把 schema 的 target 解析成 pywinauto wrapper。

target 結構(見 core/schema.py Step.target):
  {
    "primary":   {"strategy": "uia"|"title"|"auto_id", "value": "..."},
    "fallbacks": [ {strategy,value}, ... ],
    "fingerprint": {...}   # 預留,本層未使用
  }

支援的 strategy(fallback 順序見 spec §2:UIA → win32 → image(CV)→ coord):
  - uia     : value 為 JSON 字串(或 dict),欄位:
              {title?, control_type?, auto_id?, name?, class_name?, window_title?}
              先用 backend=uia 定位;找不到時 fallback 到 win32。
  - title   : value 為視窗/控制項標題(字串)
  - auto_id : value 為 AutomationId(僅 uia 有意義)
  - image   : value 為 anchor PNG 檔名,從「該 flow 的 anchor 目錄」(anchor_dir)找檔,
              用 engines.vision CV 比對,命中回傳「螢幕點目標」ScreenPoint。
  - coord   : value 為 "x,y" 螢幕座標(最後手段),回傳 ScreenPoint。

回傳:
  - uia / win32 命中  -> pywinauto wrapper(已 .wrapper_object(),可 .click_input() 等)
  - image / coord 命中 -> ScreenPoint(帶 .coords() / .click()),非 pywinauto wrapper。
    動作層(actions.py)需要能同時處理這兩種回傳物件。

anchor_dir:image 策略要找 anchor 檔的目錄,由呼叫方(actions)傳入 resolve(),
通常取自 ctx.extra["anchor_dir"]。
"""
from __future__ import annotations
import json
import os
import re
import time

from core import heal as _heal

# 單次 child_window 尋找逾時設「很短」(0.4s):因為強化版會試「多視窗 × 多候選 × 雙
# backend」很多組合,每組合若等太久會累加爆炸(實測曾達 63s)。重播時元素本就該在,
# 找不到就快點換下一個組合 / 往 image(CV)/coord 走。總時間另由 _resolve_uia 的預算上限封頂。
try:
    from pywinauto.timings import Timings as _Timings
    _Timings.window_find_timeout = 0.4
except Exception:
    pass

# 整個 UIA 定位(所有視窗/候選/backend 嘗試)的總時間預算(秒);超過就放棄 → 走 CV/coord。
_UIA_TOTAL_BUDGET = 4.0

_PLOG_PATH = os.path.join("logs", "playback_debug.log")


def _plog(msg: str):
    """把重播定位的詳細過程寫進 logs/playback_debug.log,供事後檢視。"""
    try:
        os.makedirs("logs", exist_ok=True)
        with open(_PLOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


class ScreenPoint:
    """image / coord 策略命中後回傳的「螢幕點目標」。

    刻意不是 pywinauto wrapper —— 它只代表「一個螢幕座標」。
    動作層用 .coords() 取座標、或直接呼叫 .click() 點擊(走 pyautogui /
    pywinauto mouse)。提供與 pywinauto wrapper 部分相容的介面以降低 actions
    分支成本。

    屬性:
      x, y      : 螢幕絕對座標
      strategy  : "image" | "coord"(debug 用)
    """

    def __init__(self, x: int, y: int, strategy: str = "coord"):
        self.x = int(x)
        self.y = int(y)
        self.strategy = strategy

    def coords(self):
        return (self.x, self.y)

    def click(self, button: str = "left"):
        """點此螢幕座標。優先 pyautogui,退到 pywinauto.mouse。"""
        try:
            import pyautogui  # type: ignore
            pyautogui.click(x=self.x, y=self.y, button=button)
            return
        except Exception:
            pass
        from pywinauto import mouse  # type: ignore
        mouse.click(button=button, coords=(self.x, self.y))

    # 與 pywinauto wrapper 部分相容:click_input 等同 click
    def click_input(self, button: str = "left"):
        self.click(button=button)

    def __repr__(self):
        return f"ScreenPoint({self.x},{self.y},{self.strategy})"


def is_screen_point(obj) -> bool:
    return isinstance(obj, ScreenPoint)


def _resolve_image(value, anchor_dir, confidence: float = 0.85):
    """image 策略:從 anchor_dir 找 anchor 檔,用 CV 比對 -> ScreenPoint 或丟錯。"""
    if not value:
        raise ValueError("image strategy 缺少 value(anchor 檔名)")
    # value 可能已是絕對路徑;否則接在 anchor_dir 下
    path = value
    if not os.path.isabs(path):
        if not anchor_dir:
            raise ValueError(
                f"image strategy 需要 anchor_dir 才能定位 anchor 檔: {value!r}")
        path = os.path.join(anchor_dir, value)
    if not os.path.exists(path):
        raise FileNotFoundError(f"anchor 檔不存在: {path}")

    from engines.vision import image_match
    x, y, score = image_match.locate_score(path, confidence=confidence)
    if x is None:
        best = f"{score:.3f}" if score is not None else "n/a"
        raise RuntimeError(f"CV 比對未達門檻 {confidence}(最佳分數 {best}) anchor: {path}")
    sp = ScreenPoint(x, y, strategy="image")
    sp.score = float(score)   # 供日誌記錄實際比對率/信心度
    return sp


def _resolve_coord(value):
    """coord 策略:value="x,y" -> ScreenPoint。"""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return ScreenPoint(int(value[0]), int(value[1]), strategy="coord")
    if not isinstance(value, str) or "," not in value:
        raise ValueError(f"coord strategy 的 value 需為 'x,y':{value!r}")
    xs, ys = value.split(",", 1)
    return ScreenPoint(int(float(xs.strip())), int(float(ys.strip())),
                       strategy="coord")


def _parse_uia_value(value):
    """uia strategy 的 value 可能是 JSON 字串或已是 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except Exception:
                pass
        # 非 JSON 的純字串:當成 title
        return {"title": value}
    return {}


def _spec_to_kwargs(spec: dict) -> dict:
    """把 uia spec dict 轉成 pywinauto child_window/window 的關鍵字。

    pywinauto 的關鍵字:title / control_type / auto_id / class_name / found_index ...
    這裡把 name 也對應到 title(UIA Name == title)。
    """
    kw: dict = {}
    if spec.get("title"):
        kw["title"] = spec["title"]
    elif spec.get("name"):
        kw["title"] = spec["name"]
    if spec.get("control_type"):
        kw["control_type"] = spec["control_type"]
    if spec.get("auto_id"):
        kw["auto_id"] = spec["auto_id"]
    if spec.get("class_name"):
        kw["class_name"] = spec["class_name"]
    return kw


def _ordered_candidates(spec: dict):
    """產生 child_window 搜尋條件,由「最穩」到「最寬鬆」逐條退讓。

    auto_id 最不易變 → 其次 name+control_type → name → class+type → type+index。
    這樣即使部分屬性在重播時變了,仍有機會用較寬鬆的條件命中。
    """
    name = spec.get("title") or spec.get("name")
    ctype = spec.get("control_type")
    aid = spec.get("auto_id")
    cls = spec.get("class_name")
    if aid:
        yield {"auto_id": aid}
    if name and ctype:
        yield {"title": name, "control_type": ctype}
    if name:
        yield {"title": name}
    if cls and ctype:
        yield {"class_name": cls, "control_type": ctype}
    if ctype:
        yield {"control_type": ctype, "found_index": 0}


def _find_in_window(win, spec: dict):
    """在指定 window 內逐條退讓嘗試候選條件,任一成功即回 wrapper。"""
    cands = list(_ordered_candidates(spec))
    if not cands:
        return win.wrapper_object()
    last = None
    for kw in cands:
        try:
            return win.child_window(**kw).wrapper_object()
        except Exception as e:  # noqa: BLE001
            last = e
    raise last or RuntimeError("window 內無候選命中")


def _title_variants(title: str):
    """視窗標題模糊變體:處理會變動的標題(如 '常用 - 檔案總管')。"""
    title = (title or "").strip()
    if not title:
        return
    parts = [p.strip() for p in title.split(" - ") if p.strip()]
    if len(parts) > 1:
        yield parts[0]
        yield parts[-1]
    head = title[:12].strip()
    if head and head != title:
        yield head


def _iter_window_getters(desktop, controller, window_title):
    """產生候選頂層視窗的 getter,由精確到模糊。"""
    if window_title:
        yield lambda: desktop.window(title=window_title)
        for v in _title_variants(window_title):
            yield (lambda v=v: desktop.window(title_re=f".*{re.escape(v)}.*"))
    if getattr(controller, "app", None) is not None:
        yield lambda: controller.app.top_window()
    yield lambda: desktop.window(active_only=True)


def _find_global_by_aid(automation_id: str, timeout: float = 0.8):
    """全域:從 UIA 根元素掃整棵樹找指定 AutomationId,回元素中心螢幕座標或 None。

    用於抓「不在一般 top-level window 列舉裡」的 shell UI(開始功能表、Action
    Center 等)。FindFirst/FindAll 是 blocking,故用 daemon thread + join 限制
    等待上限;過濾 offscreen 與 0x0 殘留元素。
    """
    if not automation_id:
        return None
    try:
        from pywinauto.uia_defines import IUIA
        iuia = IUIA().iuia
        _AID_PROP = 30011   # UIA_AutomationIdPropertyId
        _SCOPE_DESC = 4     # TreeScope_Descendants
        root = iuia.GetRootElement()
        cond = iuia.CreatePropertyCondition(_AID_PROP, automation_id)
    except Exception as e:  # noqa: BLE001
        _plog(f"    ↳ 全域搜尋初始化失敗: {e}")
        return None

    import threading
    out = {"pt": None}
    cancelled = threading.Event()

    def _center(elem):
        if elem is None:
            return None
        try:
            if elem.CurrentIsOffscreen:
                return None
        except Exception:
            pass
        try:
            r = elem.CurrentBoundingRectangle
            w, h = r.right - r.left, r.bottom - r.top
            if w <= 0 or h <= 0:
                return None
            return (int(r.left + w / 2), int(r.top + h / 2))
        except Exception:
            return None

    def _worker():
        try:
            elem = root.FindFirst(_SCOPE_DESC, cond)
        except Exception:
            elem = None
        if cancelled.is_set():
            return
        c = _center(elem)
        if c:
            out["pt"] = c
            return
        if elem is None:
            return  # 沒有元素就不浪費時間跑 FindAll
        try:
            els = root.FindAll(_SCOPE_DESC, cond)
            n = els.Length
        except Exception:
            return
        for i in range(n):
            if cancelled.is_set():
                return
            try:
                c = _center(els.GetElement(i))
            except Exception:
                continue
            if c:
                out["pt"] = c
                return

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=max(timeout, 0.1))
    cancelled.set()
    return out["pt"]


def _top_window(controller, backend: str, window_title: str | None):
    """取得要在其中搜尋的頂層視窗。

    優先序:
      1) spec.window_title -> 用對應 backend 的 Desktop().window(title_re=...)
      2) controller.app.top_window()(本 session 啟動/附掛的 app)
      3) Desktop().windows() 中第一個可見(極少用到)
    """
    desktop = controller.desktop if backend == "uia" else (
        controller.win32_desktop or controller.desktop)

    if window_title:
        return desktop.window(title_re=window_title)
    if getattr(controller, "app", None) is not None:
        try:
            return controller.app.top_window()
        except Exception:
            pass
    # 退而求其次:用 desktop active window
    return desktop.window(active_only=True)


def _resolve_uia(controller, spec: dict):
    """UIA 定位(強化版):多視窗(含標題模糊變體)× 逐條退讓候選 ×
    win32 fallback × 全域 AutomationId 搜尋(抓 Start 選單等 shell UI)。
    回傳 pywinauto wrapper 或 ScreenPoint(全域命中時)。"""
    window_title = (spec.get("window_title") or "").strip() or None
    last_err = None
    deadline = time.time() + _UIA_TOTAL_BUDGET   # 總時間上限,避免組合爆炸卡住

    for backend in ("uia", "win32"):
        if time.time() > deadline:
            break
        desktop = controller.desktop if backend == "uia" else controller.win32_desktop
        if desktop is None:
            continue
        for get_win in _iter_window_getters(desktop, controller, window_title):
            if time.time() > deadline:
                last_err = last_err or TimeoutError(
                    f"UIA 定位超過總預算 {_UIA_TOTAL_BUDGET}s,放棄改走 fallback")
                break
            try:
                win = get_win()
                try:
                    win.wait("exists", timeout=0.4)
                except Exception:
                    pass
                return _find_in_window(win, spec)
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue

    # ---- 全域 AutomationId 搜尋:抓不在一般視窗列舉裡的 shell UI(開始功能表等)----
    aid = (spec.get("auto_id") or "").strip()
    if aid:
        pt = _find_global_by_aid(aid, timeout=0.8)
        if pt is not None:
            _plog(f"    ↳ 靠全域 AutomationId 命中: aid={aid!r} @ {pt}")
            return ScreenPoint(pt[0], pt[1], strategy="uia-global")

    raise RuntimeError(f"uia/win32/global locate failed for {spec!r}: {last_err}")


# --------------------------------------------------------------------------- #
# Self-healing(自癒)
# --------------------------------------------------------------------------- #
_HEAL_SCAN_CAP = 400  # UIA 樹掃描上限,避免超大視窗爆量


def wrapper_to_candidate(w) -> dict:
    """把一個 pywinauto wrapper 轉成 core.heal 可評分的候選 dict。

    抽出 name / control_type / auto_id / class_name 等識別資訊;
    任一欄位讀取失敗就略過(不可讓掃描中斷)。
    """
    def _safe(fn, default=""):
        try:
            v = fn()
            return v if v is not None else default
        except Exception:
            return default

    elem = getattr(w, "element_info", None)
    name = _safe(lambda: w.window_text())
    control_type = ""
    auto_id = ""
    class_name = ""
    if elem is not None:
        control_type = getattr(elem, "control_type", "") or ""
        auto_id = getattr(elem, "automation_id", "") or ""
        class_name = getattr(elem, "class_name", "") or ""
    if not class_name:
        class_name = _safe(lambda: w.class_name())
    return {
        "text": name,
        "name": name,
        "control_type": control_type,
        "role": control_type,
        "auto_id": auto_id,
        "class_name": class_name,
        "attrs": {"name": name, "auto_id": auto_id, "class_name": class_name},
    }


def _scan_uia_tree(controller, window_title: str | None):
    """掃當前頂層視窗的 UIA 後代控制項,回傳 [(candidate_dict, wrapper), ...]。"""
    win = _top_window(controller, "uia", window_title)
    try:
        descendants = win.descendants()
    except Exception:
        descendants = []
    out = []
    for w in descendants[:_HEAL_SCAN_CAP]:
        try:
            out.append((wrapper_to_candidate(w), w))
        except Exception:
            continue
    return out


def score_candidates(fingerprint: dict, candidates, threshold: float = 0.7):
    """純評分:對 [(candidate_dict, payload), ...] 算分,挑最高過門檻者。

    回傳 (payload, score, detail);沒過門檻回 (None, best_score, detail)。
    candidate_dict 走 core.heal.score_candidate;payload 是要回傳的物件
    (desktop 為 pywinauto wrapper)。本函式不碰 GUI,方便單元測試。
    """
    cand_dicts = [c for c, _ in candidates]
    idx, score, detail = _heal.best_candidate(fingerprint, cand_dicts, threshold)
    if idx is None:
        return None, score, detail
    return candidates[idx][1], score, detail


def _fingerprint_for_heal(target: dict) -> dict:
    """把 target.fingerprint 整理成 core.heal 用的扁平 fingerprint。

    spec 的 fingerprint.uia 是巢狀 dict;這裡攤平 name/control_type/auto_id/
    class_name 到頂層,並補 text。
    """
    fp = dict((target or {}).get("fingerprint") or {})
    uia = fp.get("uia") or {}
    flat = {
        "text": fp.get("text") or uia.get("name"),
        "name": uia.get("name"),
        "control_type": uia.get("control_type"),
        "role": uia.get("control_type"),
        "auto_id": uia.get("auto_id"),
        "class_name": uia.get("class_name"),
    }
    return {k: v for k, v in flat.items() if v}


def heal(controller, target: dict, threshold: float = 0.7):
    """desktop 自癒:掃 UIA 樹候選,挑與 fingerprint 最像且過門檻者。

    回傳 (wrapper, score, detail);找不到回 (None, score, detail)。
    """
    fingerprint = _fingerprint_for_heal(target)
    if not fingerprint:
        return None, 0.0, {"reason": "target 無可用 fingerprint,無法自癒"}
    window_title = ((target.get("fingerprint") or {}).get("uia") or {}).get(
        "window_title")
    try:
        candidates = _scan_uia_tree(controller, window_title)
    except Exception as e:  # noqa: BLE001
        return None, 0.0, {"reason": f"掃描 UIA 樹失敗: {e}"}
    if not candidates:
        return None, 0.0, {"reason": "當前畫面掃不到 UIA 候選"}
    payload, score, detail = score_candidates(fingerprint, candidates, threshold)
    if payload is None:
        detail = dict(detail or {})
        detail["reason"] = f"最佳候選 score={score:.3f} 未過門檻 {threshold}"
        return None, score, detail
    return payload, score, detail


def resolve(controller, target, anchor_dir: str | None = None,
            report: dict | None = None, heal_enabled: bool = True,
            heal_threshold: float = 0.7):
    """主入口:依 target 解析出可操作物件。

    fallback 順序(spec §2):primary 與 fallbacks 逐一嘗試,任一成功即用。
    各 strategy 內部還有自己的 fallback(uia 內含 uia→win32)。
      - uia / title / auto_id -> pywinauto wrapper
      - image / coord          -> ScreenPoint(螢幕點目標)

    anchor_dir:image 策略要找 anchor 檔的目錄(呼叫方傳入,通常 ctx.extra["anchor_dir"])。

    report:選填可變 dict。命中時填 {"strategy","score","detail"}:
      - 正常命中:strategy=該策略字串、score=1.0。
      - 自癒命中:strategy="heal"、score=分數、detail=評分明細。
      呼叫端(action)據此 ctx.store.log_heal(...)。不傳 report 行為與舊版相同。

    全部 fallback 失敗且 heal_enabled 時啟動 self-healing(掃 UIA 樹挑最像者);
    仍失敗才丟出最後一個錯誤。**不修改 flow 檔**,替換只在本次執行生效。
    """
    if not target or "primary" not in target:
        raise ValueError("target 缺少 primary locator")

    candidates = [target["primary"]] + list(target.get("fallbacks", []) or [])
    last_err: Exception | None = None
    attempts: list = []

    # 日誌標頭:用 fingerprint 的元素名稱/型別當識別,方便事後對照是哪一步。
    _fp = (target.get("fingerprint") or {}).get("uia") or {}
    _label = _fp.get("name") or _fp.get("auto_id") or _fp.get("control_type") or "?"
    _plog(f"=== resolve 元素[{_label}] 候選層: "
          f"{[ (c or {}).get('strategy') for c in candidates ]} ===")

    for loc in candidates:
        strategy = (loc or {}).get("strategy", "uia")
        value = (loc or {}).get("value")
        confidence = (loc or {}).get("confidence", 0.85)
        t0 = time.time()
        try:
            if strategy == "uia":
                spec = _parse_uia_value(value)
                w = _resolve_uia(controller, spec)
            elif strategy == "title":
                w = _resolve_uia(controller, {"title": value})
            elif strategy == "auto_id":
                w = _resolve_uia(controller, {"auto_id": value})
            elif strategy == "image":
                w = _resolve_image(value, anchor_dir, confidence=confidence)
            elif strategy == "coord":
                w = _resolve_coord(value)
            else:
                raise ValueError(f"unsupported desktop strategy: {strategy!r}")
            ms = int((time.time() - t0) * 1000)
            # image 命中帶實際比對率;其餘層命中視為精確比對(score=1.0)。
            score = float(getattr(w, "score", 1.0)) if strategy == "image" else 1.0
            conf_txt = f" 比對率={score:.3f}(門檻 {confidence})" if strategy == "image" else ""
            _plog(f"  ✔ 命中 [{strategy}] {ms}ms{conf_txt}")
            attempts.append({"strategy": strategy, "ok": True, "ms": ms,
                             "score": score})
            if report is not None:
                report["strategy"] = strategy
                report["score"] = score
                report["detail"] = {"value": value}
                report["attempts"] = attempts
            return w
        except Exception as e:  # noqa: BLE001
            ms = int((time.time() - t0) * 1000)
            last_err = e
            _plog(f"  ✘ 失敗 [{strategy}] {ms}ms → {type(e).__name__}: "
                  f"{str(e)[:120]}")
            attempts.append({"strategy": strategy, "ok": False, "ms": ms,
                             "err": f"{type(e).__name__}: {str(e)[:120]}"})
            continue

    # ---- 全部失敗 -> 嘗試 self-healing(指紋評分挑最像者)----
    if heal_enabled and (target.get("fingerprint")):
        t0 = time.time()
        try:
            w, score, detail = heal(controller, target, threshold=heal_threshold)
        except Exception as e:  # noqa: BLE001
            w, score, detail = None, 0.0, {"reason": f"heal 例外: {e}"}
        ms = int((time.time() - t0) * 1000)
        if w is not None:
            _plog(f"  ✔ 自癒命中 [heal] {ms}ms 信心度={score:.3f} "
                  f"(門檻 {heal_threshold}) {detail}")
            attempts.append({"strategy": "heal", "ok": True, "ms": ms,
                             "score": score, "detail": detail})
            if report is not None:
                report["strategy"] = "heal"
                report["score"] = score
                report["detail"] = detail
                report["attempts"] = attempts
            return w
        _plog(f"  ✘ 自癒未過門檻 [heal] {ms}ms 最佳信心度={score:.3f} "
              f"(門檻 {heal_threshold}) {detail.get('reason','')}")
        attempts.append({"strategy": "heal", "ok": False, "ms": ms,
                         "score": score})
        last_err = RuntimeError(
            f"heal failed (best score={score:.3f}); "
            f"{detail.get('reason', '')}; prior: {last_err}")

    if report is not None:
        report["attempts"] = attempts
    _plog(f"  ✘✘ 元素[{_label}] 所有定位層皆失敗: {last_err}")
    raise RuntimeError(f"all locators failed for target={target!r}: {last_err}")
