# -*- coding: utf-8 -*-
"""流程清單頁:列出 Store 內 flows、從 flows/*.json 載入範例、檢視某條 flow 的 steps。"""
from __future__ import annotations
import os
import glob

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
    QMessageBox,
)

from core.schema import Flow
from ui.widgets import page_header, Card

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FLOWS_DIR = os.path.join(_ROOT, "flows")


def _target_summary(target: dict | None) -> str:
    if not target:
        return ""
    primary = (target or {}).get("primary") or {}
    strat = primary.get("strategy", "")
    val = primary.get("value", "")
    return f"{strat}={val}" if strat else str(val)


class FlowsPage(QWidget):
    """流程清單。選一條會在右側顯示其 steps 摘要表。"""

    flows_changed = Signal()   # 載入範例後通知其他頁(例 RunPage)刷新下拉

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, title, hint = page_header(
            "流程清單",
            "管理已存的自動化流程;可從 flows/ 載入範例,點選查看步驟。")
        root.addWidget(header)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_load_samples = QPushButton("📥  載入範例")
        self.btn_refresh = QPushButton("↻  重新整理")
        self.btn_refresh.setObjectName("Ghost")
        bar.addWidget(self.btn_load_samples)
        bar.addWidget(self.btn_refresh)
        bar.addStretch(1)
        root.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(14)

        left_card = Card(margins=(10, 10, 10, 10))
        list_label = QLabel("流程")
        list_label.setObjectName("SectionLabel")
        self.list = QListWidget()
        self.list.setMinimumWidth(220)
        left_card.body.addWidget(list_label)
        left_card.body.addWidget(self.list, 1)
        split.addWidget(left_card)

        right = Card()
        self.detail_title = QLabel("（未選擇流程）")
        self.detail_title.setObjectName("SectionLabel")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "action", "label", "target"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        right.body.addWidget(self.detail_title)
        right.body.addWidget(self.table, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 640])
        root.addWidget(split, 1)

        self.btn_load_samples.clicked.connect(self.load_samples)
        self.btn_refresh.clicked.connect(self.refresh)
        self.list.currentItemChanged.connect(self._on_select)

        self.refresh()

    # ---- 資料 ---- #
    def refresh(self):
        self.list.clear()
        for row in self.store.list_flows():
            item = QListWidgetItem(f"{row['name']}  ·  {row.get('engine', '')}")
            item.setData(Qt.UserRole, row["name"])
            self.list.addItem(item)

    def load_samples(self):
        files = sorted(glob.glob(os.path.join(_FLOWS_DIR, "*.json")))
        if not files:
            QMessageBox.information(self, "載入範例",
                                    f"flows/ 內找不到 *.json 範例。\n({_FLOWS_DIR})")
            return
        loaded = 0
        errors = []
        for path in files:
            try:
                flow = Flow.load(path)
                self.store.save_flow(flow.to_dict())
                loaded += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{os.path.basename(path)}: {e}")
        self.refresh()
        self.flows_changed.emit()
        msg = f"已載入 {loaded} 條範例流程。"
        if errors:
            msg += "\n\n下列檔案載入失敗:\n" + "\n".join(errors)
        QMessageBox.information(self, "載入範例", msg)

    def _on_select(self, cur, _prev):
        if cur is None:
            return
        name = cur.data(Qt.UserRole)
        d = self.store.load_flow(name)
        if not d:
            return
        flow = Flow.from_dict(d)
        self.detail_title.setText(f"{flow.name}（engine={flow.engine}, {len(flow.steps)} 步）")
        self.table.setRowCount(len(flow.steps))
        for i, step in enumerate(flow.steps):
            self.table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.table.setItem(i, 1, QTableWidgetItem(step.action))
            self.table.setItem(i, 2, QTableWidgetItem(step.label))
            self.table.setItem(i, 3, QTableWidgetItem(_target_summary(step.target)))
