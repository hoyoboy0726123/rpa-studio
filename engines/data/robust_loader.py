# -*- coding: utf-8 -*-
"""robust_loader — 容錯讀表。

企業內部從各系統匯出的 CSV / Excel 常有以下髒污,這裡一次處理乾淨:

  1. 編碼亂:CSV 可能是 utf-8 / utf-8-sig(含 BOM) / cp950 / big5 / gbk。
  2. 前置雜訊列:報表前幾列是標題 / 空白,真正表頭在中間 → 自動偵測並跳過。
  3. 欄名髒:全形括號（）、前後空白、BOM、換行、連續空白 → 正規化。
  4. 數值髒:千分位 1,234、會計負數 (1,234)、貨幣符號、百分比、N/A / - / NaN。
  5. 日期髒:民國年 (113/01/05、民國113年1月5日)、中文年月日、英文月份 → ISO。

對外只暴露 `read_table(path, sheet=None) -> list[dict]`,值已正規化:
  - 數值欄回 float / int
  - 日期欄回 'YYYY-MM-DD' 字串(無法解析者保留原字串)
  - 缺值回 None

純函式、不依賴 ctx;actions.py 會包成 data.read_table 步驟。
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import unicodedata

import pandas as pd

# 嘗試的編碼順序(utf-8-sig 先吃掉 BOM,再退 cp950/big5/gbk)
_ENCODINGS = ("utf-8-sig", "utf-8", "cp950", "big5", "gbk", "latin-1")

# 視為缺值的字串(大小寫不敏感、去空白後比對)
_NA_TOKENS = {
    "", "-", "--", "—", "n/a", "na", "n.a.", "n-a", "n_a", "null", "none",
    "nil", "nan", "#n/a", "#na", "#value!", "#ref!", "(空白)", "無", "(null)",
}

# 千分位 / 貨幣 / 百分比 數值樣式
_NUM_RE = re.compile(
    r"""^[\s]*                         # 前置空白
        (?P<paren>\()?                 # 會計負數左括號
        [\s]*
        (?P<sign>[+\-])?               # 一般正負號
        [\$＄¥￥€£NT\s]*               # 貨幣符號(含全形)
        (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?   # 千分位整數(可帶小數)
              |\d+(?:\.\d+)?            # 一般數字
              |\.\d+)                  # .5 這種
        \s*(?P<pct>%)?                 # 百分比
        \s*\)?                         # 會計負數右括號
        \s*$""",
    re.VERBOSE,
)

# 民國年:113/1/5、113-01-05、民國113年1月5日、113年1月
_ROC_SLASH_RE = re.compile(r"^\s*(?P<y>\d{2,3})[/\-.](?P<m>\d{1,2})(?:[/\-.](?P<d>\d{1,2}))?\s*$")
_ROC_CJK_RE = re.compile(
    r"^\s*(?:民國)?\s*(?P<y>\d{2,3})\s*年\s*(?P<m>\d{1,2})\s*月"
    r"(?:\s*(?P<d>\d{1,2})\s*日?)?\s*$"
)
# 西元中文:2024年1月5日 / 2024年1月
_CJK_DATE_RE = re.compile(
    r"^\s*(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月"
    r"(?:\s*(?P<d>\d{1,2})\s*日?)?\s*$"
)
# 英文月份:Jan 5, 2024 / 5 Jan 2024 / January 2024
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_ENG_DATE_RE = re.compile(
    r"^\s*(?:(?P<d1>\d{1,2})\s+)?(?P<mon>[A-Za-z]{3,9})\.?\s+"
    r"(?:(?P<d2>\d{1,2})\s*,?\s*)?(?P<y>\d{4})\s*$"
)
# 純 ISO / 斜線西元:2024-01-05 / 2024/1/5
_ISO_RE = re.compile(r"^\s*(?P<y>\d{4})[/\-.](?P<m>\d{1,2})(?:[/\-.](?P<d>\d{1,2}))?\s*$")


# --------------------------------------------------------------------------- #
# 欄名正規化
# --------------------------------------------------------------------------- #
def normalize_header(name) -> str:
    """欄名正規化:去 BOM / 全形括號轉半形 / 壓縮空白 / 去頭尾空白 / 換行轉空白。"""
    if name is None:
        return ""
    s = str(name)
    s = s.replace("﻿", "")                      # BOM
    s = unicodedata.normalize("NFKC", s)             # 全形 → 半形(含（）()）
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


# --------------------------------------------------------------------------- #
# 數值正規化
# --------------------------------------------------------------------------- #
def normalize_number(value):
    """把字串數值正規化成 float / int;非數值回原值。

    支援:千分位、會計負數 (1,234)、前綴貨幣符號、百分比(轉成小數)。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return value
    s = str(value).strip()
    if s == "":
        return None
    m = _NUM_RE.match(s)
    if not m:
        return value
    num = m.group("num").replace(",", "")
    try:
        val = float(num)
    except ValueError:
        return value
    if m.group("paren") or m.group("sign") == "-":
        val = -val
    if m.group("pct"):
        val = val / 100.0
    # 整數值回 int(避免 113.0 這種)
    if val == int(val) and not m.group("pct"):
        return int(val)
    return val


# --------------------------------------------------------------------------- #
# 日期正規化
# --------------------------------------------------------------------------- #
def _safe_date(y: int, m: int, d: int) -> str | None:
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def normalize_date(value):
    """把民國年 / 中文年月 / 英文月份 / ISO 等格式統一成 'YYYY-MM-DD';失敗回原值。

    僅日(無日)的情況補成當月 1 日。民國年判定:1~3 位數年(<1000)視為民國 → +1911。
    """
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if s == "":
        return None

    # 民國 / 中文(有「民國」「年」字樣 → 一定是民國)
    m = _ROC_CJK_RE.match(s)
    if m:
        y = int(m.group("y")) + 1911
        mo = int(m.group("m"))
        d = int(m.group("d")) if m.group("d") else 1
        r = _safe_date(y, mo, d)
        if r:
            return r

    # 西元中文:2024年1月5日
    m = _CJK_DATE_RE.match(s)
    if m:
        d = int(m.group("d")) if m.group("d") else 1
        r = _safe_date(int(m.group("y")), int(m.group("m")), d)
        if r:
            return r

    # 英文月份
    m = _ENG_DATE_RE.match(s)
    if m:
        mon = _MONTHS.get(m.group("mon").lower())
        if mon:
            day = m.group("d1") or m.group("d2")
            d = int(day) if day else 1
            r = _safe_date(int(m.group("y")), mon, d)
            if r:
                return r

    # 純 ISO / 斜線:四位數年 → 西元
    m = _ISO_RE.match(s)
    if m:
        d = int(m.group("d")) if m.group("d") else 1
        r = _safe_date(int(m.group("y")), int(m.group("m")), d)
        if r:
            return r

    # 民國斜線:2~3 位數年 → 民國(113/1/5)
    m = _ROC_SLASH_RE.match(s)
    if m:
        y = int(m.group("y"))
        if y < 1000:
            y += 1911
        d = int(m.group("d")) if m.group("d") else 1
        r = _safe_date(y, int(m.group("m")), d)
        if r:
            return r

    return value


def _looks_like_date_col(values: list) -> bool:
    """欄內非空字串值多數(>=60%)能解析成日期 → 視為日期欄。"""
    samples = [v for v in values if isinstance(v, str) and v.strip() != ""]
    if len(samples) < 1:
        return False
    hit = sum(1 for v in samples if normalize_date(v) != v)
    return hit >= max(1, int(len(samples) * 0.6))


# --------------------------------------------------------------------------- #
# 值層級正規化
# --------------------------------------------------------------------------- #
def _normalize_cell(value):
    """單格:缺值 → None;否則先試數值,不是數值再原樣回(日期在欄層級處理)。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str):
        if value.strip().lower() in _NA_TOKENS:
            return None
        num = normalize_number(value)
        if not isinstance(num, str):           # 成功轉成數值
            return num
        return value.strip()
    return value


# --------------------------------------------------------------------------- #
# 前置空白列偵測
# --------------------------------------------------------------------------- #
def _detect_header_row(df_raw: pd.DataFrame) -> int:
    """在「無表頭」讀進來的 DataFrame 找出真正表頭所在列索引。

    啟發式:從上往下找第一列「非空儲存格 >= 2 且 >= 該列一半」的列當表頭。
    全空列(報表前置空白)會被跳過。找不到回 0。
    """
    n_cols = df_raw.shape[1]
    for i in range(min(len(df_raw), 30)):
        row = df_raw.iloc[i].tolist()
        non_empty = [c for c in row if not (c is None or (isinstance(c, float) and math.isnan(c))
                                            or str(c).strip() == "")]
        if len(non_empty) >= max(2, math.ceil(n_cols / 2)):
            return i
    return 0


def _frame_to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame → list[dict],套用欄名 / 數值 / 日期正規化。"""
    # 欄名正規化(去重:重名加 _2/_3)
    seen: dict[str, int] = {}
    new_cols = []
    for c in df.columns:
        name = normalize_header(c) or "col"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        new_cols.append(name)
    df = df.copy()
    df.columns = new_cols

    # 先做格層級正規化(缺值 / 數值)
    records: list[dict] = [
        {col: _normalize_cell(val) for col, val in zip(new_cols, row)}
        for row in df.itertuples(index=False, name=None)
    ]

    # 欄層級:整欄像日期才轉,避免把代號型字串誤判
    for col in new_cols:
        col_vals = [r[col] for r in records]
        if _looks_like_date_col(col_vals):
            for r in records:
                r[col] = normalize_date(r[col])
    return records


def read_table(path: str, sheet=None) -> list[dict]:
    """容錯讀表,回 list[dict](值已正規化)。

    path  : .csv / .tsv / .txt / .xlsx / .xls
    sheet : Excel 工作表名稱或索引(預設第一張);CSV 忽略此參數。
    """
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"read_table: 找不到檔案 {path}")

    ext = os.path.splitext(path)[1].lower()

    if ext in (".xlsx", ".xls", ".xlsm"):
        # 先無表頭讀,偵測前置空白列,再以該列為表頭重組
        raw = pd.read_excel(path, sheet_name=(0 if sheet is None else sheet), header=None)
        hdr = _detect_header_row(raw)
        df = pd.read_excel(path, sheet_name=(0 if sheet is None else sheet), header=hdr)
        return _frame_to_records(df)

    # CSV / TSV / TXT:多編碼嘗試。用 csv 模組逐列解析(容忍欄數不齊的雜訊列),
    # 自行偵測表頭列再組 DataFrame —— 比 pd.read_csv 對「前置雜訊 + ragged rows」更穩。
    import csv as _csv
    import io

    delimiter = "\t" if ext == ".tsv" else ","
    last_err: Exception | None = None
    text: str | None = None
    for enc in _ENCODINGS:
        try:
            with open(path, "r", encoding=enc, newline="") as fh:
                text = fh.read()
            break
        except UnicodeDecodeError as e:
            last_err = e
            continue
    if text is None:
        raise ValueError(f"read_table: 無法以任何編碼讀取 {path}:{last_err}")

    rows = list(_csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return []
    # 對齊成等寬矩陣(取最長列寬)以利表頭偵測
    width = max(len(r) for r in rows)
    grid = [r + [""] * (width - len(r)) for r in rows]
    raw_df = pd.DataFrame(grid)
    hdr = _detect_header_row(raw_df)

    header_cells = grid[hdr]
    # 表頭右側的空欄位不算欄(避免雜訊列把寬度撐大)
    n_real = len(header_cells)
    while n_real > 1 and str(header_cells[n_real - 1]).strip() == "":
        n_real -= 1
    columns = header_cells[:n_real]

    data_rows = []
    for r in grid[hdr + 1:]:
        cells = r[:n_real] + [""] * max(0, n_real - len(r))
        if all(str(c).strip() == "" for c in cells):     # 跳過全空列
            continue
        data_rows.append(cells[:n_real])

    df = pd.DataFrame(data_rows, columns=columns)
    return _frame_to_records(df)
