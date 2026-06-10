# -*- coding: utf-8 -*-
"""排程頁:實際呼叫 Windows 工作排程器 (schtasks) 建立 / 列出 / 刪除排程任務。

任務內容 = 用本機 python 跑 `run_cli.py --flow <name>`(每日 / 每週 / 每月 + 時間)。
- 上方表單:選 flow、頻率、時間(每週可選星期、每月可選日)→「建立排程」。
- 下方表格:列出所有 RPAStudio_ 前綴的排程任務,可重新整理 / 刪除選取任務。
- schtasks 失敗(多半為權限不足)→ 以 QMessageBox 友善提示改用系統管理員身分。

不卡 UI:schtasks 是外部 process,放進 _SchtasksWorker(QThread)執行,
結果以 signal 丟回主執行緒。組指令 / 解析列表的純邏輯在 ui.schtasks_ops。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QAbstractItemView, QPlainTextEdit, QTabWidget,
)

from ui import schtasks_ops as st
from ui.widgets import page_header, Card


_FREQ_ITEMS = [
    ("每天", st.FREQ_DAILY),
    ("每週", st.FREQ_WEEKLY),
    ("每月", st.FREQ_MONTHLY),
]
_WEEKDAY_ITEMS = [
    ("週一", "MON"), ("週二", "TUE"), ("週三", "WED"), ("週四", "THU"),
    ("週五", "FRI"), ("週六", "SAT"), ("週日", "SUN"),
]


class _SchtasksWorker(QThread):
    """在背景跑一個 schtasks 操作(create / delete / list),不卡 UI 主執行緒。"""

    done = Signal(str, object)   # (op, payload):list -> (tasks, result);其餘 -> result

    def __init__(self, op: str, kwargs: dict, parent=None):
        super().__init__(parent)
        self.op = op
        self.kwargs = kwargs or {}

    def run(self):
        try:
            if self.op == "create":
                payload = st.create_task(**self.kwargs)
            elif self.op == "delete":
                payload = st.delete_task(**self.kwargs)
            elif self.op == "list":
                payload = st.list_tasks(**self.kwargs)
            else:
                payload = st.SchtasksResult(False, f"未知操作:{self.op}")
        except Exception as e:  # noqa: BLE001
            payload = st.SchtasksResult(False, f"{type(e).__name__}: {e}")
        self.done.emit(self.op, payload)


class SchedulePage(QWidget):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._worker: _SchtasksWorker | None = None

        self._inapp_sched = None     # lazy 建立的 FlowScheduler(APScheduler)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, title, hint = page_header(
            "排程",
            "兩種排程並存:① Windows 工作排程器(schtasks)— 程式關著也會跑,需系統管理員權限;"
            "② App 內排程(APScheduler)— 只在本程式開著時生效、免權限。")
        root.addWidget(header)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # 分頁一:Windows schtasks(作業系統層)
        st_tab = QWidget()
        root = QVBoxLayout(st_tab)   # 後續沿用既有 root.add* 程式碼,改往 st_tab 加
        root.setContentsMargins(4, 8, 4, 4)
        root.setSpacing(14)
        self.tabs.addTab(st_tab, "Windows 排程 (schtasks)")

        # ---- 建立表單 ---- #
        form_card = Card()
        create_lbl = QLabel("建立新排程")
        create_lbl.setObjectName("SectionLabel")
        form_card.body.addWidget(create_lbl)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(180)
        self.freq = QComboBox()
        for label, code in _FREQ_ITEMS:
            self.freq.addItem(label, code)
        self.weekday = QComboBox()
        for label, code in _WEEKDAY_ITEMS:
            self.weekday.addItem(label, code)
        self.day = QSpinBox()
        self.day.setRange(1, 31)
        self.day.setValue(1)
        self.day.setSuffix(" 日")
        self.time_edit = QLineEdit("08:00")
        self.time_edit.setMaximumWidth(80)

        lbl_flow = QLabel("流程:"); lbl_flow.setObjectName("FieldLabel")
        lbl_freq = QLabel("頻率:"); lbl_freq.setObjectName("FieldLabel")
        lbl_time = QLabel("時間:"); lbl_time.setObjectName("FieldLabel")
        bar.addWidget(lbl_flow)
        bar.addWidget(self.combo, 1)
        bar.addWidget(lbl_freq)
        bar.addWidget(self.freq)
        bar.addWidget(self.weekday)
        bar.addWidget(self.day)
        bar.addWidget(lbl_time)
        bar.addWidget(self.time_edit)
        form_card.body.addLayout(bar)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_refresh_flows = QPushButton("↻  重新整理流程")
        self.btn_refresh_flows.setObjectName("Ghost")
        self.btn_create = QPushButton("＋  建立排程")
        self.btn_preview = QPushButton("👁  預覽指令")
        self.btn_preview.setObjectName("Ghost")
        btns.addWidget(self.btn_refresh_flows)
        btns.addStretch(1)
        btns.addWidget(self.btn_preview)
        btns.addWidget(self.btn_create)
        form_card.body.addLayout(btns)
        root.addWidget(form_card)

        # ---- 既有任務清單 ---- #
        list_card = Card()
        list_bar = QHBoxLayout()
        tasks_lbl = QLabel("已建立的排程任務(RPAStudio_*)")
        tasks_lbl.setObjectName("SectionLabel")
        list_bar.addWidget(tasks_lbl)
        list_bar.addStretch(1)
        self.btn_reload_tasks = QPushButton("↻  重新整理清單")
        self.btn_reload_tasks.setObjectName("Ghost")
        self.btn_delete = QPushButton("🗑  刪除選取任務")
        self.btn_delete.setObjectName("Danger")
        list_bar.addWidget(self.btn_reload_tasks)
        list_bar.addWidget(self.btn_delete)
        list_card.body.addLayout(list_bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["任務名稱", "排程", "下次執行", "狀態"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        list_card.body.addWidget(self.table, 1)
        root.addWidget(list_card, 1)

        out_card = Card(margins=(14, 12, 14, 12))
        out_lbl = QLabel("操作訊息 / 指令預覽")
        out_lbl.setObjectName("SectionLabel")
        out_card.body.addWidget(out_lbl)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMaximumHeight(120)
        out_card.body.addWidget(self.out)
        root.addWidget(out_card)

        # ---- 訊號 ---- #
        self.freq.currentIndexChanged.connect(self._on_freq_changed)
        self.btn_refresh_flows.clicked.connect(self.refresh)
        self.btn_create.clicked.connect(self._create)
        self.btn_preview.clicked.connect(self._preview)
        self.btn_reload_tasks.clicked.connect(self._reload_tasks)
        self.btn_delete.clicked.connect(self._delete)

        # 分頁二:App 內排程(APScheduler)
        self._build_inapp_tab()

        self._on_freq_changed()
        self.refresh()

    # ------------------------------------------------------------------ #
    def _build_inapp_tab(self):
        """App 內排程分頁(APScheduler)。程式開著時定時跑 flow;免系統權限。"""
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(14)
        self.tabs.addTab(tab, "App 內排程 (APScheduler)")

        form = Card()
        lbl = QLabel("新增 App 內排程(僅本程式開著時生效)")
        lbl.setObjectName("SectionLabel")
        form.body.addWidget(lbl)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.ia_combo = QComboBox()
        self.ia_combo.setMinimumWidth(180)
        self.ia_freq = QComboBox()
        for label, code in _FREQ_ITEMS:
            self.ia_freq.addItem(label, code)
        self.ia_weekday = QComboBox()
        for label, code in _WEEKDAY_ITEMS:
            self.ia_weekday.addItem(label, code)
        self.ia_day = QSpinBox()
        self.ia_day.setRange(1, 31)
        self.ia_day.setValue(1)
        self.ia_day.setSuffix(" 日")
        self.ia_time = QLineEdit("08:00")
        self.ia_time.setMaximumWidth(80)
        for w, t in ((QLabel("流程:"), None), (self.ia_combo, 1),
                     (QLabel("頻率:"), None), (self.ia_freq, None),
                     (self.ia_weekday, None), (self.ia_day, None),
                     (QLabel("時間:"), None), (self.ia_time, None)):
            if isinstance(w, QLabel):
                w.setObjectName("FieldLabel")
            bar.addWidget(w, t or 0)
        form.body.addLayout(bar)

        btns = QHBoxLayout()
        self.ia_btn_start = QPushButton("▶  啟動排程器")
        self.ia_btn_add = QPushButton("＋  加入排程")
        self.ia_btn_runnow = QPushButton("⚡  立即執行一次")
        self.ia_btn_runnow.setObjectName("Ghost")
        self.ia_btn_remove = QPushButton("🗑  移除選取")
        self.ia_btn_remove.setObjectName("Danger")
        btns.addWidget(self.ia_btn_start)
        btns.addStretch(1)
        btns.addWidget(self.ia_btn_runnow)
        btns.addWidget(self.ia_btn_remove)
        btns.addWidget(self.ia_btn_add)
        form.body.addLayout(btns)
        lay.addWidget(form)

        list_card = Card()
        ll = QLabel("目前 App 內排程")
        ll.setObjectName("SectionLabel")
        list_card.body.addWidget(ll)
        self.ia_table = QTableWidget(0, 3)
        self.ia_table.setHorizontalHeaderLabels(["Job ID", "下次執行", "觸發條件"])
        self.ia_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.ia_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ia_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        list_card.body.addWidget(self.ia_table, 1)
        lay.addWidget(list_card, 1)

        self.ia_freq.currentIndexChanged.connect(self._ia_on_freq_changed)
        self.ia_btn_start.clicked.connect(self._ia_start)
        self.ia_btn_add.clicked.connect(self._ia_add)
        self.ia_btn_runnow.clicked.connect(self._ia_run_now)
        self.ia_btn_remove.clicked.connect(self._ia_remove)
        self._ia_on_freq_changed()

    def _ia_scheduler(self):
        """lazy 取得 FlowScheduler;缺 APScheduler 時回傳實例但 available=False。"""
        if self._inapp_sched is None:
            try:
                from core.scheduler import FlowScheduler
                self._inapp_sched = FlowScheduler(
                    store=self.store, vault=None, log=lambda *_a: None)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "排程器不可用", str(e))
                return None
        return self._inapp_sched

    def _ia_on_freq_changed(self):
        code = self.ia_freq.currentData()
        self.ia_weekday.setVisible(code == st.FREQ_WEEKLY)
        self.ia_day.setVisible(code == st.FREQ_MONTHLY)

    _FREQ_MAP = {st.FREQ_DAILY: "daily", st.FREQ_WEEKLY: "weekly",
                 st.FREQ_MONTHLY: "monthly"}

    def _ia_start(self):
        sch = self._ia_scheduler()
        if sch is None:
            return
        res = sch.start()
        if not res.ok:
            QMessageBox.warning(self, "啟動失敗", res.message)
        else:
            self.ia_btn_start.setEnabled(False)
            self.ia_btn_start.setText("● 排程器執行中")
        self._ia_reload()

    def _ia_add(self):
        sch = self._ia_scheduler()
        if sch is None:
            return
        name = self.ia_combo.currentText().strip()
        if not name:
            QMessageBox.information(self, "尚未選流程", "請先選一條流程。")
            return
        sch.start()
        res = sch.add_flow_job(
            name, self._FREQ_MAP.get(self.ia_freq.currentData(), "daily"),
            self.ia_time.text().strip() or "08:00",
            weekday=self.ia_weekday.currentData(),
            day=self.ia_day.value(),
        )
        if not res.ok:
            QMessageBox.warning(self, "加排程失敗", res.message)
        self.ia_btn_start.setEnabled(False)
        self.ia_btn_start.setText("● 排程器執行中")
        self._ia_reload()

    def _ia_run_now(self):
        sch = self._ia_scheduler()
        if sch is None:
            return
        name = self.ia_combo.currentText().strip()
        if not name:
            QMessageBox.information(self, "尚未選流程", "請先選一條流程。")
            return
        sch.trigger_now(name)

    def _ia_remove(self):
        sch = self._ia_scheduler()
        if sch is None:
            return
        row = self.ia_table.currentRow()
        if row < 0:
            return
        item = self.ia_table.item(row, 0)
        if item:
            sch.remove_job(item.text())
        self._ia_reload()

    def _ia_reload(self):
        sch = self._inapp_sched
        jobs = sch.list_jobs() if sch is not None else []
        self.ia_table.setRowCount(len(jobs))
        for r, j in enumerate(jobs):
            self.ia_table.setItem(r, 0, QTableWidgetItem(j.get("id", "")))
            self.ia_table.setItem(r, 1, QTableWidgetItem(j.get("next_run", "")))
            self.ia_table.setItem(r, 2, QTableWidgetItem(j.get("trigger", "")))

    # ------------------------------------------------------------------ #
    def refresh(self):
        """重建流程下拉(供 main_window 在 flows_changed 時呼叫)。"""
        current = self.combo.currentText()
        self.combo.clear()
        for row in self.store.list_flows():
            self.combo.addItem(row["name"])
        if current:
            idx = self.combo.findText(current)
            if idx >= 0:
                self.combo.setCurrentIndex(idx)
        # 同步 App 內排程的流程下拉
        if hasattr(self, "ia_combo"):
            cur = self.ia_combo.currentText()
            self.ia_combo.clear()
            for row in self.store.list_flows():
                self.ia_combo.addItem(row["name"])
            if cur:
                idx = self.ia_combo.findText(cur)
                if idx >= 0:
                    self.ia_combo.setCurrentIndex(idx)

    def _on_freq_changed(self):
        code = self.freq.currentData()
        self.weekday.setVisible(code == st.FREQ_WEEKLY)
        self.day.setVisible(code == st.FREQ_MONTHLY)

    def _form_kwargs(self) -> dict | None:
        name = self.combo.currentText().strip()
        if not name:
            QMessageBox.information(self, "尚未選流程", "請先選一條流程。")
            return None
        return {
            "flow_name": name,
            "freq": self.freq.currentData(),
            "time_s": self.time_edit.text().strip() or "08:00",
            "weekday": self.weekday.currentData(),
            "day": self.day.value(),
        }

    def _busy(self, busy: bool):
        for b in (self.btn_create, self.btn_delete, self.btn_reload_tasks):
            b.setEnabled(not busy)

    def _log(self, text: str):
        self.out.appendPlainText(text)

    # ---- 預覽(純字串,不執行)---- #
    def _preview(self):
        kw = self._form_kwargs()
        if not kw:
            return
        cmd = st.build_create_command(**kw)
        self.out.setPlainText("# 將執行的 schtasks 指令(預覽):\n" + cmd)

    # ---- 建立 ---- #
    def _create(self):
        kw = self._form_kwargs()
        if not kw:
            return
        self._log(f"建立排程:{kw['flow_name']} …")
        self._start_worker("create", kw)

    # ---- 刪除 ---- #
    def _delete(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未選取", "請先在清單中選一個任務。")
            return
        item = self.table.item(row, 0)
        tn = item.text() if item else ""
        if not tn:
            return
        if QMessageBox.question(self, "確認刪除", f"確定刪除排程任務「{tn}」?") \
                != QMessageBox.Yes:
            return
        self._log(f"刪除排程:{tn} …")
        self._start_worker("delete", {"task_name": tn})

    def _reload_tasks(self):
        self._log("讀取排程清單 …")
        self._start_worker("list", {})

    # ---- worker 管理 ---- #
    def _start_worker(self, op: str, kwargs: dict):
        if self._worker is not None and self._worker.isRunning():
            return
        self._busy(True)
        self._worker = _SchtasksWorker(op, kwargs)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, op: str, payload):
        self._busy(False)
        if op == "list":
            tasks, res = payload
            self._fill_table(tasks)
            if not res.ok and not tasks:
                self._log(f"清單:{res.message}")
            else:
                self._log(f"清單:找到 {len(tasks)} 個 RPA 排程任務。")
            return

        # create / delete
        res = payload
        if res.ok:
            self._log(f"成功:{res.message}")
            self._reload_tasks()   # 成功後刷新清單
        else:
            self._log(f"失敗:{res.message}")
            QMessageBox.warning(self, "schtasks 失敗", res.message)

    def _fill_table(self, tasks: list[dict]):
        self.table.setRowCount(len(tasks))
        for r, t in enumerate(tasks):
            self.table.setItem(r, 0, QTableWidgetItem(t.get("task_name", "")))
            self.table.setItem(r, 1, QTableWidgetItem(t.get("schedule", "")))
            self.table.setItem(r, 2, QTableWidgetItem(t.get("next_run", "")))
            self.table.setItem(r, 3, QTableWidgetItem(t.get("status", "")))
