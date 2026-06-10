# -*- coding: utf-8 -*-
"""desktop.* 動作(pywinauto backend=uia)。

每個動作簽名:fn(ctx, step) -> ActionResult|None,經 @action 註冊。
ctx.engine 為 DesktopController(見 session.py)。
逾時用 step.timeout_ms;可中斷點查 ctx.should_stop()。

注意:本模組在 import 時就完成註冊,不在 import 時觸碰 pywinauto/GUI,
因此即使在 headless 環境也能成功 import 並註冊(只有實際執行動作才需要 GUI)。
"""
from __future__ import annotations
import time

from core.registry import action, ActionResult
from . import locators


def _timeout_s(step, default: float = 15.0) -> float:
    try:
        return max(0.1, int(step.timeout_ms) / 1000.0)
    except Exception:
        return default


def _check_stop(ctx):
    if ctx.should_stop():
        return ActionResult(ok=False, error="stopped")
    return None


def _anchor_dir(ctx):
    """從 ctx.extra 取該 flow 的 anchor 目錄(image 策略用);沒有回 None。"""
    try:
        return (ctx.extra or {}).get("anchor_dir")
    except Exception:
        return None


def _heal_opts(ctx):
    """從 ctx.extra 取 heal 開關與門檻(可開關、門檻可調;預設開、0.7)。"""
    extra = (getattr(ctx, "extra", None) or {})
    enabled = extra.get("heal_enabled", True)
    threshold = extra.get("heal_threshold", 0.7)
    try:
        threshold = float(threshold)
    except Exception:
        threshold = 0.7
    return bool(enabled), threshold


def _record_heal(ctx, step, report: dict):
    """若這次解析走自癒(strategy=='heal'),記進 store 供人審核;只記 log 不改檔。

    ctx 沒有 store / run_id 時略過記錄(仍已完成替換)。
    """
    if not report or report.get("strategy") != "heal":
        return
    store = getattr(ctx, "store", None)
    run_id = getattr(ctx, "run_id", None)
    step_id = getattr(step, "id", "")
    if store is None or not hasattr(store, "log_heal") or not run_id:
        try:
            ctx.log(f"[heal] step={step_id} score={report.get('score')} "
                    f"(無 store/run_id,未記錄)")
        except Exception:
            pass
        return
    try:
        store.log_heal(run_id, step_id, "heal(desktop)",
                       report.get("score", 0.0), report.get("detail", ""))
    except Exception:
        pass


def _resolve(ctx, target, step=None):
    """包一層 locators.resolve,自動帶入 anchor_dir;觸發自癒時記 heal log。"""
    report: dict = {}
    enabled, threshold = _heal_opts(ctx)
    w = locators.resolve(ctx.engine, target, anchor_dir=_anchor_dir(ctx),
                         report=report, heal_enabled=enabled,
                         heal_threshold=threshold)
    if step is not None:
        _record_heal(ctx, step, report)
    return w


def _click_resolved(w, button: str = "left"):
    """點擊 resolve() 回傳的物件:可能是 pywinauto wrapper 或 ScreenPoint。

    - ScreenPoint(image/coord 螢幕點):走 .click(button)(內部 pyautogui / mouse)。
    - pywinauto wrapper:優先 click_input;UIA invoke fallback 用 click()。
    button 僅對螢幕點 / 支援的 wrapper 有意義。
    """
    if locators.is_screen_point(w):
        w.click(button=button)
        return
    # pywinauto wrapper
    try:
        if button and button != "left":
            w.click_input(button=button)
        else:
            w.click_input()
    except Exception:
        w.click()  # invoke pattern fallback


def _find_window(controller, title: str, timeout: float):
    """在 uia desktop 等待並回傳符合 title_re 的頂層視窗 wrapper。"""
    backend = getattr(controller, "backend", "uia")
    desktop = controller.desktop if backend == "uia" else (
        controller.win32_desktop or controller.desktop)
    win = desktop.window(title_re=title)
    win.wait("visible exists", timeout=timeout)
    return win


# ---------------------------------------------------------------- focus_window
@action("desktop.focus_window")
def focus_window(ctx, step):
    """desktop.focus_window(params.title):把指定標題的視窗帶到前景並聚焦。"""
    stop = _check_stop(ctx)
    if stop:
        return stop
    title = (step.params or {}).get("title")
    if not title:
        return ActionResult(ok=False, error="focus_window 需要 params.title")
    controller = ctx.engine
    timeout = _timeout_s(step)
    try:
        win = _find_window(controller, title, timeout)
        try:
            win.set_focus()
        except Exception:
            win.set_focus()  # 二次嘗試;某些視窗第一次會閃
        return ActionResult(ok=True, value=title)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"focus_window failed: {e}")


# ----------------------------------------------------------------------- click
@action("desktop.click")
def click(ctx, step):
    """desktop.click(target):定位控制項並點擊。

    target 走 UIA → win32 → image(CV)→ coord 逐層 fallback。
    命中 UIA/win32 回 wrapper(click_input);命中 image/coord 回 ScreenPoint
    (用 pyautogui / pywinauto mouse 點座標)。兩者由 _click_resolved 統一處理。
    params.button 可選 left/right/middle。
    """
    stop = _check_stop(ctx)
    if stop:
        return stop
    button = (step.params or {}).get("button", "left")
    timeout = _timeout_s(step)
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            w = _resolve(ctx, step.target, step)
            _click_resolved(w, button=button)
            return ActionResult(ok=True)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.3)
            if ctx.should_stop():
                return ActionResult(ok=False, error="stopped")
    return ActionResult(ok=False, error=f"click failed: {last_err}")


# ------------------------------------------------------------------------ type
@action("desktop.type")
def type_text(ctx, step):
    """desktop.type(target?, params.text):輸入文字。

    - 給 target:先定位該編輯控制項並聚焦。Edit 類優先用 set_edit_text
      (直接設值,不模擬鍵盤 -> 不會掉字/重複,內容精準);
      set_edit_text 不可用(非 Edit / ValuePattern 唯讀失敗)才退到 type_keys。
    - 不給 target:對目前焦點視窗 send_keys(無法用 set_edit_text)。
    - params.append=True 時保留原內容、把 text 接在後面(預設覆寫)。
    - 支援 _secret:runner 注入的 secret 值會覆蓋 text。
    """
    stop = _check_stop(ctx)
    if stop:
        return stop
    params = step.params or {}
    text = params.get("_secret", params.get("text", ""))
    if text is None:
        text = ""
    append = bool(params.get("append", False))
    controller = ctx.engine
    timeout = _timeout_s(step)
    try:
        if step.target:
            deadline = time.time() + timeout
            last_err = None
            target_w = None
            while time.time() < deadline:
                try:
                    target_w = _resolve(ctx, step.target, step)
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    time.sleep(0.3)
            if target_w is None:
                return ActionResult(ok=False, error=f"type target not found: {last_err}")
            try:
                target_w.set_focus()
            except Exception:
                pass

            # 1) 優先 set_edit_text(精準、不掉字)
            new_text = text
            if append:
                try:
                    new_text = _read_text(target_w) + text
                except Exception:
                    new_text = text
            done = False
            for setter in ("set_edit_text", "set_text"):
                fn = getattr(target_w, setter, None)
                if fn is None:
                    continue
                try:
                    fn(new_text)
                    done = True
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
            # 2) 退到 type_keys(鍵盤模擬;只在 set_* 全失敗時用)
            if not done:
                target_w.type_keys(_escape(text), with_spaces=True,
                                   with_newlines=True, pause=0.02)
        else:
            from pywinauto.keyboard import send_keys
            send_keys(_escape(text), with_spaces=True, with_newlines=True,
                      pause=0.02)
        return ActionResult(ok=True, value=len(text))
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"type failed: {e}")


def _escape(text: str) -> str:
    """type_keys 會把 {}()+^%~ 當特殊鍵;把純文字中的這些字元跳脫。"""
    out = []
    for ch in str(text):
        if ch in "{}()+^%~[]":
            out.append("{" + ch + "}")
        elif ch == "\n":
            out.append("{ENTER}")
        elif ch == "\t":
            out.append("{TAB}")
        else:
            out.append(ch)
    return "".join(out)


# ------------------------------------------------------------------------ read
@action("desktop.read")
def read(ctx, step):
    """desktop.read(target, params.var):讀控制項文字,存入變數。"""
    stop = _check_stop(ctx)
    if stop:
        return stop
    params = step.params or {}
    var = params.get("var")
    if not var:
        return ActionResult(ok=False, error="read 需要 params.var")
    controller = ctx.engine
    timeout = _timeout_s(step)
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            w = _resolve(ctx, step.target, step)
            value = _read_text(w)
            ctx.vars.set(var, value)
            return ActionResult(ok=True, value=value)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.3)
            if ctx.should_stop():
                return ActionResult(ok=False, error="stopped")
    return ActionResult(ok=False, error=f"read failed: {last_err}")


def _read_text(w) -> str:
    """盡量取出控制項文字:window_text -> texts() -> get_value()。"""
    # Edit 控制項:texts() 通常回 [全部內容]
    try:
        texts = w.texts()
        if texts:
            # texts()[0] 多為標題;Edit 內容常在後續或就是全部
            joined = "\n".join(t for t in texts if t)
            if joined:
                return joined
    except Exception:
        pass
    try:
        return w.window_text()
    except Exception:
        pass
    try:
        return w.get_value()  # ValuePattern
    except Exception:
        return ""


# ------------------------------------------------------------------------ wait
@action("desktop.wait")
def wait(ctx, step):
    """desktop.wait(params.seconds):固定等待(可中斷)。"""
    seconds = float((step.params or {}).get("seconds", 1))
    end = time.time() + seconds
    while time.time() < end:
        if ctx.should_stop():
            return ActionResult(ok=False, error="stopped")
        time.sleep(min(0.2, max(0.0, end - time.time())))
    return ActionResult(ok=True)


# -------------------------------------------------------------------- wait_for
@action("desktop.wait_for")
def wait_for(ctx, step):
    """desktop.wait_for(target):等待控制項出現(逾時 step.timeout_ms)。"""
    stop = _check_stop(ctx)
    if stop:
        return stop
    controller = ctx.engine
    timeout = _timeout_s(step)
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        if ctx.should_stop():
            return ActionResult(ok=False, error="stopped")
        try:
            _resolve(ctx, step.target, step)
            return ActionResult(ok=True)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.3)
    return ActionResult(ok=False, error=f"wait_for timeout: {last_err}")


# ----------------------------------------------------------------- menu_select
@action("desktop.menu_select")
def menu_select(ctx, step):
    """desktop.menu_select(params.path):選功能表,path 例 "File->Open"。

    優先對 focus_window 指定的視窗;否則對 app top_window。
    """
    stop = _check_stop(ctx)
    if stop:
        return stop
    params = step.params or {}
    path = params.get("path")
    if not path:
        return ActionResult(ok=False, error="menu_select 需要 params.path")
    controller = ctx.engine
    timeout = _timeout_s(step)
    win_title = params.get("window_title")
    try:
        if win_title:
            win = _find_window(controller, win_title, timeout)
        else:
            win = controller.top_window()
        if win is None:
            return ActionResult(ok=False, error="menu_select 找不到目標視窗")
        win.menu_select(path)
        return ActionResult(ok=True, value=path)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"menu_select failed: {e}")


# ------------------------------------------------------------------- send_keys
@action("desktop.send_keys")
def send_keys(ctx, step):
    """desktop.send_keys(params.keys):送出按鍵序列(pywinauto 語法,例 "^s")。

    keys 直接照 pywinauto.keyboard.send_keys 語法,不做跳脫(由使用者掌控特殊鍵)。
    """
    stop = _check_stop(ctx)
    if stop:
        return stop
    keys = (step.params or {}).get("keys")
    if not keys:
        return ActionResult(ok=False, error="send_keys 需要 params.keys")
    try:
        from pywinauto.keyboard import send_keys as _sk
        _sk(keys, with_spaces=True, with_newlines=True)
        return ActionResult(ok=True, value=keys)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"send_keys failed: {e}")


# ================================================ Vision 視覺層動作(CV + OCR)
def _resolve_anchor_path(ctx, anchor: str) -> str:
    """把 anchor 檔名解析成完整路徑(絕對路徑直接用,否則接 anchor_dir)。"""
    import os
    if not anchor:
        raise ValueError("anchor 不可為空")
    if os.path.isabs(anchor):
        return anchor
    adir = _anchor_dir(ctx)
    if adir:
        return os.path.join(adir, anchor)
    return anchor  # 交給 vision 層,找不到再報錯


# ------------------------------------------------------------------ wait_image
@action("desktop.wait_image")
def wait_image(ctx, step):
    """desktop.wait_image(params.anchor/timeout/confidence):等 anchor 影像出現。

    用 engines.vision CV 比對輪詢;長等待查 ctx.should_stop()。
    成功:value=(x,y) 中心螢幕座標。逾時 / 被中斷:fail。
    """
    stop = _check_stop(ctx)
    if stop:
        return stop
    params = step.params or {}
    anchor = params.get("anchor")
    if not anchor:
        return ActionResult(ok=False, error="wait_image 需要 params.anchor")
    confidence = float(params.get("confidence", 0.85))
    # timeout 優先用 params.timeout(秒),否則退到 step.timeout_ms
    timeout = params.get("timeout")
    timeout = float(timeout) if timeout is not None else _timeout_s(step, 10.0)
    try:
        from engines.vision import image_match
        path = _resolve_anchor_path(ctx, anchor)
        pt = image_match.wait_locate(
            path, timeout=timeout, confidence=confidence,
            stop_event=ctx.stop_event)
        if ctx.should_stop():
            return ActionResult(ok=False, error="stopped")
        if pt is None:
            return ActionResult(ok=False, error=f"wait_image timeout: {anchor}")
        return ActionResult(ok=True, value=pt)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"wait_image failed: {e}")


# ----------------------------------------------------------------- image_click
@action("desktop.image_click")
def image_click(ctx, step):
    """desktop.image_click(params.anchor/confidence/button):CV 定位後點擊。

    截螢幕用 CV 找 anchor -> 點該中心座標(pyautogui / pywinauto mouse)。
    在 step.timeout_ms(預設 10s)內輪詢重試;長等待查 ctx.should_stop()。
    """
    stop = _check_stop(ctx)
    if stop:
        return stop
    params = step.params or {}
    anchor = params.get("anchor")
    if not anchor:
        return ActionResult(ok=False, error="image_click 需要 params.anchor")
    confidence = float(params.get("confidence", 0.85))
    button = params.get("button", "left")
    timeout = _timeout_s(step, 10.0)
    try:
        from engines.vision import image_match
        path = _resolve_anchor_path(ctx, anchor)
        pt = image_match.wait_locate(
            path, timeout=timeout, confidence=confidence,
            stop_event=ctx.stop_event)
        if ctx.should_stop():
            return ActionResult(ok=False, error="stopped")
        if pt is None:
            return ActionResult(ok=False, error=f"image_click 未命中 anchor: {anchor}")
        sp = locators.ScreenPoint(pt[0], pt[1], strategy="image")
        sp.click(button=button)
        return ActionResult(ok=True, value=pt)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"image_click failed: {e}")


# -------------------------------------------------------------------- ocr_read
@action("desktop.ocr_read")
def ocr_read(ctx, step):
    """desktop.ocr_read(params.x/y/w/h/var):OCR 該螢幕區域,寫入 ctx.vars。

    用 engines.vision.ocr.read_region(rapidocr,中英混排)。
    OCR 後端不可用時 graceful 回空字串(動作仍視為成功,變數=空字串)。
    """
    stop = _check_stop(ctx)
    if stop:
        return stop
    params = step.params or {}
    var = params.get("var")
    if not var:
        return ActionResult(ok=False, error="ocr_read 需要 params.var")
    try:
        x = int(params["x"]); y = int(params["y"])
        w = int(params["w"]); h = int(params["h"])
    except (KeyError, ValueError, TypeError) as e:
        return ActionResult(ok=False, error=f"ocr_read 需要數值 x/y/w/h: {e}")
    try:
        from engines.vision import ocr
        text = ocr.read_region(x, y, w, h)
        ctx.vars.set(var, text)
        return ActionResult(ok=True, value=text)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"ocr_read failed: {e}")
