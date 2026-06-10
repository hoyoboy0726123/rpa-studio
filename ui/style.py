# -*- coding: utf-8 -*-
"""全域 QSS 主題:現代專業風格 — 乾淨淺色內容區 + 深色 sidebar。

設計目標:給非工程使用者一個清楚、現代、好看好懂的桌面控制台。

調色盤(集中管理,QSS 內以實際色碼展開):
  主色 PRIMARY        #2563eb  (藍,主要動作 / 高亮)
  主色深 PRIMARY_DARK #1d4ed8  (hover/pressed)
  次色 SECONDARY      #e2e8f0  (淺灰按鈕底)
  成功 SUCCESS        #16a34a  (綠,進度 / 完成)
  警告 WARNING        #f59e0b  (黃 / 橙,暫停 / MFA)
  危險 DANGER         #dc2626  (紅,刪除 / 停止 / 失敗)
  中性深 INK          #0f172a  (主要文字 / 標題)
  中性 TEXT           #1f2937  (內文)
  中性淡 MUTED        #64748b  (說明文字)
  邊框 BORDER         #e2e8f0  (卡片 / 表格細邊)
  內容底 CANVAS       #f1f5f9  (主視窗背景)
  卡片底 SURFACE      #ffffff  (卡片 / 輸入框)
  側欄底 SIDEBAR_BG   #0f172a  (深藍灰)

字型:介面用微軟正黑體 / Segoe UI(無襯線);log / 程式碼區用 Consolas 等寬。
"""

# --- 對外可程式化引用的色票(供需要時用 setStyleSheet 局部覆寫)--- #
PRIMARY = "#2563eb"
PRIMARY_DARK = "#1d4ed8"
SUCCESS = "#16a34a"
WARNING = "#f59e0b"
DANGER = "#dc2626"
INK = "#0f172a"
MUTED = "#64748b"
BORDER = "#e2e8f0"
CANVAS = "#f1f5f9"
SURFACE = "#ffffff"
SIDEBAR_BG = "#0f172a"
ACCENT = PRIMARY  # 向後相容(舊程式可能 import ACCENT)

FONT_UI = '"Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI", "PingFang TC", sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New", monospace'

APP_QSS = """
/* ============================ 全域基底 ============================ */
QWidget {
    font-family: "Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI", "PingFang TC", sans-serif;
    font-size: 14px;
    color: #1f2937;
}
QMainWindow, QDialog { background: #f1f5f9; }

QToolTip {
    background: #0f172a;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px 8px;
}

/* ============================ 左側 Sidebar ============================ */
#Sidebar {
    background: #0f172a;
    min-width: 216px;
    max-width: 216px;
    border: none;
}
#SidebarBrand {
    background: transparent;
    padding: 22px 18px 2px 18px;
}
#SidebarTitle {
    color: #f8fafc;
    font-size: 19px;
    font-weight: 800;
    background: transparent;
    padding: 0;
}
#SidebarSubtitle {
    color: #94a3b8;
    font-size: 11px;
    background: transparent;
    padding: 2px 18px 14px 18px;
}
#SidebarSep {
    background: #1e293b;
    max-height: 1px;
    min-height: 1px;
    margin: 0 14px 8px 14px;
    border: none;
}
#NavButton {
    color: #cbd5e1;
    background: transparent;
    border: none;
    border-left: 3px solid transparent;
    text-align: left;
    padding: 11px 18px 11px 17px;
    margin: 1px 8px;
    border-radius: 8px;
    font-size: 14px;
}
#NavButton:hover {
    background: #1e293b;
    color: #ffffff;
}
#NavButton:checked {
    background: #1d4ed8;
    color: #ffffff;
    font-weight: 700;
    border-left: 3px solid #93c5fd;
}
#SidebarFooter {
    color: #475569;
    font-size: 10px;
    background: transparent;
    padding: 8px 18px 14px 18px;
}

/* ============================ 頁面 header ============================ */
#PageTitle {
    font-size: 23px;
    font-weight: 800;
    color: #0f172a;
    background: transparent;
}
#PageHint {
    color: #64748b;
    font-size: 12px;
    background: transparent;
}
#HeaderBar {
    background: transparent;
    border-bottom: 1px solid #e2e8f0;
}

/* 區塊小標題(放在卡片內或卡片上方) */
#SectionLabel {
    color: #334155;
    font-size: 13px;
    font-weight: 700;
    background: transparent;
}
#FieldLabel {
    color: #475569;
    background: transparent;
}

/* ============================ 卡片 ============================ */
#Card, QGroupBox {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
}
#Card { padding: 4px; }
QGroupBox {
    margin-top: 14px;
    padding: 16px 14px 14px 14px;
    font-weight: 700;
    color: #334155;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    top: 2px;
    padding: 0 6px;
    background: #ffffff;
    color: #1d4ed8;
}

/* ============================ 按鈕 ============================ */
QPushButton {
    background: #2563eb;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    padding: 8px 18px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton:hover { background: #1d4ed8; }
QPushButton:pressed { background: #1e40af; }
QPushButton:disabled { background: #cbd5e1; color: #f1f5f9; }

/* 次要 / Ghost 按鈕 */
QPushButton#Ghost {
    background: #ffffff;
    color: #334155;
    border: 1px solid #cbd5e1;
}
QPushButton#Ghost:hover { background: #f1f5f9; border-color: #94a3b8; }
QPushButton#Ghost:pressed { background: #e2e8f0; }
QPushButton#Ghost:disabled { background: #f8fafc; color: #cbd5e1; border-color: #e2e8f0; }

/* 危險按鈕 */
QPushButton#Danger {
    background: #dc2626;
    color: #ffffff;
}
QPushButton#Danger:hover { background: #b91c1c; }
QPushButton#Danger:pressed { background: #991b1b; }
QPushButton#Danger:disabled { background: #fecaca; color: #ffffff; }

/* ============================ 輸入元件 ============================ */
QComboBox, QLineEdit, QSpinBox, QDateTimeEdit {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: #bfdbfe;
    selection-color: #0f172a;
}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover { border-color: #94a3b8; }
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDateTimeEdit:focus {
    border: 1px solid #2563eb;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {
    background: #f1f5f9;
    color: #94a3b8;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
}
QComboBox::down-arrow {
    image: none;
    width: 0; height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #64748b;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    selection-background-color: #dbeafe;
    selection-color: #0f172a;
    outline: none;
    padding: 4px;
}
QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: none; background: transparent; }

/* ============================ log / 程式碼區 ============================ */
QPlainTextEdit, QTextEdit {
    background: #0b1220;
    color: #e2e8f0;
    border: 1px solid #1e293b;
    border-radius: 10px;
    padding: 8px;
    font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New", monospace;
    font-size: 12.5px;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}

/* ============================ 表格 ============================ */
QTableWidget, QTableView {
    background: #ffffff;
    alternate-background-color: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    gridline-color: #eef2f7;
    selection-background-color: #dbeafe;
    selection-color: #0f172a;
    outline: none;
}
QTableWidget::item, QTableView::item {
    padding: 6px 8px;
    border: none;
}
QTableWidget::item:selected, QTableView::item:selected {
    background: #dbeafe;
    color: #0f172a;
}
QHeaderView::section {
    background: #f1f5f9;
    color: #475569;
    padding: 8px 8px;
    border: none;
    border-bottom: 1px solid #e2e8f0;
    border-right: 1px solid #eef2f7;
    font-weight: 700;
}
QHeaderView::section:first { border-top-left-radius: 10px; }
QHeaderView::section:last { border-top-right-radius: 10px; border-right: none; }
QTableCornerButton::section { background: #f1f5f9; border: none; }

/* ============================ 清單 ============================ */
QListWidget {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 4px;
    outline: none;
}
QListWidget::item {
    padding: 9px 10px;
    border-radius: 8px;
    margin: 1px 2px;
    color: #1f2937;
}
QListWidget::item:hover { background: #f1f5f9; }
QListWidget::item:selected {
    background: #dbeafe;
    color: #1e3a8a;
    font-weight: 600;
}

/* ============================ 進度條 ============================ */
QProgressBar {
    border: none;
    border-radius: 8px;
    text-align: center;
    background: #e2e8f0;
    color: #0f172a;
    font-weight: 700;
    height: 22px;
}
QProgressBar::chunk {
    background: #2563eb;
    border-radius: 8px;
}

/* ============================ Tab ============================ */
QTabWidget::pane {
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    top: -1px;
    background: #ffffff;
}
QTabBar::tab {
    background: transparent;
    color: #64748b;
    padding: 9px 18px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
}
QTabBar::tab:hover { color: #1d4ed8; }
QTabBar::tab:selected {
    color: #1d4ed8;
    border-bottom: 2px solid #2563eb;
}

/* ============================ Splitter ============================ */
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 10px; }
QSplitter::handle:vertical { height: 10px; }

/* ============================ 捲軸 ============================ */
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #cbd5e1;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #94a3b8; }
QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #cbd5e1;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: #94a3b8; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* ============================ 狀態徽章 / 標籤 ============================ */
QLabel#StatusGood { color: #16a34a; font-weight: 700; }
QLabel#StatusBad  { color: #dc2626; font-weight: 700; }

/* run / record 狀態膠囊(以 objectName 套用) */
#StatusBadge {
    background: #f1f5f9;
    color: #475569;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 5px 14px;
    font-weight: 700;
}
#StatusBadgeRunning {
    background: #dbeafe;
    color: #1d4ed8;
    border: 1px solid #bfdbfe;
    border-radius: 12px;
    padding: 5px 14px;
    font-weight: 700;
}
#StatusBadgeOk {
    background: #dcfce7;
    color: #15803d;
    border: 1px solid #bbf7d0;
    border-radius: 12px;
    padding: 5px 14px;
    font-weight: 700;
}
#StatusBadgeBad {
    background: #fee2e2;
    color: #b91c1c;
    border: 1px solid #fecaca;
    border-radius: 12px;
    padding: 5px 14px;
    font-weight: 700;
}
#StatusBadgePaused {
    background: #fef3c7;
    color: #b45309;
    border: 1px solid #fde68a;
    border-radius: 12px;
    padding: 5px 14px;
    font-weight: 700;
}

/* MFA / 警示橫幅 */
#PauseBanner {
    background: #fffbeb;
    border: 1px solid #fcd34d;
    border-radius: 10px;
}
#PauseBannerText {
    color: #92400e;
    font-weight: 700;
    background: transparent;
}
"""
