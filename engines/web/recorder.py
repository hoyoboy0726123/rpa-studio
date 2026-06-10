# -*- coding: utf-8 -*-
"""Web 錄製器 — Playwright codegen 轉譯成我們的 flow JSON。

設計:
  1. `record_web(url, out_flow_path)`:用 subprocess 跑
     `playwright codegen --target python -o <tmp.py> <url>`,讓使用者實際操作;
     關閉 codegen 視窗後,Playwright 會把錄到的動作寫成一個 python script。
  2. `parse_codegen_python(code)`:**純文字解析器**(不需開瀏覽器),把 codegen 的
     python 動作(goto / click / fill / select_option / press / check / ...)轉成
     我們的 `web.*` steps。可單獨被測試。

轉譯出的 target 遵守 docs/phase2_spec.md §1:
  - primary 用語意定位(role / text / label / placeholder → 對應 web 的 role|text)
  - fallbacks 補 css / xpath(codegen 的 .locator("css=") / get_by_test_id 等)
  - fingerprint 帶 text(供 self-healing / debug)

codegen 產出的典型呼叫:
    page.goto("https://x")
    page.get_by_role("button", name="登入").click()
    page.get_by_label("帳號").fill("alice")
    page.get_by_placeholder("搜尋").fill("abc")
    page.get_by_text("更多").click()
    page.get_by_test_id("submit").click()
    page.locator("#user").fill("bob")
    page.locator("css=.row").click()
    page.get_by_role("textbox").press("Enter")
    page.get_by_role("combobox").select_option("TW")

注意:codegen 互動(實際開瀏覽器讓人操作)無法在自動化測試環境驗證;
測試只覆蓋「解析器」這段確定性邏輯(見 tests/test_recorder_smoke.py)。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

from core.schema import Flow, Step, new_id


# --------------------------------------------------------------------------- #
# 內部:解析「定位器表達式」鏈(get_by_* / locator(...) 串接)
# --------------------------------------------------------------------------- #

# 比對單一 get_by_* / locator() 呼叫;在 chain 中可能出現多個(例 .filter())
# 我們只取「鏈上第一個能成為主定位」的呼叫,其餘忽略(MVP)。
_RE_GET_BY_ROLE = re.compile(
    r'get_by_role\(\s*(["\'])(?P<role>.*?)\1'
    r'(?:\s*,\s*name\s*=\s*(["\'])(?P<name>.*?)\3)?'
    r'(?:\s*,\s*exact\s*=\s*(?:True|False))?\s*\)')
_RE_GET_BY_TEXT = re.compile(
    r'get_by_text\(\s*(["\'])(?P<text>.*?)\1'
    r'(?:\s*,\s*exact\s*=\s*(?:True|False))?\s*\)')
_RE_GET_BY_LABEL = re.compile(
    r'get_by_label\(\s*(["\'])(?P<label>.*?)\1'
    r'(?:\s*,\s*exact\s*=\s*(?:True|False))?\s*\)')
_RE_GET_BY_PLACEHOLDER = re.compile(
    r'get_by_placeholder\(\s*(["\'])(?P<ph>.*?)\1'
    r'(?:\s*,\s*exact\s*=\s*(?:True|False))?\s*\)')
_RE_GET_BY_TESTID = re.compile(
    r'get_by_test_id\(\s*(["\'])(?P<tid>.*?)\1\s*\)')
_RE_LOCATOR = re.compile(
    r'\.locator\(\s*(["\'])(?P<sel>.*?)\1\s*\)')


def _selector_to_locator(sel: str) -> dict | None:
    """把 page.locator("...") 的字串轉成 {strategy, value}。

    Playwright 接受 "css=...", "xpath=...", "text=...", 或裸 CSS。
    """
    s = sel.strip()
    if s.startswith("xpath=") or s.startswith("//") or s.startswith("("):
        return {"strategy": "xpath", "value": s[len("xpath="):] if s.startswith("xpath=") else s}
    if s.startswith("css="):
        return {"strategy": "css", "value": s[len("css="):]}
    if s.startswith("text="):
        return {"strategy": "text", "value": s[len("text="):].strip('"\'')}
    # 裸字串:當 CSS
    return {"strategy": "css", "value": s}


def parse_locator_chain(expr: str) -> dict:
    """把一段 codegen 的定位器表達式(page 之後、動作 .click() 之前)解析成 target。

    回傳 docs §1 的 target dict:primary + fallbacks + fingerprint。
    優先序(primary):role > text > label/placeholder(text) > testid > css/xpath。
    其餘抓到的定位器塞進 fallbacks(去重),fingerprint.text 紀錄可見文字。
    """
    primary: dict | None = None
    fallbacks: list[dict] = []
    text_hint: str | None = None

    m = _RE_GET_BY_ROLE.search(expr)
    if m:
        role = m.group("role")
        name = m.group("name")
        value = f"{role}:{name}" if name else role
        primary = {"strategy": "role", "value": value}
        if name:
            text_hint = name

    m = _RE_GET_BY_TEXT.search(expr)
    if m:
        txt = m.group("text")
        cand = {"strategy": "text", "value": txt}
        text_hint = text_hint or txt
        if primary is None:
            primary = cand
        else:
            fallbacks.append(cand)

    m = _RE_GET_BY_LABEL.search(expr)
    if m:
        lbl = m.group("label")
        # label 在我們的策略集裡沒有專屬值;用 text 近似定位 + 記 fingerprint
        cand = {"strategy": "text", "value": lbl}
        text_hint = text_hint or lbl
        if primary is None:
            primary = cand
        else:
            fallbacks.append(cand)

    m = _RE_GET_BY_PLACEHOLDER.search(expr)
    if m:
        ph = m.group("ph")
        # placeholder → CSS 屬性選擇器(精準);primary 缺則升為 primary
        cand_css = {"strategy": "css", "value": f'[placeholder="{ph}"]'}
        text_hint = text_hint or ph
        if primary is None:
            primary = cand_css
        else:
            fallbacks.append(cand_css)

    m = _RE_GET_BY_TESTID.search(expr)
    if m:
        tid = m.group("tid")
        cand = {"strategy": "testid", "value": tid}
        if primary is None:
            primary = cand
        else:
            fallbacks.append(cand)

    # locator("css=..") / locator("//xpath") → 補進 fallbacks(或當 primary)
    for lm in _RE_LOCATOR.finditer(expr):
        cand = _selector_to_locator(lm.group("sel"))
        if not cand:
            continue
        if primary is None:
            primary = cand
        elif cand not in fallbacks and cand != primary:
            fallbacks.append(cand)

    if primary is None:
        # 兜底:整段當 css(極少發生,通常 codegen 一定有定位器)
        primary = {"strategy": "css", "value": expr.strip()}

    target: dict = {"primary": primary, "fallbacks": fallbacks}
    fp: dict = {}
    if text_hint:
        fp["text"] = text_hint
    if fp:
        target["fingerprint"] = fp
    return target


# --------------------------------------------------------------------------- #
# 內部:把單一 codegen 陳述句轉成一個 Step(或 None 表示忽略)
# --------------------------------------------------------------------------- #

# page.goto("url")
_RE_GOTO = re.compile(r'\bpage\d*\.goto\(\s*(["\'])(?P<url>.*?)\1')
# 行尾的動作呼叫:.click() / .fill("x") / .select_option("v") / .press("Enter") /
# .check() / .uncheck() / .dblclick()
_RE_ACTION_TAIL = re.compile(
    r'\.(?P<act>click|dblclick|fill|type|press|select_option|check|uncheck|'
    r'set_input_files)\(\s*(?P<args>.*?)\s*\)\s*$')

_RE_FIRST_STR_ARG = re.compile(r'^\s*(["\'])(?P<val>.*?)\1')


def _first_str_arg(args: str) -> str | None:
    """從動作參數字串取第一個字串常數(fill/press/select_option 的值)。"""
    m = _RE_FIRST_STR_ARG.match(args)
    return m.group("val") if m else None


def codegen_line_to_step(line: str) -> Step | None:
    """把 codegen 的一行 python 轉成一個 web.* Step;不可轉的回 None。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # 1) goto
    g = _RE_GOTO.search(line)
    if g and ".goto(" in line:
        url = g.group("url")
        return Step(id=new_id(), action="web.goto",
                    label=f"goto {url}", params={"url": url})

    # 2) 必須是對某定位器鏈做動作:page....<action>(...)
    if not re.search(r'\bpage\d*\.', line):
        return None
    tail = _RE_ACTION_TAIL.search(line)
    if not tail:
        return None

    act = tail.group("act")
    args = tail.group("args")

    # 定位器表達式 = 去掉行尾動作那段
    locator_expr = line[: tail.start()]
    target = parse_locator_chain(locator_expr)

    if act in ("click", "dblclick"):
        return Step(id=new_id(), action="web.click",
                    label=_label("click", target), target=target,
                    params={"double": True} if act == "dblclick" else {})

    if act in ("fill", "type"):
        val = _first_str_arg(args) or ""
        return Step(id=new_id(), action="web.fill",
                    label=_label("fill", target), target=target,
                    params={"value": val})

    if act == "press":
        key = _first_str_arg(args) or ""
        return Step(id=new_id(), action="web.press",
                    label=f"press {key}", target=target,
                    params={"key": key})

    if act == "select_option":
        val = _first_str_arg(args) or ""
        return Step(id=new_id(), action="web.select",
                    label=_label("select", target), target=target,
                    params={"value": val})

    if act in ("check", "uncheck"):
        # 沒有專屬 web.check;用 click 表達(codegen 的 checkbox 點擊)
        return Step(id=new_id(), action="web.click",
                    label=_label(act, target), target=target, params={})

    if act == "set_input_files":
        val = _first_str_arg(args) or ""
        # 檔案上傳:MVP 用 fill 帶檔名(回放層可再特化);保留語意於 label
        return Step(id=new_id(), action="web.fill",
                    label=f"set_input_files {val}", target=target,
                    params={"value": val})

    return None


def _label(verb: str, target: dict) -> str:
    fp = (target.get("fingerprint") or {})
    hint = fp.get("text") or (target.get("primary") or {}).get("value", "")
    return f"{verb} {hint}".strip()


# --------------------------------------------------------------------------- #
# 對外:解析整段 codegen python → Flow dict
# --------------------------------------------------------------------------- #
def parse_codegen_python(code: str, flow_name: str = "web_recording") -> dict:
    """把整段 codegen python 字串轉成 Flow dict(engine='web')。

    只挑出 `page.*` 動作行;import / with sync_playwright / browser launch 等樣板忽略。
    """
    steps: list[dict] = []
    for raw in code.splitlines():
        step = codegen_line_to_step(raw)
        if step is not None:
            steps.append(_step_to_dict(step))

    flow = Flow(name=flow_name, engine="web")
    flow.steps = [Step.from_dict(s) for s in steps]
    return flow.to_dict()


def _step_to_dict(step: Step) -> dict:
    from dataclasses import asdict
    return asdict(step)


# --------------------------------------------------------------------------- #
# 對外:啟動 codegen 互動錄製 → 存 flow JSON
# --------------------------------------------------------------------------- #
def _run_codegen(url: str, tmp_py: str, timeout: float | None = None) -> None:
    """呼叫 `playwright codegen --target python -o <tmp_py> <url>`。

    使用者實際操作、關閉視窗後 codegen 才結束並寫出 tmp_py。
    優先用 `python -m playwright`(綁定當前直譯器),退到 PATH 上的 `playwright`。
    """
    cmds = [
        [sys.executable, "-m", "playwright", "codegen",
         "--target", "python", "-o", tmp_py, url],
        ["playwright", "codegen", "--target", "python", "-o", tmp_py, url],
    ]
    last_err: Exception | None = None
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True, timeout=timeout)
            return
        except FileNotFoundError as e:
            last_err = e
            continue
    raise RuntimeError(
        f"無法啟動 playwright codegen(請先 `pip install playwright` 並 "
        f"`playwright install chromium`):{last_err}")


def record_web(url: str, out_flow_path: str, flow_name: str | None = None,
               timeout: float | None = None) -> str:
    """啟動 Playwright codegen 錄 web 操作,轉成 flow JSON 存檔並回傳路徑。

    流程:
      1. codegen 開瀏覽器到 url,使用者操作 → 關閉視窗 → codegen 寫出 tmp python。
      2. 讀 tmp python → parse_codegen_python → Flow dict。
      3. 存成 out_flow_path 並回傳。

    注意:此函式需要真實桌面 + 已安裝 playwright,無法在 headless CI 驗證;
    純轉譯邏輯請見 parse_codegen_python(可單測)。
    """
    if flow_name is None:
        flow_name = os.path.splitext(os.path.basename(out_flow_path))[0] or "web_recording"

    tmp_fd, tmp_py = tempfile.mkstemp(suffix="_codegen.py", prefix="rpa_")
    os.close(tmp_fd)
    try:
        _run_codegen(url, tmp_py, timeout=timeout)
        with open(tmp_py, "r", encoding="utf-8") as fh:
            code = fh.read()
    finally:
        try:
            os.remove(tmp_py)
        except OSError:
            pass

    flow_dict = parse_codegen_python(code, flow_name=flow_name)
    # 把起始 url 也存進 variables(方便 UI 顯示/重用)
    flow_dict.setdefault("variables", {})["start_url"] = url

    out_dir = os.path.dirname(os.path.abspath(out_flow_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_flow_path, "w", encoding="utf-8") as fh:
        json.dump(flow_dict, fh, ensure_ascii=False, indent=2)
    return out_flow_path
