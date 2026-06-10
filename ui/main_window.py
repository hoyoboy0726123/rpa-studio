# -*- coding: utf-8 -*-
"""MainWindow:左側深色 sidebar 導覽 + 右側 QStackedWidget 五個頁面。

頁面:流程清單 / 執行 / 排程 / 憑證 / 日誌。
共用 Store / Vault 單例傳給各頁;與引擎完全解耦(僅在 RunWorker 內 lazy import)。
"""
from __future__ import annotations
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QStackedWidget, QLabel, QButtonGroup, QFrame,
)

from core.store import Store
from core.vault import Vault

from ui.pages.flows_page import FlowsPage
from ui.pages.editor_page import EditorPage
from ui.pages.run_page import RunPage
from ui.pages.schedule_page import SchedulePage
from ui.pages.vault_page import VaultPage
from ui.pages.logs_page import LogsPage
from ui.pages.record_page import RecordPage
from ui.pages.graph_page import GraphPage
from ui.pages.report_page import ReportPage

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MainWindow(QMainWindow):
    def __init__(self, store: Store | None = None, vault: Vault | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("RPA Studio — 桌面控制台")
        self.resize(1200, 780)
        self.setMinimumSize(1000, 640)

        # 共用持久層(預設落在專案根)
        self.store = store or Store(os.path.join(_ROOT, "rpa_studio.db"))
        self.vault = vault or Vault(_ROOT)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- sidebar ---- #
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sblay = QVBoxLayout(sidebar)
        sblay.setContentsMargins(0, 0, 0, 0)
        sblay.setSpacing(0)

        brand_box = QWidget()
        brand_box.setObjectName("SidebarBrand")
        brand_lay = QHBoxLayout(brand_box)
        brand_lay.setContentsMargins(0, 0, 0, 0)
        brand_lay.setSpacing(8)
        brand = QLabel("🤖  RPA Studio")
        brand.setObjectName("SidebarTitle")
        brand_lay.addWidget(brand)
        brand_lay.addStretch(1)
        sblay.addWidget(brand_box)

        sub = QLabel("桌面自動化控制台")
        sub.setObjectName("SidebarSubtitle")
        sblay.addWidget(sub)

        sep = QFrame()
        sep.setObjectName("SidebarSep")
        sep.setFrameShape(QFrame.HLine)
        sblay.addWidget(sep)

        # ---- 頁面 ---- #
        self.stack = QStackedWidget()
        self.flows_page = FlowsPage(self.store)
        self.editor_page = EditorPage(self.store)
        self.run_page = RunPage(self.store, self.vault)
        self.schedule_page = SchedulePage(self.store)
        self.vault_page = VaultPage(self.vault)
        self.logs_page = LogsPage(self.store)
        self.record_page = RecordPage(self.store)
        self.graph_page = GraphPage(self.store)
        self.report_page = ReportPage(self.store)

        self.pages = [
            ("流程清單", self.flows_page),
            ("編輯器", self.editor_page),
            ("流程圖", self.graph_page),
            ("錄製", self.record_page),
            ("執行", self.run_page),
            ("排程", self.schedule_page),
            ("憑證", self.vault_page),
            ("日誌", self.logs_page),
            ("執行報表", self.report_page),
        ]

        # 導覽 icon(Unicode / emoji,免外部資源);僅影響按鈕顯示,不動 pages 標籤。
        nav_icons = {
            "流程清單": "📋",
            "編輯器": "✏️",
            "流程圖": "🔀",
            "錄製": "⏺",
            "執行": "▶",
            "排程": "🗓",
            "憑證": "🔑",
            "日誌": "📑",
            "執行報表": "📊",
        }

        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)
        for i, (label, page) in enumerate(self.pages):
            self.stack.addWidget(page)
            icon = nav_icons.get(label, "•")
            btn = QPushButton(f"  {icon}   {label}")
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _checked, idx=i: self._goto(idx))
            self._btn_group.addButton(btn, i)
            sblay.addWidget(btn)
        sblay.addStretch(1)

        footer = QLabel("v1.0 · 本機單機版")
        footer.setObjectName("SidebarFooter")
        sblay.addWidget(footer)

        outer.addWidget(sidebar)
        outer.addWidget(self.stack, 1)

        # 載入範例後刷新依賴流程清單的頁
        self.flows_page.flows_changed.connect(self.run_page.refresh)
        self.flows_page.flows_changed.connect(self.schedule_page.refresh)
        self.flows_page.flows_changed.connect(self.editor_page.refresh)
        # 編輯器存檔後也通知其他頁刷新
        self.editor_page.flows_changed.connect(self.flows_page.refresh)
        self.editor_page.flows_changed.connect(self.run_page.refresh)
        self.editor_page.flows_changed.connect(self.schedule_page.refresh)

        # 切到某些頁時刷新資料
        self.stack.currentChanged.connect(self._on_page_changed)

        # 預設第一頁
        self._btn_group.button(0).setChecked(True)
        self._goto(0)

        self._center_on_screen()

    def _center_on_screen(self):
        try:
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return
            geo = screen.availableGeometry()
            fr = self.frameGeometry()
            fr.moveCenter(geo.center())
            self.move(fr.topLeft())
        except Exception:
            pass

    def _goto(self, idx: int):
        self.stack.setCurrentIndex(idx)
        btn = self._btn_group.button(idx)
        if btn is not None:
            btn.setChecked(True)

    def _on_page_changed(self, idx: int):
        page = self.pages[idx][1]
        if hasattr(page, "refresh"):
            try:
                page.refresh()
            except Exception:
                pass
