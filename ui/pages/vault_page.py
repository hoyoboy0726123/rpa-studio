# -*- coding: utf-8 -*-
"""憑證頁:用 Vault 設定 / 列出 secret。只顯示名稱,絕不顯示值。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QListWidget, QMessageBox,
)
from PySide6.QtCore import Qt

from ui.widgets import page_header, Card


class VaultPage(QWidget):
    def __init__(self, vault, parent=None):
        super().__init__(parent)
        self.vault = vault

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, title, hint = page_header(
            "憑證管理",
            "secret 值存進 OS keyring 或本機加密檔,不會寫進流程 JSON;"
            "此處只顯示名稱,不顯示值。")
        root.addWidget(header)

        form_card = Card()
        add_lbl = QLabel("新增 / 更新 secret")
        add_lbl.setObjectName("SectionLabel")
        form_card.body.addWidget(add_lbl)
        form = QHBoxLayout()
        form.setSpacing(8)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("secret 名稱(例 bis_password)")
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("secret 值(只在儲存時使用,不會被顯示)")
        self.value_edit.setEchoMode(QLineEdit.Password)
        self.btn_save = QPushButton("💾  儲存 / 更新")
        form.addWidget(self.name_edit, 1)
        form.addWidget(self.value_edit, 1)
        form.addWidget(self.btn_save)
        form_card.body.addLayout(form)
        root.addWidget(form_card)

        list_card = Card()
        names_lbl = QLabel("已存的 secret 名稱")
        names_lbl.setObjectName("SectionLabel")
        list_card.body.addWidget(names_lbl)
        self.list = QListWidget()
        list_card.body.addWidget(self.list, 1)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_delete = QPushButton("🗑  刪除選取")
        self.btn_delete.setObjectName("Danger")
        self.btn_refresh = QPushButton("↻  重新整理")
        self.btn_refresh.setObjectName("Ghost")
        bar.addStretch(1)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_delete)
        list_card.body.addLayout(bar)
        root.addWidget(list_card, 1)

        self.btn_save.clicked.connect(self._save)
        self.btn_delete.clicked.connect(self._delete)
        self.btn_refresh.clicked.connect(self.refresh)

        self.refresh()

    def refresh(self):
        self.list.clear()
        try:
            names = self.vault.list_names()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "憑證", f"讀取 secret 名稱失敗:{e}")
            names = []
        for n in names:
            self.list.addItem(n)

    def _save(self):
        name = self.name_edit.text().strip()
        value = self.value_edit.text()
        if not name or not value:
            QMessageBox.information(self, "憑證", "請同時填寫名稱與值。")
            return
        try:
            self.vault.set_secret(name, value)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "憑證", f"儲存失敗:{e}")
            return
        self.value_edit.clear()
        self.name_edit.clear()
        self.refresh()
        QMessageBox.information(self, "憑證", f"已儲存 secret「{name}」(值未顯示)。")

    def _delete(self):
        item = self.list.currentItem()
        if item is None:
            return
        name = item.text()
        if QMessageBox.question(self, "刪除", f"確定刪除 secret「{name}」?") \
                != QMessageBox.Yes:
            return
        try:
            self.vault.delete_secret(name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "憑證", f"刪除失敗:{e}")
            return
        self.refresh()
