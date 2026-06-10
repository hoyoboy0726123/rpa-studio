# -*- coding: utf-8 -*-
"""流程編輯器頁 (EditorPage)。

對選定的 flow 做:
  - step 增 / 刪 / 上移 / 下移(左側步驟清單 + 工具列)
  - 編輯選定 step:action(ACTION_CATALOG 下拉)/ label / on_error / timeout / retry
  - 編輯 params(key-value 表格,可增減列)
  - 編輯 target(primary strategy+value + fallbacks 表格)
  - 存回 Store(store.save_flow)

設計重點:
- 所有結構操作都委派給 ui.flow_edit_ops 的純函式(可被測試直接呼叫,不必點 UI)。
- 切換選取 / 切 flow 前,先把目前表單值收斂回記憶體中的 Flow(_commit_form),
  避免使用者改了沒按存就遺失。
- 不卡主執行緒:本頁全是同步的記憶體操作 + 一次 save_flow,無長任務。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QListWidget, QListWidgetItem, QLineEdit, QSpinBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QFormLayout, QGroupBox, QSplitter,
    QMessageBox, QAbstractItemView, QScrollArea,
)

from core.schema import Flow
from ui import flow_edit_ops as ops
from ui.widgets import page_header, Card

_STRATEGIES = ["", "role", "text", "testid", "css", "xpath", "uia", "image", "coord"]
_ON_ERROR = ["abort", "continue", "goto:"]


class EditorPage(QWidget):
    flows_changed = Signal()   # 存檔後通知其他頁刷新

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.flow: Flow | None = None
        self._current_row: int = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, title, hint = page_header(
            "流程編輯器",
            "選一條流程,即可新增 / 刪除 / 上下移動步驟,並編輯每步的動作、參數與定位目標,最後存回。")
        root.addWidget(header)

        # ---- 頂列:選 flow + 存檔 ---- #
        topcard = Card(margins=(14, 12, 14, 12))
        topbar = QHBoxLayout()
        topbar.setSpacing(8)
        flow_lbl = QLabel("流程:")
        flow_lbl.setObjectName("FieldLabel")
        topbar.addWidget(flow_lbl)
        self.combo_flow = QComboBox()
        self.combo_flow.setMinimumWidth(220)
        topbar.addWidget(self.combo_flow)
        self.btn_reload = QPushButton("↻  重新載入")
        self.btn_reload.setObjectName("Ghost")
        self.btn_save = QPushButton("💾  存檔")
        topbar.addWidget(self.btn_reload)
        topbar.addStretch(1)
        topbar.addWidget(self.btn_save)
        topcard.body.addLayout(topbar)
        root.addWidget(topcard)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(14)

        # ---- 左:步驟清單 + 工具列 ---- #
        left = Card(margins=(10, 10, 10, 10))
        step_lbl = QLabel("步驟")
        step_lbl.setObjectName("SectionLabel")
        left.body.addWidget(step_lbl)
        self.list = QListWidget()
        self.list.setMinimumWidth(240)
        left.body.addWidget(self.list, 1)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.btn_add = QPushButton("＋ 新增")
        self.btn_del = QPushButton("刪除")
        self.btn_del.setObjectName("Danger")
        self.btn_up = QPushButton("↑ 上移")
        self.btn_up.setObjectName("Ghost")
        self.btn_down = QPushButton("↓ 下移")
        self.btn_down.setObjectName("Ghost")
        for b in (self.btn_add, self.btn_del, self.btn_up, self.btn_down):
            toolbar.addWidget(b)
        left.body.addLayout(toolbar)
        split.addWidget(left)

        # ---- 右:選定 step 的編輯表單(放進可捲動區)---- #
        self.detail = self._build_detail_form()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.detail)
        split.addWidget(scroll)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([300, 600])
        root.addWidget(split, 1)

        # ---- 訊號 ---- #
        self.combo_flow.currentIndexChanged.connect(self._on_flow_changed)
        self.btn_reload.clicked.connect(self._reload_current)
        self.btn_save.clicked.connect(self._save)
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.btn_add.clicked.connect(self._add)
        self.btn_del.clicked.connect(self._delete)
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(+1))

        self.refresh()

    # ------------------------------------------------------------------ #
    # 詳細表單
    # ------------------------------------------------------------------ #
    def _build_detail_form(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # 基本欄位
        gb_basic = QGroupBox("基本")
        form = QFormLayout(gb_basic)
        self.cb_action = QComboBox()
        self.cb_action.setEditable(True)   # 允許輸入未列出的 action
        self.ed_label = QLineEdit()
        self.cb_on_error = QComboBox()
        self.cb_on_error.addItems(_ON_ERROR)
        self.ed_goto = QLineEdit()
        self.ed_goto.setPlaceholderText("on_error=goto: 時填目標 step id")
        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(0, 600000)
        self.sp_timeout.setSingleStep(1000)
        self.sp_timeout.setSuffix(" ms")
        self.ed_secret = QLineEdit()
        self.ed_secret.setPlaceholderText("secret 名稱(可留空)")
        form.addRow("動作 action", self.cb_action)
        form.addRow("標籤 label", self.ed_label)
        form.addRow("錯誤處理 on_error", self.cb_on_error)
        form.addRow("goto 目標", self.ed_goto)
        form.addRow("逾時 timeout", self.sp_timeout)
        form.addRow("secret_ref", self.ed_secret)
        lay.addWidget(gb_basic)

        # retry
        gb_retry = QGroupBox("重試 retry")
        rform = QFormLayout(gb_retry)
        self.sp_retry_times = QSpinBox()
        self.sp_retry_times.setRange(0, 100)
        self.sp_retry_interval = QSpinBox()
        self.sp_retry_interval.setRange(0, 600000)
        self.sp_retry_interval.setSingleStep(500)
        self.sp_retry_interval.setSuffix(" ms")
        rform.addRow("次數 times", self.sp_retry_times)
        rform.addRow("間隔 interval", self.sp_retry_interval)
        lay.addWidget(gb_retry)

        # params key-value 表格
        gb_params = QGroupBox("參數 params(key-value)")
        play = QVBoxLayout(gb_params)
        self.tbl_params = QTableWidget(0, 2)
        self.tbl_params.setHorizontalHeaderLabels(["key", "value"])
        self.tbl_params.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        play.addWidget(self.tbl_params)
        pbtns = QHBoxLayout()
        self.btn_param_add = QPushButton("＋ 加一列")
        self.btn_param_add.setObjectName("Ghost")
        self.btn_param_del = QPushButton("刪除選取列")
        self.btn_param_del.setObjectName("Ghost")
        self.btn_pick_region = QPushButton("🖱 框選區域")
        self.btn_pick_region.setToolTip("在畫面上拖一個框,自動填入 x / y / w / h"
                                        "(OCR / 影像區域用,不必自己算座標)")
        pbtns.addWidget(self.btn_param_add)
        pbtns.addWidget(self.btn_param_del)
        pbtns.addWidget(self.btn_pick_region)
        pbtns.addStretch(1)
        play.addLayout(pbtns)
        lay.addWidget(gb_params)
        self.btn_param_add.clicked.connect(lambda: self.tbl_params.insertRow(self.tbl_params.rowCount()))
        self.btn_param_del.clicked.connect(self._del_param_row)
        self.btn_pick_region.clicked.connect(self._pick_region)

        # target
        gb_target = QGroupBox("定位目標 target")
        tlay = QVBoxLayout(gb_target)
        pform = QFormLayout()
        self.cb_strat = QComboBox()
        self.cb_strat.addItems(_STRATEGIES)
        self.ed_strat_val = QLineEdit()
        pform.addRow("primary strategy", self.cb_strat)
        pform.addRow("primary value", self.ed_strat_val)
        tlay.addLayout(pform)
        tlay.addWidget(QLabel("fallbacks(備援定位器)"))
        self.tbl_fb = QTableWidget(0, 2)
        self.tbl_fb.setHorizontalHeaderLabels(["strategy", "value"])
        self.tbl_fb.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        tlay.addWidget(self.tbl_fb)
        fbtns = QHBoxLayout()
        self.btn_fb_add = QPushButton("＋ 加一列")
        self.btn_fb_add.setObjectName("Ghost")
        self.btn_fb_del = QPushButton("刪除選取列")
        self.btn_fb_del.setObjectName("Ghost")
        fbtns.addWidget(self.btn_fb_add)
        fbtns.addWidget(self.btn_fb_del)
        fbtns.addStretch(1)
        tlay.addLayout(fbtns)
        lay.addWidget(gb_target)
        self.btn_fb_add.clicked.connect(lambda: self.tbl_fb.insertRow(self.tbl_fb.rowCount()))
        self.btn_fb_del.clicked.connect(self._del_fb_row)

        lay.addStretch(1)
        self.detail_widgets = w
        w.setEnabled(False)
        return w

    # ------------------------------------------------------------------ #
    # flow 載入 / 刷新
    # ------------------------------------------------------------------ #
    def refresh(self):
        """重建 flow 下拉(供 main_window 在 flows_changed 時呼叫)。"""
        current = self.combo_flow.currentText()
        self.combo_flow.blockSignals(True)
        self.combo_flow.clear()
        for row in self.store.list_flows():
            self.combo_flow.addItem(row["name"])
        if current:
            idx = self.combo_flow.findText(current)
            if idx >= 0:
                self.combo_flow.setCurrentIndex(idx)
        self.combo_flow.blockSignals(False)
        # 載入目前選定的 flow
        if self.combo_flow.count() > 0:
            self._load_flow(self.combo_flow.currentText())
        else:
            self.flow = None
            self.list.clear()
            self.detail_widgets.setEnabled(False)

    def _on_flow_changed(self, _idx):
        self._load_flow(self.combo_flow.currentText())

    def _reload_current(self):
        self._load_flow(self.combo_flow.currentText())

    def _load_flow(self, name: str):
        if not name:
            return
        d = self.store.load_flow(name)
        if not d:
            return
        self.flow = Flow.from_dict(d)
        self._populate_action_combo()
        self._rebuild_step_list()
        self._current_row = -1
        if self.flow.steps:
            self.list.setCurrentRow(0)
        else:
            self.detail_widgets.setEnabled(False)

    def _populate_action_combo(self):
        self.cb_action.blockSignals(True)
        self.cb_action.clear()
        engine = self.flow.engine if self.flow else None
        self.cb_action.addItems(ops.all_actions(engine))
        self.cb_action.blockSignals(True)
        self.cb_action.blockSignals(False)

    def _rebuild_step_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for i, s in enumerate(self.flow.steps if self.flow else []):
            label = s.label or s.action
            QListWidgetItem(f"{i + 1}. {s.action}  —  {label}", self.list)
        self.list.blockSignals(False)

    # ------------------------------------------------------------------ #
    # 選取 step → 載入表單 / 收斂表單
    # ------------------------------------------------------------------ #
    def _on_row_changed(self, row: int):
        # 切換前先把目前表單收回上一個 step
        if self._current_row >= 0 and self.flow and self._current_row < len(self.flow.steps):
            self._commit_form(self._current_row)
        self._current_row = row
        if self.flow and 0 <= row < len(self.flow.steps):
            self.detail_widgets.setEnabled(True)
            self._load_form(self.flow.steps[row])
        else:
            self.detail_widgets.setEnabled(False)

    def _load_form(self, step):
        self.cb_action.setCurrentText(step.action)
        self.ed_label.setText(step.label or "")
        oe = step.on_error or "abort"
        if oe.startswith("goto:"):
            self.cb_on_error.setCurrentText("goto:")
            self.ed_goto.setText(oe.split(":", 1)[1])
        else:
            self.cb_on_error.setCurrentText(oe if oe in _ON_ERROR else "abort")
            self.ed_goto.setText("")
        self.sp_timeout.setValue(int(step.timeout_ms or 0))
        self.ed_secret.setText(step.secret_ref or "")
        retry = step.retry or {}
        self.sp_retry_times.setValue(int(retry.get("times", 0)))
        self.sp_retry_interval.setValue(int(retry.get("interval_ms", 1000)))

        # params
        self.tbl_params.setRowCount(0)
        for k, v in (step.params or {}).items():
            if k == "_secret":
                continue
            r = self.tbl_params.rowCount()
            self.tbl_params.insertRow(r)
            self.tbl_params.setItem(r, 0, QTableWidgetItem(str(k)))
            self.tbl_params.setItem(r, 1, QTableWidgetItem("" if v is None else str(v)))

        # target
        target = step.target or {}
        primary = target.get("primary") or {}
        self.cb_strat.setCurrentText(primary.get("strategy", ""))
        self.ed_strat_val.setText(str(primary.get("value", "")))
        self.tbl_fb.setRowCount(0)
        for fb in target.get("fallbacks") or []:
            r = self.tbl_fb.rowCount()
            self.tbl_fb.insertRow(r)
            self.tbl_fb.setItem(r, 0, QTableWidgetItem(str(fb.get("strategy", ""))))
            self.tbl_fb.setItem(r, 1, QTableWidgetItem(str(fb.get("value", ""))))

    def _commit_form(self, row: int):
        """把表單目前值寫回 flow.steps[row]。"""
        if not self.flow or not (0 <= row < len(self.flow.steps)):
            return
        step = self.flow.steps[row]

        on_error = self.cb_on_error.currentText()
        if on_error == "goto:":
            on_error = "goto:" + self.ed_goto.text().strip()
        ops.update_step_basic(
            step,
            action=self.cb_action.currentText().strip() or step.action,
            label=self.ed_label.text(),
            secret_ref=self.ed_secret.text().strip(),
            on_error=on_error,
            timeout_ms=self.sp_timeout.value(),
        )
        ops.set_retry(step, self.sp_retry_times.value(), self.sp_retry_interval.value())

        params = {}
        for r in range(self.tbl_params.rowCount()):
            k_item = self.tbl_params.item(r, 0)
            v_item = self.tbl_params.item(r, 1)
            key = k_item.text() if k_item else ""
            val = v_item.text() if v_item else ""
            if key.strip():
                params[key] = val
        ops.set_params(step, params)

        fbs = []
        for r in range(self.tbl_fb.rowCount()):
            s_item = self.tbl_fb.item(r, 0)
            v_item = self.tbl_fb.item(r, 1)
            fbs.append((s_item.text() if s_item else "",
                        v_item.text() if v_item else ""))
        ops.set_target(step, self.cb_strat.currentText(),
                       self.ed_strat_val.text(), fbs)

        # 同步清單顯示文字
        item = self.list.item(row)
        if item is not None:
            item.setText(f"{row + 1}. {step.action}  —  {step.label or step.action}")

    # ------------------------------------------------------------------ #
    # 增 / 刪 / 移動
    # ------------------------------------------------------------------ #
    def _add(self):
        if not self.flow:
            QMessageBox.information(self, "尚未選擇流程", "請先在上方選一條流程。")
            return
        if self._current_row >= 0:
            self._commit_form(self._current_row)
        default_action = ops.all_actions(self.flow.engine)[0]
        at = self.list.currentRow() + 1 if self.list.currentRow() >= 0 else None
        ops.add_step(self.flow, action=default_action, at=at)
        self._rebuild_step_list()
        new_row = at if at is not None else len(self.flow.steps) - 1
        self.list.setCurrentRow(new_row)

    def _delete(self):
        if not self.flow:
            return
        row = self.list.currentRow()
        if row < 0:
            return
        if ops.delete_step(self.flow, row):
            self._current_row = -1   # 不要把已刪 step 的表單收回
            self._rebuild_step_list()
            if self.flow.steps:
                self.list.setCurrentRow(min(row, len(self.flow.steps) - 1))
            else:
                self.detail_widgets.setEnabled(False)

    def _move(self, delta: int):
        if not self.flow:
            return
        row = self.list.currentRow()
        if row < 0:
            return
        self._commit_form(row)
        new_row = ops.move_step(self.flow, row, delta)
        self._current_row = -1   # 已在記憶體換好,避免重複收斂
        self._rebuild_step_list()
        self.list.setCurrentRow(new_row)

    def _del_param_row(self):
        r = self.tbl_params.currentRow()
        if r >= 0:
            self.tbl_params.removeRow(r)

    def _del_fb_row(self):
        r = self.tbl_fb.currentRow()
        if r >= 0:
            self.tbl_fb.removeRow(r)

    # ------------------------------------------------------------------ #
    # 框選螢幕區域 → 自動填 x / y / w / h(OCR / 影像區域用,免算座標)
    # ------------------------------------------------------------------ #
    def _set_param(self, key: str, value):
        """把 params 表格中 key 那列的值設成 value;沒有就新增一列。"""
        t = self.tbl_params
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it and it.text().strip() == key:
                t.setItem(r, 1, QTableWidgetItem(str(value)))
                return
        r = t.rowCount()
        t.insertRow(r)
        t.setItem(r, 0, QTableWidgetItem(key))
        t.setItem(r, 1, QTableWidgetItem(str(value)))

    def _pick_region(self):
        """最小化主視窗 → 全螢幕拖框 → 放開後把螢幕座標 x/y/w/h 填回 params。"""
        try:
            from ui.overlay import ElementPicker
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "框選區域", f"無法載入框選工具:{e}")
            return
        mw = self.window()
        picker = ElementPicker()
        self._region_picker = picker  # 持有參考,避免被 GC

        def _done(rect):
            self._set_param("x", int(rect.x()))
            self._set_param("y", int(rect.y()))
            self._set_param("w", int(rect.width()))
            self._set_param("h", int(rect.height()))
            try:
                picker.close()
            except Exception:
                pass
            self._region_picker = None
            mw.showNormal(); mw.raise_(); mw.activateWindow()

        def _cancel():
            try:
                picker.close()
            except Exception:
                pass
            self._region_picker = None
            mw.showNormal(); mw.raise_(); mw.activateWindow()

        picker.picked.connect(_done)
        picker.cancelled.connect(_cancel)
        # 先把 RPA Studio 最小化(露出後面目標 App),稍候再顯示全螢幕遮罩讓使用者框選
        mw.showMinimized()
        QTimer.singleShot(350, picker.showFullScreen)

    # ------------------------------------------------------------------ #
    # 存檔
    # ------------------------------------------------------------------ #
    def _save(self):
        if not self.flow:
            QMessageBox.information(self, "尚未選擇流程", "沒有可存的流程。")
            return
        if self._current_row >= 0:
            self._commit_form(self._current_row)
        try:
            ops.save_flow_to_store(self.flow, self.store)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "存檔失敗", f"{type(e).__name__}: {e}")
            return
        self.flows_changed.emit()
        QMessageBox.information(self, "已存檔",
                                f"流程「{self.flow.name}」已存回({len(self.flow.steps)} 步)。")
