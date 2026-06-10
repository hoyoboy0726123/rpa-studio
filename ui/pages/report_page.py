# -*- coding: utf-8 -*-
"""執行報表 / 稽核頁:檢視 runs / step_logs / heal_logs。

設計重點:
  - 上方 runs 清單(id / flow / 開始 / 結束 / 狀態;狀態用顏色徽章),可選 flow 篩選。
  - 選一個 run → 頂部摘要(總步數 / 成功 / 失敗 / 自癒次數)+ 下方兩區:
      a) step_logs:每步明細(含截圖「開啟」)。
      b) heal_logs:自癒猜測,明確標示「請審核是否回寫流程」。
  - 可把 step_logs 用 Pandas 匯出成 Excel(openpyxl)。

資料載入邏輯抽成模組級純函式(load_runs / load_run_detail / step_logs_dataframe /
export_steps_excel),不依賴 Qt,供測試直接呼叫斷言。
ReportPage(store) 可單獨建立(不需 vault / main_window)。
"""
from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QSplitter, QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QMessageBox,
)

from ui.widgets import page_header, Card


# ----------------------------------------------------------------------------
# 純資料層(無 Qt 依賴,供 UI 與測試共用)
# ----------------------------------------------------------------------------

# run.status -> (顯示文字, 徽章 objectName)
_RUN_STATUS = {
    "running": ("● 執行中", "StatusBadgeRunning"),
    "completed": ("✓ 完成", "StatusBadgeOk"),
    "failed": ("✕ 失敗", "StatusBadgeBad"),
    "stopped": ("■ 已停止", "StatusBadge"),
}


def run_status_display(status: str) -> tuple[str, str]:
    """把 run 狀態碼轉成 (顯示文字, 徽章 objectName)。未知狀態原樣顯示。"""
    return _RUN_STATUS.get(status or "", (status or "—", "StatusBadge"))


def load_runs(store, flow: str | None = None, limit: int = 200) -> list:
    """讀 runs 清單(新到舊),可選 flow 篩選。回 list[dict]。"""
    rows = store.list_runs(limit=limit)
    if flow:
        rows = [r for r in rows if (r.get("flow") or "") == flow]
    return rows


def list_run_flows(store, limit: int = 200) -> list:
    """收集出現過的 flow 名稱(去重、保序),供篩選下拉選單。"""
    seen: list[str] = []
    for r in store.list_runs(limit=limit):
        f = r.get("flow") or ""
        if f and f not in seen:
            seen.append(f)
    return seen


def summarize_steps(steps: list) -> dict:
    """彙總 step_logs:總步數 / 成功 / 失敗 / 略過。"""
    total = len(steps)
    ok = sum(1 for s in steps if (s.get("status") or "") == "ok")
    failed = sum(1 for s in steps if (s.get("status") or "") == "failed")
    skipped = sum(1 for s in steps if (s.get("status") or "") == "skipped")
    return {"total": total, "ok": ok, "failed": failed, "skipped": skipped}


def load_run_detail(store, run_id: int) -> dict:
    """讀一個 run 的完整明細:run / steps / heals / summary。

    回 dict:
      {"run": {...}, "steps": [...], "heals": [...],
       "summary": {"total","ok","failed","skipped","heals"}}
    """
    report = store.run_report(run_id)
    steps = report.get("steps", []) or []
    heals = store.list_heals(run_id) or []
    summary = summarize_steps(steps)
    summary["heals"] = len(heals)
    return {
        "run": report.get("run", {}) or {},
        "steps": steps,
        "heals": heals,
        "summary": summary,
    }


def step_logs_dataframe(steps: list):
    """把 step_logs 轉成 Pandas DataFrame(欄位順序固定,供匯出 Excel)。"""
    import pandas as pd

    cols = ["id", "run_id", "step_id", "action", "status",
            "ms", "retries", "error", "screenshot", "ts"]
    rows = [{c: s.get(c) for c in cols} for s in steps]
    return pd.DataFrame(rows, columns=cols)


def export_steps_excel(steps: list, path: str) -> str:
    """把 step_logs 匯出成 Excel(openpyxl 引擎)。回寫出的檔案路徑。"""
    df = step_logs_dataframe(steps)
    df.to_excel(path, index=False, engine="openpyxl", sheet_name="step_logs")
    return path


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

class ReportPage(QWidget):
    """執行報表 / 稽核頁。可 ReportPage(store) 單獨建立(供測試)。"""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._current_run_id: int | None = None
        self._current_steps: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, _title, _hint = page_header(
            "執行報表 / 稽核",
            "檢視每次執行的步驟結果與自癒(self-healing)紀錄;自癒為系統猜測,"
            "請人工審核是否回寫流程。")
        root.addWidget(header)

        # ---- 控制列:flow 篩選 + 重新整理 ---- #
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        flt_lbl = QLabel("flow 篩選:")
        flt_lbl.setObjectName("FieldLabel")
        self.combo_flow = QComboBox()
        self.combo_flow.addItem("（全部）", userData="")
        self.btn_refresh = QPushButton("↻  重新整理")
        self.btn_refresh.setObjectName("Ghost")
        ctrl.addWidget(flt_lbl)
        ctrl.addWidget(self.combo_flow, 1)
        ctrl.addWidget(self.btn_refresh)
        root.addLayout(ctrl)

        split = QSplitter(Qt.Vertical)
        split.setHandleWidth(14)

        # ---- runs 清單 ---- #
        runs_card = Card(margins=(10, 10, 10, 10))
        runs_lbl = QLabel("執行紀錄(runs)")
        runs_lbl.setObjectName("SectionLabel")
        runs_card.body.addWidget(runs_lbl)
        self.runs = QTableWidget(0, 5)
        self.runs.setHorizontalHeaderLabels(["run id", "flow", "開始", "結束", "狀態"])
        self.runs.setAlternatingRowColors(True)
        self.runs.verticalHeader().setVisible(False)
        self.runs.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.runs.setEditTriggers(QTableWidget.NoEditTriggers)
        self.runs.setSelectionBehavior(QTableWidget.SelectRows)
        self.runs.setSelectionMode(QTableWidget.SingleSelection)
        runs_card.body.addWidget(self.runs, 1)
        split.addWidget(runs_card)

        # ---- 明細區(摘要 + step_logs + heal_logs)---- #
        detail_card = Card(margins=(10, 10, 10, 10))

        # 摘要列
        summ = QHBoxLayout()
        summ.setSpacing(8)
        self.lbl_summary = QLabel("選一筆 run 以檢視明細。")
        self.lbl_summary.setObjectName("SectionLabel")
        summ.addWidget(self.lbl_summary, 1)
        self.btn_export = QPushButton("匯出 step_logs 為 Excel")
        self.btn_export.setObjectName("Ghost")
        self.btn_export.setEnabled(False)
        summ.addWidget(self.btn_export)
        detail_card.body.addLayout(summ)

        # step_logs
        steps_lbl = QLabel("步驟明細(step_logs)")
        steps_lbl.setObjectName("SectionLabel")
        detail_card.body.addWidget(steps_lbl)
        self.steps = QTableWidget(0, 8)
        self.steps.setHorizontalHeaderLabels(
            ["step", "action", "狀態", "耗時ms", "重試", "錯誤訊息", "截圖", ""])
        self.steps.setAlternatingRowColors(True)
        self.steps.verticalHeader().setVisible(False)
        self.steps.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.steps.setEditTriggers(QTableWidget.NoEditTriggers)
        detail_card.body.addWidget(self.steps, 2)

        # heal_logs(審核提示)
        heal_lbl = QLabel("自癒紀錄(heal_logs)— 系統猜測,請審核是否回寫流程")
        heal_lbl.setObjectName("SectionLabel")
        detail_card.body.addWidget(heal_lbl)
        self.heals = QTableWidget(0, 5)
        self.heals.setHorizontalHeaderLabels(
            ["step", "strategy_used", "score", "detail", "時間"])
        self.heals.setAlternatingRowColors(True)
        self.heals.verticalHeader().setVisible(False)
        self.heals.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.heals.setEditTriggers(QTableWidget.NoEditTriggers)
        detail_card.body.addWidget(self.heals, 1)

        split.addWidget(detail_card)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        self.btn_refresh.clicked.connect(self.refresh)
        self.combo_flow.currentIndexChanged.connect(self._reload_runs)
        self.runs.itemSelectionChanged.connect(self._on_run_selected)
        self.btn_export.clicked.connect(self._export_excel)

        self.refresh()

    # ---- runs ---- #
    def refresh(self):
        """重抓 flow 清單與 runs 表(保留目前 flow 篩選)。"""
        current = self.combo_flow.currentData()
        self.combo_flow.blockSignals(True)
        self.combo_flow.clear()
        self.combo_flow.addItem("（全部）", userData="")
        for f in list_run_flows(self.store):
            self.combo_flow.addItem(f, userData=f)
        idx = self.combo_flow.findData(current)
        if idx >= 0:
            self.combo_flow.setCurrentIndex(idx)
        self.combo_flow.blockSignals(False)
        self._reload_runs()

    def _selected_flow(self) -> str:
        data = self.combo_flow.currentData()
        return data or ""

    def _reload_runs(self):
        rows = load_runs(self.store, flow=self._selected_flow() or None)
        self.runs.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.runs.setItem(i, 0, QTableWidgetItem(str(r.get("id"))))
            self.runs.setItem(i, 1, QTableWidgetItem(r.get("flow") or ""))
            self.runs.setItem(i, 2, QTableWidgetItem(r.get("started") or ""))
            self.runs.setItem(i, 3, QTableWidgetItem(r.get("finished") or ""))
            text, _obj = run_status_display(r.get("status"))
            cell = QTableWidgetItem(text)
            cell.setData(Qt.UserRole, r.get("status") or "")
            self.runs.setItem(i, 4, cell)
        # runs 重載後清空明細
        self._clear_detail()

    def _clear_detail(self):
        self._current_run_id = None
        self._current_steps = []
        self.steps.setRowCount(0)
        self.heals.setRowCount(0)
        self.lbl_summary.setText("選一筆 run 以檢視明細。")
        self.btn_export.setEnabled(False)

    def _on_run_selected(self):
        items = self.runs.selectedItems()
        if not items:
            return
        row = items[0].row()
        id_item = self.runs.item(row, 0)
        if id_item is None:
            return
        try:
            run_id = int(id_item.text())
        except (TypeError, ValueError):
            return
        self.load_run(run_id)

    # ---- 明細載入(供測試直接呼叫)---- #
    def load_run(self, run_id: int) -> dict:
        """載入並填充某個 run 的摘要 / step_logs / heal_logs。回 detail dict。"""
        detail = load_run_detail(self.store, run_id)
        self._current_run_id = run_id
        self._current_steps = detail["steps"]
        self._fill_summary(run_id, detail["summary"])
        self._fill_steps(detail["steps"])
        self._fill_heals(detail["heals"])
        self.btn_export.setEnabled(len(detail["steps"]) > 0)
        return detail

    def _fill_summary(self, run_id: int, s: dict):
        self.lbl_summary.setText(
            f"Run #{run_id}　總步數 {s['total']}　"
            f"成功 {s['ok']}　失敗 {s['failed']}　自癒 {s['heals']} 次")

    def _fill_steps(self, steps: list):
        self.steps.setRowCount(len(steps))
        for i, s in enumerate(steps):
            self.steps.setItem(i, 0, QTableWidgetItem(s.get("step_id") or ""))
            self.steps.setItem(i, 1, QTableWidgetItem(s.get("action") or ""))
            self.steps.setItem(i, 2, QTableWidgetItem(s.get("status") or ""))
            self.steps.setItem(i, 3, QTableWidgetItem(str(s.get("ms") or 0)))
            self.steps.setItem(i, 4, QTableWidgetItem(str(s.get("retries") or 0)))
            self.steps.setItem(i, 5, QTableWidgetItem(s.get("error") or ""))
            shot = s.get("screenshot") or ""
            self.steps.setItem(i, 6, QTableWidgetItem(shot))
            if shot:
                btn = QPushButton("開啟")
                btn.setObjectName("Ghost")
                btn.clicked.connect(lambda _=False, p=shot: self._open_shot(p))
                self.steps.setCellWidget(i, 7, btn)
            else:
                self.steps.setCellWidget(i, 7, None)
                self.steps.setItem(i, 7, QTableWidgetItem(""))

    def _fill_heals(self, heals: list):
        self.heals.setRowCount(len(heals))
        for i, h in enumerate(heals):
            self.heals.setItem(i, 0, QTableWidgetItem(h.get("step_id") or ""))
            self.heals.setItem(i, 1, QTableWidgetItem(h.get("strategy_used") or ""))
            score = h.get("score")
            self.heals.setItem(
                i, 2, QTableWidgetItem("" if score is None else f"{float(score):.3f}"))
            self.heals.setItem(i, 3, QTableWidgetItem(h.get("detail") or ""))
            self.heals.setItem(i, 4, QTableWidgetItem(h.get("ts") or ""))

    # ---- 截圖開啟 ---- #
    def _open_shot(self, path: str):
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "截圖", f"找不到截圖檔:\n{path}")
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))
        except Exception:
            # 後援:平台原生開檔
            try:
                if sys.platform.startswith("win"):
                    os.startfile(path)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "截圖", f"無法開啟截圖:\n{e}")

    # ---- Excel 匯出 ---- #
    def _export_excel(self):
        if not self._current_steps:
            QMessageBox.information(self, "匯出", "目前沒有可匯出的步驟。")
            return
        default = f"run_{self._current_run_id}_steps.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self, "匯出 step_logs 為 Excel", default, "Excel (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            export_steps_excel(self._current_steps, path)
            QMessageBox.information(self, "匯出", f"已匯出:\n{path}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "匯出", f"匯出失敗:\n{e}")
