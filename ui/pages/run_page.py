# -*- coding: utf-8 -*-
"""執行頁:選 flow → Run / Stop → 進度條 + 即時 log。

所有執行都丟給 RunWorker(QThread),UI 主執行緒只接 signal 更新,不會卡住。
搭配 StatusOverlay 在螢幕上顯示 PLAYING 燈與目前步數(執行狀態燈場景)。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QProgressBar, QPlainTextEdit,
)

from core.schema import Flow
from ui.run_worker import RunWorker
from ui.overlay import StatusOverlay
from ui.widgets import page_header, Card


class RunPage(QWidget):
    def __init__(self, store, vault, parent=None):
        super().__init__(parent)
        self.store = store
        self.vault = vault
        self.worker: RunWorker | None = None
        self.overlay = StatusOverlay()

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, title, hint = page_header(
            "執行流程",
            "選一條流程後按「執行」;執行狀態會顯示在右下角的置頂狀態燈。")
        root.addWidget(header)

        ctrl_card = Card()
        bar = QHBoxLayout()
        bar.setSpacing(8)
        flow_lbl = QLabel("流程:")
        flow_lbl.setObjectName("FieldLabel")
        self.combo = QComboBox()
        self.btn_refresh = QPushButton("↻  重新整理")
        self.btn_refresh.setObjectName("Ghost")
        self.btn_run = QPushButton("▶  執行")
        self.btn_stop = QPushButton("■  停止")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        bar.addWidget(flow_lbl)
        bar.addWidget(self.combo, 1)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_run)
        bar.addWidget(self.btn_stop)
        ctrl_card.body.addLayout(bar)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        ctrl_card.body.addWidget(self.progress)

        status_row = QHBoxLayout()
        self.status_label = QLabel("待命")
        self.status_label.setObjectName("StatusBadge")
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        ctrl_card.body.addLayout(status_row)

        # ---- MFA 人工介入橫幅(平時隱藏)---- #
        self.pause_banner = QWidget()
        self.pause_banner.setObjectName("PauseBanner")
        pb = QHBoxLayout(self.pause_banner)
        pb.setContentsMargins(14, 10, 14, 10)
        self.pause_label = QLabel("⏸ 等待人工(完成 MFA / 驗證後按繼續)")
        self.pause_label.setObjectName("PauseBannerText")
        self.pause_label.setWordWrap(True)
        self.btn_resume = QPushButton("繼續")
        pb.addWidget(self.pause_label, 1)
        pb.addWidget(self.btn_resume)
        self.pause_banner.setVisible(False)
        ctrl_card.body.addWidget(self.pause_banner)
        self.btn_resume.clicked.connect(self._resume_run)
        root.addWidget(ctrl_card)

        log_card = Card()
        log_lbl = QLabel("即時日誌")
        log_lbl.setObjectName("SectionLabel")
        log_card.body.addWidget(log_lbl)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        log_card.body.addWidget(self.logbox, 1)
        root.addWidget(log_card, 1)

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_run.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)

        self.refresh()

    def refresh(self):
        current = self.combo.currentText()
        self.combo.clear()
        for row in self.store.list_flows():
            self.combo.addItem(row["name"])
        if current:
            idx = self.combo.findText(current)
            if idx >= 0:
                self.combo.setCurrentIndex(idx)

    def _append(self, text: str):
        self.logbox.appendPlainText(text)

    def _set_status(self, text: str, kind: str = "idle"):
        """更新狀態徽章文字與配色。kind: idle/running/ok/bad/paused。"""
        obj = {
            "idle": "StatusBadge",
            "running": "StatusBadgeRunning",
            "ok": "StatusBadgeOk",
            "bad": "StatusBadgeBad",
            "paused": "StatusBadgePaused",
        }.get(kind, "StatusBadge")
        self.status_label.setText(text)
        if self.status_label.objectName() != obj:
            self.status_label.setObjectName(obj)
            # 重新套用 QSS(objectName 變更後需 polish)
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)

    def start_run(self):
        name = self.combo.currentText()
        if not name:
            self._append("請先選擇一條流程。")
            return
        d = self.store.load_flow(name)
        if not d:
            self._append(f"找不到流程:{name}")
            return
        flow = Flow.from_dict(d)

        self.logbox.clear()
        self.progress.setValue(0)
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._set_status("● 執行中", "running")
        self.overlay.set_state("playing")
        self.overlay.set_step(0, len(flow.steps))
        self._position_overlay()
        self.overlay.show()

        self.pause_banner.setVisible(False)

        self.worker = RunWorker(flow, self.store, self.vault)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._append)
        self.worker.finished.connect(self._on_finished)
        self.worker.paused.connect(self._on_paused)
        self.worker.resumed.connect(self._on_resumed)
        self.worker.start()

    def stop_run(self):
        if self.worker is not None:
            self.worker.request_stop()
            self.btn_stop.setEnabled(False)

    # ---- MFA 暫停 / 繼續 ---- #
    def _on_paused(self, message: str):
        msg = message or "完成 MFA / 驗證後按繼續"
        self.pause_label.setText(f"⏸ 等待人工:{msg}")
        self.pause_banner.setVisible(True)
        self.btn_resume.setEnabled(True)
        self._set_status("⏸ 暫停中(等待人工完成 MFA / 驗證)", "paused")
        self.overlay.set_state("paused")
        self.overlay.set_hint("等待人工:完成驗證後回 RPA Studio 按「繼續」")

    def _on_resumed(self):
        self.pause_banner.setVisible(False)
        self._set_status("● 執行中", "running")
        self.overlay.set_state("playing")
        self.overlay.set_hint("按 F9 或 Stop 可中止")

    def _resume_run(self):
        self.btn_resume.setEnabled(False)
        if self.worker is not None:
            self.worker.resume()

    def _on_progress(self, i, total, label):
        if total > 0:
            self.progress.setMaximum(total)
            self.progress.setValue(i)
        self._set_status(f"● 執行中 — 第 {i}/{total} 步:{label}", "running")
        self.overlay.set_step(i, total)

    def _on_finished(self, result):
        status_map = {"completed": "完成", "stopped": "已停止", "failed": "失敗"}
        zh = status_map.get(result.status, result.status)
        icon = {"completed": "✓", "stopped": "■", "failed": "✕"}.get(result.status, "•")
        kind = {"completed": "ok", "stopped": "idle", "failed": "bad"}.get(result.status, "idle")
        self._set_status(
            f"{icon} {zh}（成功 {result.steps_ok} / 失敗 {result.steps_failed}"
            f" / 共 {result.steps_total}）",
            kind,
        )
        self._append(f"=== 執行結束:{zh} ===")
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.pause_banner.setVisible(False)
        self.overlay.set_state("idle")
        self.overlay.hide()
        self.worker = None

    def _position_overlay(self):
        try:
            screen = self.screen() or self.window().screen()
            geo = screen.availableGeometry()
            self.overlay.move(geo.right() - self.overlay.width() - 24,
                              geo.bottom() - self.overlay.height() - 24)
        except Exception:
            pass
