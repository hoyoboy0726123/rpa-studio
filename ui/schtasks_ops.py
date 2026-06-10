# -*- coding: utf-8 -*-
"""Windows 工作排程器 (schtasks) 整合 — 純邏輯 + subprocess 包裝。

把「組 schtasks 指令字串」「解析 schtasks /Query 列表輸出」抽成不依賴 Qt、
不需真的執行的純函式,讓 schedule_page 的 UI 只負責收表單、顯示結果,
真正組指令 / 解析的邏輯可被 tests 直接驗證。

任務內容固定為:用本機 python 跑 `run_cli.py --flow <name>`。
任務名稱統一加前綴 TASK_PREFIX,方便列出 / 篩選 RPA Studio 自己建立的任務。
"""
from __future__ import annotations
import csv
import io
import os
import subprocess
import sys

TASK_PREFIX = "RPAStudio_"

# 頻率代碼 -> (schtasks /SC 值, 中文說明)
FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
FREQ_MONTHLY = "monthly"

_WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def task_name_for(flow_name: str) -> str:
    """流程名稱 → 排程任務名稱(加前綴;清掉會干擾 schtasks 的字元)。"""
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in (flow_name or "flow"))
    return f"{TASK_PREFIX}{safe}"


def run_command(python_exe: str | None = None, cli_path: str | None = None,
                flow_name: str = "", unattended: bool = True) -> str:
    """組要被排程執行的指令字串:
        "<python>" "<run_cli.py>" --flow "<name>" --unattended

    排程是無人值守情境,預設帶 --unattended:讓 flow.pause_for_human 不等人、立即繼續,
    排程任務才不會卡在 MFA / 人工暫停而逾時掛死。"""
    py = python_exe or sys.executable or "python"
    cli = cli_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_cli.py")
    cmd = f'"{py}" "{cli}" --flow "{flow_name}"'
    if unattended:
        cmd += " --unattended"
    return cmd


def build_create_args(flow_name: str, freq: str, time_s: str = "08:00",
                      weekday: str = "MON", day: int = 1,
                      python_exe: str | None = None, cli_path: str | None = None,
                      task_name: str | None = None) -> list[str]:
    """組 schtasks /Create 的 argv(list 形式,供 subprocess 直接用,免 shell 跳脫雷)。

    freq: FREQ_DAILY / FREQ_WEEKLY / FREQ_MONTHLY。
    回傳的 list 第一個元素為 "schtasks"。
    """
    tn = task_name or task_name_for(flow_name)
    tr = run_command(python_exe, cli_path, flow_name)
    args = ["schtasks", "/Create", "/F", "/TN", tn, "/TR", tr]

    f = (freq or "").lower()
    if f == FREQ_DAILY:
        args += ["/SC", "DAILY", "/ST", time_s]
    elif f == FREQ_WEEKLY:
        wd = weekday if weekday in _WEEKDAYS else "MON"
        args += ["/SC", "WEEKLY", "/D", wd, "/ST", time_s]
    elif f == FREQ_MONTHLY:
        d = max(1, min(31, int(day or 1)))
        args += ["/SC", "MONTHLY", "/D", str(d), "/ST", time_s]
    else:
        raise ValueError(f"未知頻率: {freq}")
    return args


def build_create_command(flow_name: str, freq: str, time_s: str = "08:00",
                         weekday: str = "MON", day: int = 1,
                         python_exe: str | None = None,
                         cli_path: str | None = None,
                         task_name: str | None = None) -> str:
    """組可貼到 CMD 的 schtasks /Create 指令字串(供顯示 / 複製)。"""
    args = build_create_args(flow_name, freq, time_s, weekday, day,
                             python_exe, cli_path, task_name)
    return _join_for_display(args)


def build_delete_args(task_name: str) -> list[str]:
    return ["schtasks", "/Delete", "/F", "/TN", task_name]


def build_query_args(task_name: str | None = None) -> list[str]:
    """組 schtasks /Query argv;CSV 格式 + /V 取詳細欄位(含 Next Run Time)。"""
    args = ["schtasks", "/Query", "/FO", "CSV", "/V"]
    if task_name:
        args += ["/TN", task_name]
    return args


def _join_for_display(args: list[str]) -> str:
    """把 argv 串成可讀的一行指令(只給人看 / 複製,不用於實際執行)。"""
    out = []
    for a in args:
        out.append(f'"{a}"' if " " in a and not a.startswith('"') else a)
    return " ".join(out)


def parse_query_csv(csv_text: str, only_prefix: str | None = TASK_PREFIX) -> list[dict]:
    """解析 schtasks /Query /FO CSV /V 的輸出,回傳任務 dict 清單。

    取常見欄位:TaskName / Next Run Time / Status / Schedule Type / Task To Run。
    only_prefix 有給時只回傳任務名(去掉開頭反斜線後)以該前綴開頭者。
    輸出可能含多段表頭(每台機器 / 多任務),逐列依欄名對應,容錯處理。
    """
    if not csv_text or not csv_text.strip():
        return []
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return []

    out: list[dict] = []
    header: list[str] | None = None
    for row in rows:
        if not row:
            continue
        # 重複出現的表頭列(schtasks 會在多任務間夾表頭)
        if row and row[0].strip().strip('"') == "TaskName":
            header = [c.strip() for c in row]
            continue
        if header is None:
            continue
        rec = {header[i]: row[i] for i in range(min(len(header), len(row)))}
        raw_name = rec.get("TaskName", "").strip()
        name = raw_name.lstrip("\\")
        if only_prefix and not name.startswith(only_prefix):
            continue
        out.append({
            "task_name": name,
            "next_run": rec.get("Next Run Time", "").strip(),
            "status": rec.get("Status", "").strip(),
            "schedule": rec.get("Schedule Type", "").strip(),
            "command": rec.get("Task To Run", "").strip(),
        })
    # 去重(同任務多 trigger 會出現多列)
    seen = set()
    uniq = []
    for r in out:
        if r["task_name"] in seen:
            continue
        seen.add(r["task_name"])
        uniq.append(r)
    return uniq


# --------------------------------------------------------------------------- #
# subprocess 包裝(實際呼叫;權限不足 / 非 Windows 時回傳友善結果)
# --------------------------------------------------------------------------- #
class SchtasksResult:
    def __init__(self, ok: bool, message: str, raw: str = ""):
        self.ok = ok
        self.message = message
        self.raw = raw


def _run(args: list[str], timeout: int = 30) -> SchtasksResult:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return SchtasksResult(False, "找不到 schtasks 指令(此功能僅支援 Windows)。")
    except subprocess.TimeoutExpired:
        return SchtasksResult(False, "schtasks 執行逾時。")
    except Exception as e:  # noqa: BLE001
        return SchtasksResult(False, f"執行 schtasks 失敗:{type(e).__name__}: {e}")

    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return SchtasksResult(True, (proc.stdout or "").strip() or "完成。", out)
    # 常見:存取被拒(需系統管理員)
    low = out.lower()
    if "access is denied" in low or "拒絕存取" in out or "存取被拒" in out:
        msg = "建立 / 刪除排程任務需要系統管理員權限。請以「系統管理員」身分啟動 RPA Studio 後再試。"
    else:
        msg = (proc.stderr or proc.stdout or "schtasks 回報錯誤。").strip()
    return SchtasksResult(False, msg, out)


def create_task(flow_name: str, freq: str, time_s: str = "08:00",
                weekday: str = "MON", day: int = 1,
                python_exe: str | None = None,
                cli_path: str | None = None) -> SchtasksResult:
    try:
        args = build_create_args(flow_name, freq, time_s, weekday, day,
                                 python_exe, cli_path)
    except ValueError as e:
        return SchtasksResult(False, str(e))
    return _run(args)


def delete_task(task_name: str) -> SchtasksResult:
    return _run(build_delete_args(task_name))


def list_tasks(only_prefix: str | None = TASK_PREFIX) -> tuple[list[dict], SchtasksResult]:
    """列出 RPA 相關排程任務。回傳 (任務清單, 原始呼叫結果)。"""
    res = _run(build_query_args())
    if not res.ok:
        # 沒有任務時 schtasks 也可能回非 0;視為空清單但仍回報訊息。
        return [], res
    tasks = parse_query_csv(res.raw or res.message, only_prefix=only_prefix)
    return tasks, res
