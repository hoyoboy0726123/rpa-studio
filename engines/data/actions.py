# -*- coding: utf-8 -*-
"""data.* / excel.* actions — 資料 / Excel 動作組。

每個動作簽名 `def fn(ctx, step) -> ActionResult`,不需要 ctx.engine(純檔案 I/O)。
runner 已負責 {var} 替換 / retry / log / on_error;這裡只把單一動作做對。

「抓 → 填 Excel」接法(核心):
    1. web.scrape_table 把表格抓成 list[dict] 存進 ctx.vars['report_rows']。
    2. data.write_excel 的 params.var 給 'report_rows' → 讀出該變數的 list[dict] 寫成 xlsx。
  兩者的資料型別都是 list[dict],天然對得上,中間不需要轉換。

變數慣例:
  - 讀資料的 action(write_excel 的 var)→ 先當「變數名」查 ctx.vars;
    查不到再當「字面值」(允許直接傳 list[dict])。
  - 產出結果可寫回 params.result_var(與專案既有慣例一致)。
"""
from __future__ import annotations

from core.registry import action, ActionResult

from . import excel_ops
from .robust_loader import read_table


def _resolve_data(ctx, ref):
    """把 params.var 解析成實際資料。

    ref 為字串 → 先查變數;查不到當字面值(可能本身就是 JSON 字串,交給 to_records 處理)。
    ref 已是 list/dict/DataFrame → 直接用。
    """
    if isinstance(ref, str):
        val = ctx.vars.get(ref, None)
        if val is not None:
            return val
        return ref            # 查無此變數 → 當字面值(通常會在 to_records 報錯,訊息夠清楚)
    return ref


def _set_result(ctx, step, value):
    """若有 params.result_var,把結果寫回變數。"""
    rv = (step.params or {}).get("result_var")
    if rv:
        ctx.vars.set(rv, value)


# --------------------------------------------------------------------------- #
# data.read_table — 容錯讀進變數
# --------------------------------------------------------------------------- #
@action("data.read_table")
def data_read_table(ctx, step) -> ActionResult:
    """容錯讀表進變數。params {path, sheet, var}。

    讀出的 list[dict] 存進 ctx.vars[var];亦回傳供 result/log。
    """
    p = step.params or {}
    path = p.get("path")
    if not path:
        return ActionResult(ok=False, error="data.read_table 缺少 params.path")
    var = p.get("var")
    if not var:
        return ActionResult(ok=False, error="data.read_table 缺少 params.var")
    sheet = p.get("sheet")
    try:
        records = read_table(path, sheet=sheet)
    except Exception as e:  # noqa: BLE001 — 讀檔錯誤回成 action 失敗,交給 runner on_error
        return ActionResult(ok=False, error=f"data.read_table 失敗: {type(e).__name__}: {e}")
    ctx.vars.set(var, records)
    return ActionResult(ok=True, value={"rows": len(records), "var": var})


# --------------------------------------------------------------------------- #
# data.write_excel — 把抓到的資料填入 Excel(核心)
# --------------------------------------------------------------------------- #
@action("data.write_excel")
def data_write_excel(ctx, step) -> ActionResult:
    """把資料(變數名或 list[dict])寫入 / 附加進 xlsx。

    params {var, path, sheet, mode}
      - var   : 資料來源。字串先當變數名查 ctx.vars(對應 scrape_table 的輸出);
                查不到當字面值。也可直接給 list[dict]。
      - path  : 輸出 xlsx 路徑(支援 {var}/{today} 等,runner 已替換)。
      - sheet : 工作表名稱(預設 Sheet1)。
      - mode  : overwrite(預設) | append。
    """
    p = step.params or {}
    path = p.get("path")
    if not path:
        return ActionResult(ok=False, error="data.write_excel 缺少 params.path")
    if "var" not in p:
        return ActionResult(ok=False, error="data.write_excel 缺少 params.var(資料來源)")
    data = _resolve_data(ctx, p.get("var"))
    sheet = p.get("sheet", "Sheet1")
    mode = p.get("mode", "overwrite")
    try:
        info = excel_ops.write_excel(data, path, sheet=sheet, mode=mode)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"data.write_excel 失敗: {type(e).__name__}: {e}")
    _set_result(ctx, step, info)
    return ActionResult(ok=True, value=info)


# --------------------------------------------------------------------------- #
# data.csv_append — 附加一列到 CSV
# --------------------------------------------------------------------------- #
@action("data.csv_append")
def data_csv_append(ctx, step) -> ActionResult:
    """附加一列(dict)到 CSV。params {path, row, include_header}。"""
    p = step.params or {}
    path = p.get("path")
    if not path:
        return ActionResult(ok=False, error="data.csv_append 缺少 params.path")
    row = p.get("row")
    if not isinstance(row, dict):
        return ActionResult(ok=False, error="data.csv_append params.row 必須是 dict")
    include_header = bool(p.get("include_header", True))
    try:
        info = excel_ops.csv_append(path, row, include_header=include_header)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"data.csv_append 失敗: {type(e).__name__}: {e}")
    _set_result(ctx, step, info)
    return ActionResult(ok=True, value=info)


# --------------------------------------------------------------------------- #
# excel.split — 依某欄 group 拆檔
# --------------------------------------------------------------------------- #
@action("excel.split")
def excel_split(ctx, step) -> ActionResult:
    """依 group_col 把輸入檔拆成 N 個美化 xlsx + 索引檔。

    params {input_path, group_col, out_dir, prefix, sheet, index_var}
      - index_var : 把索引(list[dict])寫進此變數,供後續步驟使用。
    """
    p = step.params or {}
    input_path = p.get("input_path")
    group_col = p.get("group_col")
    out_dir = p.get("out_dir")
    if not input_path or not group_col or not out_dir:
        return ActionResult(ok=False,
                            error="excel.split 需要 input_path / group_col / out_dir")
    prefix = p.get("prefix", "")
    sheet = p.get("sheet")
    try:
        index = excel_ops.split_by_column(input_path, group_col, out_dir,
                                          prefix=prefix, sheet=sheet)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"excel.split 失敗: {type(e).__name__}: {e}")
    index_var = p.get("index_var")
    if index_var:
        ctx.vars.set(index_var, index)
    _set_result(ctx, step, index)
    return ActionResult(ok=True, value={"files": len(index), "index": index})


# --------------------------------------------------------------------------- #
# excel.split_rules — 規則拆檔
# --------------------------------------------------------------------------- #
@action("excel.split_rules")
def excel_split_rules(ctx, step) -> ActionResult:
    """依規則拆檔,檔名套模板。

    params {input_path, sheet, rules, out_dir, filename_template, data_yyyymm, index_var}
      - rules: [{name, column, match_type(all/exact/prefix/suffix/contains/regex/range), pattern}]
      - filename_template: 支援 {data_yyyymm}/{category}/{run_yyyymmdd}
    """
    p = step.params or {}
    input_path = p.get("input_path")
    out_dir = p.get("out_dir")
    rules = p.get("rules")
    if not input_path or not out_dir or not rules:
        return ActionResult(ok=False,
                            error="excel.split_rules 需要 input_path / out_dir / rules")
    if not isinstance(rules, list):
        return ActionResult(ok=False, error="excel.split_rules params.rules 必須是 list")
    sheet = p.get("sheet")
    template = p.get("filename_template", "{category}_{run_yyyymmdd}.xlsx")
    data_yyyymm = p.get("data_yyyymm")
    # runner 會把 template 內的 {data_yyyymm}(VarStore 內建 placeholder = 當月-1)
    # 先替換掉;若使用者另以 params.data_yyyymm 指定資料月,優先採用 → 還原 token,
    # 交給 excel_ops._render_template 以使用者值重新填入。
    if data_yyyymm is not None:
        import datetime as _dt
        _first = _dt.datetime.now().replace(day=1)
        _builtin_ym = (_first - _dt.timedelta(days=1)).strftime("%Y%m")
        if isinstance(template, str) and _builtin_ym in template:
            template = template.replace(_builtin_ym, "{data_yyyymm}")
    try:
        index = excel_ops.split_by_rules(input_path, rules, out_dir,
                                         filename_template=template, sheet=sheet,
                                         data_yyyymm=data_yyyymm)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"excel.split_rules 失敗: {type(e).__name__}: {e}")
    index_var = p.get("index_var")
    if index_var:
        ctx.vars.set(index_var, index)
    _set_result(ctx, step, index)
    return ActionResult(ok=True, value={"files": len(index), "index": index})


# --------------------------------------------------------------------------- #
# excel.diff — 兩檔比對,產色標報告
# --------------------------------------------------------------------------- #
@action("excel.diff")
def excel_diff(ctx, step) -> ActionResult:
    """兩檔比對,標 新增/消失/異動(±threshold%),產色標報告 xlsx。

    params {prev_path, curr_path, key_cols, value_col, out_path, threshold_pct, sheet}
      - key_cols : list[str](對齊主鍵)。
      - 色標:綠=新增、紅=消失、藍=上升、橙=下降。
    統計寫回 result_var(若有)。
    """
    p = step.params or {}
    prev_path = p.get("prev_path")
    curr_path = p.get("curr_path")
    key_cols = p.get("key_cols")
    value_col = p.get("value_col")
    out_path = p.get("out_path")
    if not (prev_path and curr_path and key_cols and value_col and out_path):
        return ActionResult(
            ok=False,
            error="excel.diff 需要 prev_path / curr_path / key_cols / value_col / out_path")
    if isinstance(key_cols, str):
        key_cols = [key_cols]
    try:
        threshold = float(p.get("threshold_pct", 0) or 0)
    except (TypeError, ValueError):
        threshold = 0.0
    sheet = p.get("sheet")
    try:
        stats = excel_ops.diff_files(prev_path, curr_path, key_cols, value_col,
                                     out_path, threshold_pct=threshold, sheet=sheet)
    except Exception as e:  # noqa: BLE001
        return ActionResult(ok=False, error=f"excel.diff 失敗: {type(e).__name__}: {e}")
    _set_result(ctx, step, stats)
    return ActionResult(ok=True, value=stats)
