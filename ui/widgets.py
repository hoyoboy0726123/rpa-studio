# -*- coding: utf-8 -*-
"""共用視覺輔助元件(純外觀,不含任何業務邏輯)。

提供各頁一致的版型零件:
  - page_header(title, hint): 統一的「標題 + 一行說明」header 區。
  - Card: 圓角、細邊、白底、內距的卡片容器(objectName="Card",樣式在 style.py)。

這些只負責排版與套 objectName 給 QSS,不改任何 signal / 方法名 / 頁面標題文字。
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QFrame


def page_header(title: str, hint: str) -> tuple[QWidget, QLabel, QLabel]:
    """建立統一的頁面 header(標題 + 說明)。

    回傳 (container, title_label, hint_label);呼叫端可保留 label 參考做後續設定
    (例如測試會檢查 objectName=PageTitle / PageHint 不變)。
    """
    box = QWidget()
    box.setObjectName("HeaderBar")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 12)
    lay.setSpacing(3)

    title_label = QLabel(title)
    title_label.setObjectName("PageTitle")

    hint_label = QLabel(hint)
    hint_label.setObjectName("PageHint")
    hint_label.setWordWrap(True)

    lay.addWidget(title_label)
    lay.addWidget(hint_label)
    return box, title_label, hint_label


class Card(QFrame):
    """圓角白底卡片容器;自帶內距與垂直 layout。

    用法:
        card = Card()
        card.body.addWidget(...)
    """

    def __init__(self, parent=None, *, margins=(16, 16, 16, 16), spacing=10):
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*margins)
        self.body.setSpacing(spacing)
