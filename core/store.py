# -*- coding: utf-8 -*-
"""SQLite 持久層:flows / runs / step_logs。截圖存檔案、DB 存路徑。
用 sqlite3 stdlib(輕量,免額外依賴);可被 Pandas 讀出做執行報表。
"""
from __future__ import annotations
import sqlite3
import json
import os
import datetime as dt


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, db_path: str = "rpa_studio.db"):
        self.db_path = db_path
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS flows(
                    name TEXT PRIMARY KEY,
                    engine TEXT,
                    json TEXT,
                    updated TEXT
                );
                CREATE TABLE IF NOT EXISTS runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    flow TEXT,
                    started TEXT,
                    finished TEXT,
                    status TEXT,            -- running|completed|stopped|failed
                    vars_json TEXT
                );
                CREATE TABLE IF NOT EXISTS step_logs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    step_id TEXT,
                    action TEXT,
                    status TEXT,            -- ok|failed|skipped
                    ms INTEGER,
                    retries INTEGER,
                    error TEXT,
                    screenshot TEXT,
                    ts TEXT
                );
                CREATE TABLE IF NOT EXISTS heal_logs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    step_id TEXT,
                    strategy_used TEXT,     -- 例:heal(web) / heal(desktop)
                    score REAL,             -- 候選相似度分數(0~1)
                    detail TEXT,            -- 命中候選描述 / 評分明細(JSON 字串)
                    ts TEXT
                );
                """
            )

    # ---- flows ---- #
    def save_flow(self, flow_dict: dict):
        with self._conn() as c:
            c.execute(
                "INSERT INTO flows(name,engine,json,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET engine=excluded.engine, json=excluded.json, updated=excluded.updated",
                (flow_dict.get("name"), flow_dict.get("engine", "web"),
                 json.dumps(flow_dict, ensure_ascii=False), _now()),
            )

    def load_flow(self, name: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT json FROM flows WHERE name=?", (name,)).fetchone()
            return json.loads(r["json"]) if r else None

    def list_flows(self) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT name,engine,updated FROM flows ORDER BY updated DESC").fetchall()]

    # ---- runs ---- #
    def start_run(self, flow_name: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs(flow,started,status) VALUES(?,?,?)",
                (flow_name, _now(), "running"))
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str, variables: dict | None = None):
        with self._conn() as c:
            c.execute("UPDATE runs SET finished=?, status=?, vars_json=? WHERE id=?",
                      (_now(), status, json.dumps(variables or {}, ensure_ascii=False), run_id))

    def list_runs(self, limit: int = 200) -> list:
        """列出 runs(新到舊),供執行報表 / 稽核 UI。

        只回基本欄位(id/flow/started/finished/status),不含 vars_json;
        純唯讀查詢,不動既有 schema。
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, flow, started, finished, status "
                "FROM runs ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(r) for r in rows]

    def log_step(self, run_id: int, step_id: str, action: str, status: str,
                 ms: int = 0, retries: int = 0, error: str = "", screenshot: str = ""):
        with self._conn() as c:
            c.execute(
                "INSERT INTO step_logs(run_id,step_id,action,status,ms,retries,error,screenshot,ts) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, step_id, action, status, ms, retries, error, screenshot, _now()))

    # ---- heal logs ---- #
    def log_heal(self, run_id, step_id: str, strategy_used: str,
                 score: float = 0.0, detail: object = ""):
        """記一筆自癒(self-healing)事件,供人審核。

        detail 可給字串或 dict;dict 會被序列化成 JSON 字串保存。
        刻意「只記 log、不改 flow 檔」——自癒只在本次執行替換定位器,
        是否把替換結果寫回 flow 由人審核後決定。
        """
        if not isinstance(detail, str):
            try:
                detail = json.dumps(detail, ensure_ascii=False)
            except Exception:
                detail = str(detail)
        with self._conn() as c:
            c.execute(
                "INSERT INTO heal_logs(run_id,step_id,strategy_used,score,detail,ts) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, step_id, strategy_used, float(score or 0.0), detail, _now()))

    def list_heals(self, run_id=None) -> list:
        """讀出自癒事件(供審核 UI / 報表)。run_id=None 取全部。"""
        with self._conn() as c:
            if run_id is None:
                rows = c.execute(
                    "SELECT * FROM heal_logs ORDER BY id DESC").fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM heal_logs WHERE run_id=? ORDER BY id", (run_id,)
                ).fetchall()
            return [dict(r) for r in rows]

    def run_report(self, run_id: int) -> dict:
        with self._conn() as c:
            run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            steps = c.execute("SELECT * FROM step_logs WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
            return {"run": dict(run) if run else {}, "steps": [dict(s) for s in steps]}
