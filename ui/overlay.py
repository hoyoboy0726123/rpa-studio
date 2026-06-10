# -*- coding: utf-8 -*-
"""透明 overlay 元件。

使用場景(四個):
  1. 桌面錄製標註 (recording annotation):錄製桌面操作時,以紅色 RECORDING 燈
     置頂提示「正在錄製」,避免使用者誤以為沒在錄。
  2. 執行狀態燈 (run status light):播放/執行 flow 時以綠色 PLAYING 燈 + 目前步數
     置頂顯示,讓使用者隨時看得到自動化跑到第幾步、按 F9/Stop 可中止。
  3. 元素 / 區域 picker (element / region picker):全螢幕半透明遮罩,讓使用者用滑鼠
     拖一個矩形框選螢幕上的目標元素或 OCR 區域,放開後回傳螢幕座標 rect。
     供日後桌面錄製(指定點擊目標)/ OCR 區選取使用。
  4. MFA 人工介入提示 (human-in-the-loop / MFA):流程跑到需要人工輸入 OTP / 通過
     雙因子驗證時,以醒目 overlay 提示使用者「請完成驗證後按繼續」。

兩個元件:
  StatusOverlay  — 無邊框、置頂、半透明的小狀態窗(燈號 + 步數 + 提示)。
  ElementPicker  — 全螢幕半透明遮罩,拖框選取,emit picked(QRect 螢幕座標)。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QPoint, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QApplication


# 狀態 -> (顯示文字, 燈號顏色)
_STATES = {
    "idle":      ("待命 IDLE", QColor("#94a3b8")),
    "recording": ("錄製中 RECORDING", QColor("#dc2626")),
    "playing":   ("執行中 PLAYING", QColor("#16a34a")),
    "paused":    ("等待人工 MFA / PAUSED", QColor("#f59e0b")),
}


class StatusOverlay(QWidget):
    """無邊框、置頂、半透明的狀態小窗。

    set_state('recording'|'playing'|'paused'|'idle') 切換燈號顏色;
    set_step(i, total) 更新目前步數;set_hint(str) 改提示文字(例 MFA 說明)。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._state = "idle"
        self._light = _STATES["idle"][1]
        self._drag_offset: QPoint | None = None

        self.resize(272, 104)

        lay = QVBoxLayout(self)
        # 左側留白給燈號圓點
        lay.setContentsMargins(40, 14, 18, 14)
        lay.setSpacing(4)

        self._state_label = QLabel(_STATES["idle"][0])
        f = QFont()
        f.setBold(True)
        f.setPointSize(12)
        self._state_label.setFont(f)
        self._state_label.setStyleSheet("color: #ffffff; background: transparent;")

        self._step_label = QLabel("步驟 - / -")
        self._step_label.setStyleSheet("color: #cbd5e1; background: transparent; font-size: 12px;")

        self._hint_label = QLabel("按 F9 或 Stop 可中止")
        self._hint_label.setStyleSheet("color: #94a3b8; background: transparent; font-size: 11px;")
        self._hint_label.setWordWrap(True)

        lay.addWidget(self._state_label)
        lay.addWidget(self._step_label)
        lay.addWidget(self._hint_label)

    # ---- 公開 API ---- #
    def set_state(self, state: str):
        text, color = _STATES.get(state, _STATES["idle"])
        self._state = state
        self._light = color
        self._state_label.setText(text)
        self.update()

    def set_step(self, i: int, total: int):
        self._step_label.setText(f"步驟 {i} / {total}")

    def set_hint(self, text: str):
        self._hint_label.setText(text)

    # ---- 繪製半透明圓角卡片 + 左側燈號(含柔光暈)---- #
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)

        # 卡片底:半透明深色 + 細邊
        p.setBrush(QBrush(QColor(15, 23, 42, 235)))
        p.setPen(QPen(QColor(255, 255, 255, 28), 1))
        p.drawRoundedRect(r, 14, 14)

        # 左側狀態色條(細,呼應 sidebar 高亮語彙)
        light = self._light
        bar_rect = QRect(r.left(), r.top() + 14, 4, r.height() - 28)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(light))
        p.drawRoundedRect(bar_rect, 2, 2)

        # 圓點指示燈 + 柔光暈
        cx, cy = r.left() + 22, r.top() + 22
        glow = QColor(light)
        glow.setAlpha(70)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPoint(cx, cy), 11, 11)
        p.setBrush(QBrush(light))
        p.drawEllipse(QPoint(cx, cy), 6, 6)
        # 高光點
        p.setBrush(QBrush(QColor(255, 255, 255, 160)))
        p.drawEllipse(QPoint(cx - 2, cy - 2), 2, 2)

    # ---- 可拖動移位 ---- #
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, _e):
        self._drag_offset = None


class ElementPicker(QWidget):
    """全螢幕半透明遮罩,讓使用者拖出一個矩形;放開後 emit picked(QRect)。

    picked 的 QRect 為「螢幕座標」(已加上本視窗左上原點),
    供桌面錄製指定點擊目標 / OCR 區選取使用。按 Esc 取消(emit cancelled)。
    """

    picked = Signal(QRect)     # 螢幕座標 rect
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        self._origin: QPoint | None = None
        self._rubber = QRect()
        # 覆蓋目前所在(或主)螢幕的整個可用區
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())

    def paintEvent(self, _e):
        p = QPainter(self)
        # 半透明暗化整個畫面
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if not self._rubber.isNull():
            # 選取框內挖回較亮、加綠色外框
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(self._rubber, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            pen = QPen(QColor("#16a34a"), 2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(self._rubber)
            label = f"{self._rubber.width()} x {self._rubber.height()}"
            p.setPen(QColor("#ffffff"))
            p.drawText(self._rubber.topLeft() + QPoint(2, -6), label)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._origin = e.position().toPoint()
            self._rubber = QRect(self._origin, self._origin)
            self.update()

    def mouseMoveEvent(self, e):
        if self._origin is not None:
            self._rubber = QRect(self._origin, e.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._origin is not None:
            rect = QRect(self._origin, e.position().toPoint()).normalized()
            # 轉成螢幕座標(加本視窗原點)
            screen_rect = QRect(self.mapToGlobal(rect.topLeft()), rect.size())
            self._origin = None
            self.picked.emit(screen_rect)
            self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()
