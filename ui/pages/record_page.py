# -*- coding: utf-8 -*-
"""錄製頁 (RecordPage):錄 web / desktop 操作 → 預覽 steps → 存成流程。

UX 流程(對應 docs/phase2_spec.md §6 §7):
  1. 選引擎(web / desktop)。web 需填 URL。
  2. 按「開始錄製」→ 右下角 StatusOverlay 顯示 RECORDING 紅燈 →
     在背景執行緒呼叫對應 recorder(不卡 UI 主執行緒)。
  3. 操作目標程式 / 網頁;完成後按「停止 (F9)」。
  4. recorder.stop() 回傳 flow dict → 在 QTableWidget 預覽抓到的 steps
     (action / label / target 摘要)。
  5. 按「存成流程」→ 寫進 Store(可選同時寫一份 JSON 到 flows/ 或 recordings/)。

降級設計(關鍵):
- 錄製器由別人並行開發,目前 engines/web/recorder.py、engines/desktop/recorder.py
  可能尚未存在。所有 import / 呼叫都用 lazy import + try/except 包起來,
  缺席時 log 友善訊息、不讓 UI crash。
- 真正的「停止 → 取回 flow dict → 存進 Store」wiring 抽到模組級函式
  finalize_recording(),可被測試直接呼叫(注入假 recorder),不必真的開執行緒。
"""
from __future__ import annotations
import os
import json
import time
import threading
import datetime as dt

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QTableWidget, QTableWidgetItem, QPlainTextEdit, QHeaderView,
    QMessageBox, QAbstractItemView,
)

from core.schema import Flow
from ui.overlay import StatusOverlay
from ui.widgets import page_header, Card

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------------------- #
# 純函式:供 UI 與測試共用,完全不碰 Qt 事件迴圈 / 真錄製器。
# --------------------------------------------------------------------------- #
def _summarize_target(target: dict | None) -> str:
    """把多定位器 target 壓成一行人類可讀摘要(primary strategy:value)。"""
    if not target:
        return "-"
    primary = target.get("primary") or {}
    strat = primary.get("strategy", "?")
    val = primary.get("value", "")
    if isinstance(val, str) and len(val) > 48:
        val = val[:45] + "…"
    n_fb = len(target.get("fallbacks") or [])
    suffix = f"  (+{n_fb} fallback)" if n_fb else ""
    return f"{strat}: {val}{suffix}"


def make_default_flow_name(engine: str) -> str:
    """產生不易撞名的預設流程名稱,例 rec_web_20260608_143005。"""
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"rec_{engine}_{ts}"


def finalize_recording(
    flow_dict: dict,
    store,
    name: str | None = None,
    engine: str = "web",
    write_dir: str | None = None,
) -> Flow:
    """把 recorder 回傳的 flow dict 正規化、存進 Store(可選寫一份 JSON 檔)。

    這是「停止 → 取回 flow dict → 存進 Store」wiring 的可測試核心:
    - 補上 name / engine(recorder 沒填時)。
    - 用 Flow.from_dict 正規化(確保每個 step 有 id 等欄位)。
    - store.save_flow(flow.to_dict())。
    - write_dir 有給就另存一份 <name>.json(供 flows/ 或 recordings/ 留底)。
    回傳正規化後的 Flow。
    """
    flow_dict = dict(flow_dict or {})
    if name:
        flow_dict["name"] = name
    flow_dict.setdefault("name", make_default_flow_name(engine))
    # recorder 通常會自己填 engine;沒填或想覆寫時補上。
    flow_dict.setdefault("engine", engine)

    flow = Flow.from_dict(flow_dict)
    store.save_flow(flow.to_dict())

    if write_dir:
        os.makedirs(write_dir, exist_ok=True)
        out_path = os.path.join(write_dir, f"{flow.name}.json")
        flow.save(out_path)
    return flow


# --------------------------------------------------------------------------- #
# 錄製背景 worker:start 啟動 recorder、stop 取回 flow dict。
# 用 QThread.run 跑「啟動 + 阻塞監聽」;主執行緒只送 signal、收 signal。
# --------------------------------------------------------------------------- #
class RecordWorker(QThread):
    """背景執行緒包裝錄製器,避免阻塞 UI 主執行緒。

    web 與 desktop 兩種錄製器 API 不同(spec §6):
      - web:    record_web(url, out_flow_path) -> path(阻塞直到 codegen 視窗關閉)
      - desktop: DesktopRecorder(...).start() / .stop() -> flow dict
    這裡統一成:run() 期間進行錄製,stop_recording() 要求停止,
    錄完 emit recorded(flow_dict);失敗 emit failed(msg);過程訊息 emit log(str)。
    所有 import / 呼叫都 try/except,缺錄製器不 crash。
    """

    recorded = Signal(dict)     # 錄到的 flow dict
    failed = Signal(str)        # 友善錯誤訊息
    log = Signal(str)

    def __init__(self, engine: str, url: str, flow_name: str,
                 anchor_dir: str, recorder_factory=None, parent=None,
                 self_hwnd=None, excluded_rects=None):
        super().__init__(parent)
        self.engine = engine
        self.url = url
        self.flow_name = flow_name
        self.anchor_dir = anchor_dir
        # 測試可注入假 recorder 工廠;預設為 None → run() 內 lazy import 真錄製器。
        self._recorder_factory = recorder_factory
        # 防鬼影 / 最小化自己:錄 desktop 時把工具自己的視窗 handle 與排除矩形帶給 recorder。
        self._self_hwnd = self_hwnd
        self._excluded_rects = list(excluded_rects or [])
        self._recorder = None       # desktop recorder 物件(供 stop_recording 呼叫)
        self._out_path = os.path.join(anchor_dir, f"{flow_name}.json")
        # desktop 錄製器 start() 是非阻塞(背景 listener),需自己等到使用者要求停止。
        self._stop_requested = threading.Event()

    def stop_recording(self):
        """要求停止錄製(F9 / 停止鈕)。

        - desktop:set 旗標讓 _run_desktop 的等待迴圈退出,再呼叫 recorder.stop()。
        - web:codegen 由使用者關閉視窗結束,這裡只記 log。
        """
        self.log.emit("已要求停止錄製…")
        self._stop_requested.set()
        rec = self._recorder
        # 真實 DesktopRecorder 帶 stop_event(F9 也會 set);提前 set 讓 listener 收手。
        ev = getattr(rec, "stop_event", None)
        if ev is not None:
            try:
                ev.set()
            except Exception:  # noqa: BLE001
                pass

    def run(self):  # QThread 進入點
        try:
            if self.engine == "web":
                self._run_web()
            else:
                self._run_desktop()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self._friendly_err(e))

    # ---- web:Playwright codegen ---- #
    def _run_web(self):
        factory = self._recorder_factory
        try:
            if factory is None:
                from engines.web.recorder import record_web  # lazy:缺錄製器才在此爆
                factory = record_web
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self._friendly_err(e))
            return

        self.log.emit(f"啟動 web 錄製器(codegen),目標 URL:{self.url or '(未指定)'}")
        os.makedirs(self.anchor_dir, exist_ok=True)
        # record_web 阻塞直到使用者關閉 codegen 視窗,回傳 flow JSON 路徑。
        path = factory(self.url, self._out_path)
        flow_dict = self._load_flow_dict(path)
        self.recorded.emit(flow_dict)

    # ---- desktop:pynput + UIA + anchor ---- #
    def _run_desktop(self):
        factory = self._recorder_factory
        try:
            if factory is None:
                from engines.desktop.recorder import DesktopRecorder  # lazy
                factory = DesktopRecorder
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self._friendly_err(e))
            return

        os.makedirs(self.anchor_dir, exist_ok=True)
        self.log.emit("啟動 desktop 錄製器(監聽鍵鼠;點擊抓 UIA + anchor + 座標)。")
        # 真 DesktopRecorder 支援 self_hwnd / excluded_rects(最小化自己 + 防鬼影);
        # 測試注入的假工廠可能不吃這些 kwargs,失敗就退回基本簽名。
        try:
            self._recorder = factory(
                self.flow_name, self.anchor_dir,
                self_hwnd=self._self_hwnd,
                excluded_rects=self._excluded_rects,
            )
        except TypeError:
            self._recorder = factory(self.flow_name, self.anchor_dir)
        # DesktopRecorder.start() 非阻塞(背景 listener thread);這裡要等到使用者
        # 要求停止(停止鈕 / F9),期間 listener 持續抓步驟。
        self._recorder.start()
        # 等待:停止鈕(_stop_requested)或 F9 設了 recorder.stop_event 都算停止訊號。
        rec_ev = getattr(self._recorder, "stop_event", None)
        while not self._stop_requested.is_set():
            if rec_ev is not None and rec_ev.is_set():
                break
            time.sleep(0.1)
        flow_dict = self._recorder.stop()
        if not isinstance(flow_dict, dict):
            flow_dict = self._load_flow_dict(flow_dict)
        self.recorded.emit(flow_dict)

    # ---- 共用小工具 ---- #
    def _load_flow_dict(self, path_or_dict):
        if isinstance(path_or_dict, dict):
            return path_or_dict
        with open(path_or_dict, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _friendly_err(self, e: Exception) -> str:
        return (
            f"無法啟動 {self.engine} 錄製器:{type(e).__name__}: {e}\n"
            f"(錄製器可能尚未安裝或仍在開發中;請確認 "
            f"engines/{self.engine}/recorder.py 與相依套件 "
            f"[web: playwright;desktop: pynput / pywinauto / mss / pillow]。)"
        )


# --------------------------------------------------------------------------- #
# 錄製頁
# --------------------------------------------------------------------------- #
class RecordPage(QWidget):
    def __init__(self, store, recordings_dir: str | None = None, parent=None):
        super().__init__(parent)
        self.store = store
        self.recordings_dir = recordings_dir or os.path.join(_ROOT, "recordings")
        self.worker: RecordWorker | None = None
        self.overlay = StatusOverlay()
        self._recorded_flow: dict | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, title, hint = page_header(
            "錄製流程",
            "選引擎後按「開始錄製」,右下角會亮起 RECORDING 紅燈;"
            "操作完成後按「停止 (F9)」即可預覽抓到的步驟,再存成流程。")
        root.addWidget(header)

        setup_card = Card()
        # ---- 設定列:引擎 / URL ---- #
        bar = QHBoxLayout()
        bar.setSpacing(8)
        eng_lbl = QLabel("引擎:"); eng_lbl.setObjectName("FieldLabel")
        bar.addWidget(eng_lbl)
        self.combo_engine = QComboBox()
        self.combo_engine.addItem("Web(瀏覽器)", "web")
        self.combo_engine.addItem("Desktop(桌面)", "desktop")
        bar.addWidget(self.combo_engine)
        url_lbl = QLabel("起始 URL:"); url_lbl.setObjectName("FieldLabel")
        bar.addWidget(url_lbl)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com(web 錄製用)")
        bar.addWidget(self.url_edit, 1)
        setup_card.body.addLayout(bar)

        # ---- 流程名稱 ---- #
        name_bar = QHBoxLayout()
        name_bar.setSpacing(8)
        name_lbl = QLabel("流程名稱:"); name_lbl.setObjectName("FieldLabel")
        name_bar.addWidget(name_lbl)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("留空則自動命名(例 rec_web_時間戳)")
        name_bar.addWidget(self.name_edit, 1)
        setup_card.body.addLayout(name_bar)

        # ---- 控制鈕 ---- #
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        self.btn_start = QPushButton("⏺  開始錄製")
        self.btn_stop = QPushButton("■  停止 (F9)")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        self.btn_save = QPushButton("💾  存成流程")
        self.btn_save.setObjectName("Ghost")
        self.btn_save.setEnabled(False)
        ctrl.addWidget(self.btn_start)
        ctrl.addWidget(self.btn_stop)
        ctrl.addStretch(1)
        ctrl.addWidget(self.btn_save)
        setup_card.body.addLayout(ctrl)

        status_row = QHBoxLayout()
        self.status_label = QLabel("待命")
        self.status_label.setObjectName("StatusBadge")
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        setup_card.body.addLayout(status_row)
        root.addWidget(setup_card)

        # ---- 預覽表格 ---- #
        preview_card = Card(margins=(10, 10, 10, 10))
        prev_lbl = QLabel("錄到的步驟預覽")
        prev_lbl.setObjectName("SectionLabel")
        preview_card.body.addWidget(prev_lbl)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["動作 action", "標籤 label", "目標 target"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        preview_card.body.addWidget(self.table, 1)
        root.addWidget(preview_card, 1)

        # ---- 日誌 ---- #
        log_card = Card(margins=(14, 12, 14, 12))
        log_lbl = QLabel("錄製日誌")
        log_lbl.setObjectName("SectionLabel")
        log_card.body.addWidget(log_lbl)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumHeight(120)
        log_card.body.addWidget(self.logbox)
        root.addWidget(log_card)

        self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        self.btn_start.clicked.connect(self.start_recording)
        self.btn_stop.clicked.connect(self.stop_recording)
        self.btn_save.clicked.connect(self.save_flow)
        self._on_engine_changed()

    # ---- 引擎切換:desktop 不需 URL ---- #
    def _on_engine_changed(self):
        is_web = self.combo_engine.currentData() == "web"
        self.url_edit.setEnabled(is_web)

    def _append(self, text: str):
        self.logbox.appendPlainText(text)

    # ---- F9 快捷鍵:錄製中按 F9 等同停止 ---- #
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_F9 and self.btn_stop.isEnabled():
            self.stop_recording()
        else:
            super().keyPressEvent(e)

    # ---- 開始錄製 ---- #
    # 注意:btn_start.clicked 會帶一個 checked(bool)位置參數,故第一參數要能吃掉它。
    def start_recording(self, _checked=False, *, recorder_factory=None):
        engine = self.combo_engine.currentData()
        url = self.url_edit.text().strip()
        if engine == "web" and not url:
            QMessageBox.warning(self, "缺少 URL", "Web 錄製需要填入起始 URL。")
            return

        name = self.name_edit.text().strip() or make_default_flow_name(engine)
        self.name_edit.setText(name)
        anchor_dir = os.path.join(self.recordings_dir, f"{name}_anchors")

        self.logbox.clear()
        self.table.setRowCount(0)
        self._recorded_flow = None
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_save.setEnabled(False)
        self.status_label.setText("狀態:錄製中…")

        # overlay RECORDING 紅燈
        self.overlay.set_state("recording")
        self.overlay.set_hint("錄製中:操作完成後按 F9 / 停止")
        self._position_overlay()
        self.overlay.show()

        # desktop 錄製:把工具自己的主視窗 handle 與 overlay 矩形帶給 worker,
        # 讓 recorder 最小化自己、並把這些區域排除(防鬼影)。graceful:取不到就 None。
        self_hwnd, excluded = self._self_window_info() if engine == "desktop" else (None, [])

        self.worker = RecordWorker(
            engine=engine, url=url, flow_name=name,
            anchor_dir=anchor_dir, recorder_factory=recorder_factory,
            self_hwnd=self_hwnd, excluded_rects=excluded,
        )
        self.worker.log.connect(self._append)
        self.worker.recorded.connect(self._on_recorded)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    # ---- 停止錄製 ---- #
    def stop_recording(self):
        self.btn_stop.setEnabled(False)
        self.status_label.setText("狀態:停止中,正在彙整步驟…")
        if self.worker is not None:
            self.worker.stop_recording()

    # ---- 錄製完成回呼(主執行緒)---- #
    def _on_recorded(self, flow_dict: dict):
        self._recorded_flow = flow_dict or {}
        self._populate_preview(self._recorded_flow)
        n = len(self._recorded_flow.get("steps", []))
        self.status_label.setText(f"狀態:錄製完成,共 {n} 個步驟。檢查無誤後可存成流程。")
        self._append(f"錄製完成,抓到 {n} 個步驟。")
        self.btn_start.setEnabled(True)
        self.btn_save.setEnabled(n > 0)
        self.overlay.set_state("idle")
        self.overlay.hide()

    def _on_failed(self, msg: str):
        self._append(msg)
        self.status_label.setText("狀態:錄製失敗(詳見日誌)。")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_save.setEnabled(False)
        self.overlay.set_state("idle")
        self.overlay.hide()

    def _populate_preview(self, flow_dict: dict):
        steps = flow_dict.get("steps", []) or []
        self.table.setRowCount(len(steps))
        for r, s in enumerate(steps):
            self.table.setItem(r, 0, QTableWidgetItem(str(s.get("action", ""))))
            self.table.setItem(r, 1, QTableWidgetItem(str(s.get("label", ""))))
            self.table.setItem(r, 2, QTableWidgetItem(_summarize_target(s.get("target"))))

    # ---- 存成流程 ---- #
    def save_flow(self):
        if not self._recorded_flow:
            QMessageBox.information(self, "沒有可存的內容", "尚未錄到任何步驟。")
            return
        engine = self.combo_engine.currentData()
        name = self.name_edit.text().strip() or None
        try:
            flow = finalize_recording(
                self._recorded_flow, self.store,
                name=name, engine=engine, write_dir=self.recordings_dir,
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "存檔失敗", f"{type(e).__name__}: {e}")
            self._append(f"存檔失敗:{type(e).__name__}: {e}")
            return
        self._append(
            f"已存成流程「{flow.name}」(engine={flow.engine}),"
            f"並寫入 {self.recordings_dir}\\{flow.name}.json。"
        )
        self.status_label.setText(f"狀態:已存成流程「{flow.name}」。")
        self.btn_save.setEnabled(False)
        QMessageBox.information(self, "已存檔", f"流程「{flow.name}」已存入。")

    def _self_window_info(self):
        """取得工具自己的主視窗 HWND 與要排除的螢幕矩形(防鬼影 + 最小化)。

        回傳 (hwnd_or_None, excluded_rects)。全程 graceful:取不到就回 (None, [])。
        excluded_rects 含主視窗矩形與 overlay 矩形(若可取得),避免錄到工具自己。
        """
        hwnd = None
        rects: list = []
        try:
            from core import window as _win
            top = self.window()
            hwnd = _win.hwnd_from_qt_widget(top)
            # 注意:錄製時主視窗會被「最小化」,所以**不要**把主視窗整塊矩形排除——
            # 否則最小化後使用者點目標 App(常落在原主視窗區域)會被當成「點到工具自己」
            # 而略過,造成「只錄到視窗區域外的點擊」(本 bug 的根因)。
            # 只排除錄製期間仍置頂可見的 overlay(RECORDING 紅燈)。
            ov_h = _win.hwnd_from_qt_widget(self.overlay)
            if ov_h is not None:
                r2 = _win.get_window_rect(ov_h)
                if r2:
                    rects.append(r2)
        except Exception:  # noqa: BLE001
            pass
        return hwnd, rects

    def _position_overlay(self):
        try:
            screen = self.screen() or self.window().screen()
            geo = screen.availableGeometry()
            self.overlay.move(geo.right() - self.overlay.width() - 24,
                              geo.bottom() - self.overlay.height() - 24)
        except Exception:
            pass

    def refresh(self):
        # 與其他頁一致;錄製頁目前不需從 Store 重載。
        pass
