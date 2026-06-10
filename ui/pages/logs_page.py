# -*- coding: utf-8 -*-
"""日誌頁:列最近 runs(Store),點選看該 run 的 step_logs。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView,
)

from ui.widgets import page_header, Card


class LogsPage(QWidget):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, title, hint = page_header(
            "執行日誌",
            "檢視最近的執行紀錄;點選一筆 run 可看其各步驟結果與截圖路徑。")
        root.addWidget(header)

        bar = QHBoxLayout()
        self.btn_refresh = QPushButton("↻  重新整理")
        self.btn_refresh.setObjectName("Ghost")
        bar.addStretch(1)
        bar.addWidget(self.btn_refresh)
        root.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        split.setHandleWidth(14)

        runs_card = Card(margins=(10, 10, 10, 10))
        runs_lbl = QLabel("最近執行")
        runs_lbl.setObjectName("SectionLabel")
        runs_card.body.addWidget(runs_lbl)
        self.runs = QTableWidget(0, 5)
        self.runs.setHorizontalHeaderLabels(["run id", "flow", "開始", "結束", "狀態"])
        self.runs.setAlternatingRowColors(True)
        self.runs.verticalHeader().setVisible(False)
        self.runs.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.runs.setEditTriggers(QTableWidget.NoEditTriggers)
        self.runs.setSelectionBehavior(QTableWidget.SelectRows)
        runs_card.body.addWidget(self.runs, 1)
        split.addWidget(runs_card)

        steps_card = Card(margins=(10, 10, 10, 10))
        steps_lbl = QLabel("步驟明細")
        steps_lbl.setObjectName("SectionLabel")
        steps_card.body.addWidget(steps_lbl)
        self.steps = QTableWidget(0, 6)
        self.steps.setHorizontalHeaderLabels(
            ["step", "action", "狀態", "ms", "錯誤", "截圖"])
        self.steps.setAlternatingRowColors(True)
        self.steps.verticalHeader().setVisible(False)
        self.steps.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.steps.setEditTriggers(QTableWidget.NoEditTriggers)
        steps_card.body.addWidget(self.steps, 1)
        split.addWidget(steps_card)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        self.btn_refresh.clicked.connect(self.refresh)
        self.runs.itemSelectionChanged.connect(self._on_run_selected)

        self.refresh()

    def _recent_runs(self, limit=50):
        # Store 沒有直接列 runs 的 API;用其連線讀(只讀 select,不改 schema)。
        try:
            with self.store._conn() as c:  # noqa: SLF001 — 唯讀查詢
                rows = c.execute(
                    "SELECT id, flow, started, finished, status "
                    "FROM runs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def refresh(self):
        rows = self._recent_runs()
        self.runs.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.runs.setItem(i, 0, QTableWidgetItem(str(r["id"])))
            self.runs.setItem(i, 1, QTableWidgetItem(r.get("flow") or ""))
            self.runs.setItem(i, 2, QTableWidgetItem(r.get("started") or ""))
            self.runs.setItem(i, 3, QTableWidgetItem(r.get("finished") or ""))
            self.runs.setItem(i, 4, QTableWidgetItem(r.get("status") or ""))
        self.steps.setRowCount(0)

    def _on_run_selected(self):
        items = self.runs.selectedItems()
        if not items:
            return
        run_id = int(self.runs.item(items[0].row(), 0).text())
        report = self.store.run_report(run_id)
        steps = report.get("steps", [])
        self.steps.setRowCount(len(steps))
        for i, s in enumerate(steps):
            self.steps.setItem(i, 0, QTableWidgetItem(s.get("step_id") or ""))
            self.steps.setItem(i, 1, QTableWidgetItem(s.get("action") or ""))
            self.steps.setItem(i, 2, QTableWidgetItem(s.get("status") or ""))
            self.steps.setItem(i, 3, QTableWidgetItem(str(s.get("ms") or 0)))
            self.steps.setItem(i, 4, QTableWidgetItem(s.get("error") or ""))
            self.steps.setItem(i, 5, QTableWidgetItem(s.get("screenshot") or ""))
