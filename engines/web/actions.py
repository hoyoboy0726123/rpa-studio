# -*- coding: utf-8 -*-
"""web.* actions — Playwright 引擎動作集。

每個動作簽名 `def fn(ctx, step) -> ActionResult`,以 ctx.engine(= Playwright Page)操作。
runner 已負責 retry / timeout / 失敗截圖 / log / on_error;這裡只把「單一動作」做對。

逾時統一用 step.timeout_ms;長等待時檢查 ctx.should_stop() 以支援中斷。
"""
from __future__ import annotations
import time

from core.registry import action, ActionResult
from . import locators


def _page(ctx):
    return ctx.engine


def _timeout(step) -> int:
    """取 step 逾時(ms);防呆給預設 15s。"""
    try:
        return int(step.timeout_ms or 15000)
    except Exception:
        return 15000


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
    """若這次解析走的是自癒(strategy=='heal'),把它記進 store 供人審核。

    刻意只記 log、不回寫 flow 檔:替換僅本次執行生效。
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
        store.log_heal(run_id, step_id, "heal(web)",
                       report.get("score", 0.0), report.get("detail", ""))
    except Exception:
        pass


def _target(ctx, step):
    """解析 step.target 成 Playwright Locator;若觸發自癒則記 heal log。"""
    report: dict = {}
    enabled, threshold = _heal_opts(ctx)
    loc = locators.resolve(_page(ctx), step.target, report=report,
                           heal_enabled=enabled, heal_threshold=threshold)
    _record_heal(ctx, step, report)
    return loc


# --------------------------------------------------------------------------- #
# 導航 / 互動
# --------------------------------------------------------------------------- #
@action("web.goto")
def web_goto(ctx, step) -> ActionResult:
    url = step.params.get("url")
    if not url:
        return ActionResult(ok=False, error="web.goto 缺少 params.url")
    _page(ctx).goto(url, timeout=_timeout(step))
    return ActionResult(ok=True, value=url)


@action("web.click")
def web_click(ctx, step) -> ActionResult:
    loc = _target(ctx, step)
    loc.click(timeout=_timeout(step))
    return ActionResult(ok=True)


@action("web.fill")
def web_fill(ctx, step) -> ActionResult:
    """填值;若 step.params 帶 "_secret"(runner 注入)則填 secret 值,優先於 value。"""
    loc = _target(ctx, step)
    value = step.params.get("_secret")
    if value is None:
        value = step.params.get("value", "")
    loc.fill(str(value), timeout=_timeout(step))
    return ActionResult(ok=True)


@action("web.select")
def web_select(ctx, step) -> ActionResult:
    loc = _target(ctx, step)
    value = step.params.get("value", "")
    loc.select_option(str(value), timeout=_timeout(step))
    return ActionResult(ok=True, value=value)


@action("web.press")
def web_press(ctx, step) -> ActionResult:
    """按鍵。有 target 則對該元素按,否則對 page 按(全域鍵盤)。"""
    key = step.params.get("key")
    if not key:
        return ActionResult(ok=False, error="web.press 缺少 params.key")
    if step.target:
        _target(ctx, step).press(key, timeout=_timeout(step))
    else:
        _page(ctx).keyboard.press(key)
    return ActionResult(ok=True, value=key)


# --------------------------------------------------------------------------- #
# 等待
# --------------------------------------------------------------------------- #
@action("web.wait")
def web_wait(ctx, step) -> ActionResult:
    """固定秒數等待;以 0.1s 為粒度輪詢 should_stop() 以支援中斷。"""
    seconds = float(step.params.get("seconds", 0))
    deadline = time.time() + seconds
    while time.time() < deadline:
        if ctx.should_stop():
            return ActionResult(ok=False, error="stopped")
        time.sleep(min(0.1, max(0.0, deadline - time.time())))
    return ActionResult(ok=True, value=seconds)


@action("web.wait_for")
def web_wait_for(ctx, step) -> ActionResult:
    """等待 target 出現(visible 即可)。"""
    loc = _target(ctx, step)
    loc.wait_for(state="visible", timeout=_timeout(step))
    return ActionResult(ok=True)


# --------------------------------------------------------------------------- #
# 擷取
# --------------------------------------------------------------------------- #
@action("web.scrape_field")
def web_scrape_field(ctx, step) -> ActionResult:
    """抓單一元素文字,存進 ctx.vars[params.var]。"""
    var = step.params.get("var")
    if not var:
        return ActionResult(ok=False, error="web.scrape_field 缺少 params.var")
    loc = _target(ctx, step)
    loc.wait_for(state="visible", timeout=_timeout(step))
    text = (loc.first.inner_text() or "").strip()
    ctx.vars.set(var, text)
    return ActionResult(ok=True, value=text)


@action("web.scrape_table")
def web_scrape_table(ctx, step) -> ActionResult:
    """抓 <table> 成 list[dict],存進 ctx.vars[params.var]。

    定位來源:優先 step.target(locator),否則 params.selector(CSS)。
    取 thead/第一列為表頭,其餘列為資料;無表頭時用 col_0, col_1, ...。
    """
    var = step.params.get("var")
    if not var:
        return ActionResult(ok=False, error="web.scrape_table 缺少 params.var")

    page = _page(ctx)
    if step.target:
        table = _target(ctx, step).first
    else:
        selector = step.params.get("selector")
        if not selector:
            return ActionResult(ok=False, error="web.scrape_table 需要 target 或 params.selector")
        table = page.locator(selector).first

    table.wait_for(state="visible", timeout=_timeout(step))

    # 表頭:優先 thead th,否則第一個 tr 的 th/td
    headers = table.locator("thead th").all_inner_texts()
    if not headers:
        all_tr = table.locator("tr")
        if all_tr.count() > 0:
            headers = all_tr.first.locator("th, td").all_inner_texts()
    headers = [h.strip() for h in headers]

    data: list[dict] = []
    # 重新以「所有 tr」掃描,跳過與表頭相同的首列(若無 thead)
    has_thead = table.locator("thead").count() > 0
    if has_thead:
        body_rows = table.locator("tbody tr")
        n = body_rows.count()
        tr_iter = [body_rows.nth(i) for i in range(n)]
    else:
        all_tr = table.locator("tr")
        n = all_tr.count()
        tr_iter = [all_tr.nth(i) for i in range(1, n)]  # 跳過表頭列

    for tr in tr_iter:
        if ctx.should_stop():
            return ActionResult(ok=False, error="stopped")
        cells = [c.strip() for c in tr.locator("th, td").all_inner_texts()]
        if not cells:
            continue
        if headers and len(headers) == len(cells):
            data.append(dict(zip(headers, cells)))
        else:
            data.append({f"col_{i}": v for i, v in enumerate(cells)})

    ctx.vars.set(var, data)
    return ActionResult(ok=True, value=data)


# --------------------------------------------------------------------------- #
# 下載 / 截圖
# --------------------------------------------------------------------------- #
@action("web.download")
def web_download(ctx, step) -> ActionResult:
    """點擊 target 觸發下載,等下載完成,存檔並把路徑寫進 ctx.vars[params.var]。

    需 BrowserContext accept_downloads=True(WebSession 已設)。
    落地目錄取自 page._rpa_download_dir(WebSession 注入);檔名用 download.suggested_filename。
    """
    import os

    page = _page(ctx)
    var = step.params.get("var")
    download_dir = getattr(page, "_rpa_download_dir", "logs/downloads")
    os.makedirs(download_dir, exist_ok=True)

    timeout = _timeout(step)
    with page.expect_download(timeout=timeout) as dl_info:
        loc = _target(ctx, step)
        loc.click(timeout=timeout)
    download = dl_info.value

    suggested = download.suggested_filename or "download.bin"
    save_path = os.path.join(download_dir, suggested)
    download.save_as(save_path)

    if var:
        ctx.vars.set(var, save_path)
    return ActionResult(ok=True, value=save_path)


@action("web.screenshot")
def web_screenshot(ctx, step) -> ActionResult:
    """整頁截圖到 params.path(預設落在 screenshot_dir)。"""
    import os

    path = step.params.get("path")
    if not path:
        os.makedirs(ctx.screenshot_dir, exist_ok=True)
        path = os.path.join(ctx.screenshot_dir, f"shot_{step.id}.png")
    else:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    _page(ctx).screenshot(path=path, full_page=True)
    return ActionResult(ok=True, value=path)
