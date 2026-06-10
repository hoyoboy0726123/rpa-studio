# -*- coding: utf-8 -*-
"""流程圖視覺化編輯器頁 (GraphPage)。

把選定 flow 的「扁平 step 串列」畫成節點圖(QGraphicsView/QGraphicsScene):
  - 每個 step = 一個節點方塊(序號 + action + label;依 engine 類別給不同顏色;
    flow.if / flow.loop 用特殊外觀)。
  - 連線:預設「上一步 → 下一步」順序箭頭(由上而下自動佈局)。
  - 特殊邊:
      * flow.if:依 params.skip_count 畫一條「不成立時跳過 N 步」的略過邊。
      * flow.loop:依 params.body_count 畫迴圈回邊(body 的最後一步 → loop 節點)。
      * on_error=goto:<id>:畫一條標示「錯誤跳轉」的邊到目標節點。
  - 互動:
      * 點節點 → 載入右側面板編輯該 step(重用 ui.flow_edit_ops 的純函式)。
      * 工具列:＋新增節點 / 刪除 / 連 goto(選兩節點設 on_error=goto)/ 套用面板 / 存檔。
      * 拖拉節點放開後依垂直位置重排 flat steps(走 flow_edit_ops,底層永遠是 flat 串列)。
      * 滾輪縮放、拖曳平移(ScrollHandDrag)。
  - 存回:用 flow_edit_ops.save_flow_to_store;**底層永遠是 flat step 串列**,
    圖只是視圖,改圖即改 flat steps。

設計重點 — 「圖 ↔ 扁平模型」雙向對應:
  build_graph_model(flow) 是不依賴 Qt 的純函式,把 Flow 編譯成
  GraphModel(nodes + edges),供畫布渲染與測試斷言共用。節點 index 直接對應
  flow.steps 的索引,因此「拖拉重排 / 新增 / 刪除 / 設 goto」全部回落到對 flat
  steps 的索引操作(flow_edit_ops),再 rebuild 一次圖即可。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.schema import Flow, ACTION_CATALOG


# =========================================================================== #
# 純資料模型:Flow → GraphModel(不依賴 Qt,可被測試直接斷言)
# =========================================================================== #
# 邊的種類
EDGE_SEQ = "seq"        # 順序邊:上一步 → 下一步
EDGE_SKIP = "skip"      # flow.if 不成立時的略過邊
EDGE_LOOP = "loop"      # flow.loop 的迴圈回邊
EDGE_GOTO = "goto"      # on_error=goto:<id> 的錯誤跳轉邊


@dataclass
class GraphNode:
    """一個節點,對應 flow.steps[index]。"""
    index: int
    step_id: str
    action: str
    label: str
    category: str          # web | desktop | flow | other
    kind: str              # normal | if | loop
    title: str             # 顯示用標題(序號 + action)
    subtitle: str          # 顯示用副標(label)


@dataclass
class GraphEdge:
    """一條邊,用節點 index 表示來源 / 目標。"""
    src: int
    dst: int
    kind: str              # seq | skip | loop | goto
    text: str = ""         # 邊上的標籤(略過 N 步 / 迴圈 / goto)


@dataclass
class GraphModel:
    nodes: list = field(default_factory=list)   # list[GraphNode]
    edges: list = field(default_factory=list)   # list[GraphEdge]

    def edges_of(self, kind: str) -> list:
        return [e for e in self.edges if e.kind == kind]


# action → 類別(取 "web.click" 的 "web";flow.* 再細分 if/loop)
_CATEGORY_KEYS = set(ACTION_CATALOG.keys())   # {"web","desktop","flow"}


def step_category(action: str) -> str:
    """由 action 前綴推類別。未知前綴歸 'other'。"""
    head = (action or "").split(".", 1)[0]
    return head if head in _CATEGORY_KEYS else "other"


def step_kind(action: str) -> str:
    """特殊節點種類:flow.if → 'if';flow.loop → 'loop';其餘 'normal'。"""
    if action == "flow.if":
        return "if"
    if action == "flow.loop":
        return "loop"
    return "normal"


def _loop_body_count(step) -> int:
    """flow.loop 的 body_count(同時容忍舊鍵 count_body)。"""
    p = step.params or {}
    try:
        return int(p.get("body_count", p.get("count_body", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def _if_skip_count(step) -> int:
    p = step.params or {}
    try:
        return int(p.get("skip_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def build_graph_model(flow: Flow) -> GraphModel:
    """把一條 Flow 編譯成 GraphModel(節點 + 各類邊)。純函式,供畫布與測試共用。

    產生的邊:
      - EDGE_SEQ : 每相鄰兩步一條(i → i+1)。
      - EDGE_SKIP: flow.if 依 skip_count,從 if 節點指向「跳過後的落點」
                   (i + 1 + skip_count;超界則夾到最後一個節點之後不畫,落在最後)。
      - EDGE_LOOP: flow.loop 依 body_count,從 body 最後一步指回 loop 節點
                   (body 範圍 = (i, i+body_count])。
      - EDGE_GOTO: 任一 step 的 on_error=goto:<id>,從該步指向目標 step。
    """
    steps = list(flow.steps if flow else [])
    n = len(steps)
    id_to_index = {s.id: i for i, s in enumerate(steps)}

    nodes: list = []
    for i, s in enumerate(steps):
        cat = step_category(s.action)
        kind = step_kind(s.action)
        lbl = s.label or ""
        nodes.append(GraphNode(
            index=i, step_id=s.id, action=s.action, label=lbl,
            category=cat, kind=kind,
            title=f"{i + 1}. {s.action}",
            subtitle=lbl,
        ))

    edges: list = []

    # 順序邊
    for i in range(n - 1):
        edges.append(GraphEdge(src=i, dst=i + 1, kind=EDGE_SEQ))

    for i, s in enumerate(steps):
        # flow.if 略過邊
        if s.action == "flow.if":
            skip = _if_skip_count(s)
            if skip > 0:
                dst = i + 1 + skip
                if dst > n - 1:
                    dst = n - 1            # 夾到最後一個節點(略過剩餘全部)
                if dst != i:
                    edges.append(GraphEdge(
                        src=i, dst=dst, kind=EDGE_SKIP,
                        text=f"不成立 → 跳過 {skip} 步"))

        # flow.loop 迴圈回邊(body 最後一步 → loop 節點)
        if s.action == "flow.loop":
            body = _loop_body_count(s)
            if body > 0:
                last = i + body
                if last > n - 1:
                    last = n - 1
                if last > i:
                    edges.append(GraphEdge(
                        src=last, dst=i, kind=EDGE_LOOP,
                        text=f"迴圈 body={body}"))

        # on_error=goto:<id> 錯誤跳轉邊
        oe = s.on_error or ""
        if oe.startswith("goto:"):
            target = oe.split(":", 1)[1].strip()
            if target in id_to_index:
                edges.append(GraphEdge(
                    src=i, dst=id_to_index[target], kind=EDGE_GOTO,
                    text="錯誤跳轉 goto"))

    return GraphModel(nodes=nodes, edges=edges)


def reorder_by_y(flow: Flow, order_index_by_y: list[int]) -> Flow:
    """依「節點目前由上到下的視覺順序」重排 flat steps。

    order_index_by_y:把節點「依 y 座標排序後」的原始 index 串列。
      例 [2,0,1] 代表原本 index 2 的 step 現在排最上面。
    就地重排 flow.steps 並回傳 flow。非法 / 不是完整排列時不動(回原 flow)。
    """
    steps = flow.steps
    n = len(steps)
    if sorted(order_index_by_y) != list(range(n)):
        return flow            # 不是 0..n-1 的完整排列,視為無效,安全不動
    flow.steps = [steps[i] for i in order_index_by_y]
    return flow


# =========================================================================== #
# Qt 視圖層
# =========================================================================== #
from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QLineF
from PySide6.QtGui import QColor, QPen, QBrush, QPainter, QFont, QPainterPath, QPolygonF
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QSpinBox, QFormLayout, QGroupBox, QSplitter, QMessageBox,
    QGraphicsView, QGraphicsScene, QGraphicsItem, QGraphicsObject,
    QGraphicsPathItem, QGraphicsSimpleTextItem, QScrollArea, QPlainTextEdit,
)

from ui import flow_edit_ops as ops
from ui.widgets import page_header, Card
from ui import style as S

_ON_ERROR = ["abort", "continue", "goto:"]

# 類別 → (邊框色, 底色)
_CAT_COLORS = {
    "web":     ("#2563eb", "#dbeafe"),
    "desktop": ("#7c3aed", "#ede9fe"),
    "flow":    ("#0f766e", "#ccfbf1"),
    "other":   ("#475569", "#e2e8f0"),
}
# 特殊節點覆寫(if/loop)
_KIND_COLORS = {
    "if":   ("#b45309", "#fef3c7"),
    "loop": ("#9d174d", "#fce7f3"),
}
# 邊樣式 → (顏色, 是否虛線, 標籤)
_EDGE_STYLE = {
    EDGE_SEQ:  ("#94a3b8", False),
    EDGE_SKIP: ("#b45309", True),
    EDGE_LOOP: ("#9d174d", True),
    EDGE_GOTO: (S.DANGER, True),
}

_NODE_W = 220
_NODE_H = 58
_V_GAP = 42         # 節點垂直間距(box 之間的空白)
_TOP = 30
_LEFT = 40


class _NodeItem(QGraphicsObject):
    """一個 step 節點方塊。可拖拉、可點選。"""

    clicked = Signal(int)          # 帶 node index

    def __init__(self, node: GraphNode):
        super().__init__()
        self.node = node
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setZValue(2)
        self._selected = False

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, _NODE_W, _NODE_H)

    def center_scene(self) -> QPointF:
        return self.mapToScene(QPointF(_NODE_W / 2, _NODE_H / 2))

    def top_center(self) -> QPointF:
        return self.mapToScene(QPointF(_NODE_W / 2, 0))

    def bottom_center(self) -> QPointF:
        return self.mapToScene(QPointF(_NODE_W / 2, _NODE_H))

    def right_center(self) -> QPointF:
        return self.mapToScene(QPointF(_NODE_W, _NODE_H / 2))

    def left_center(self) -> QPointF:
        return self.mapToScene(QPointF(0, _NODE_H / 2))

    def set_marked(self, on: bool):
        self._selected = on
        self.update()

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        border, fill = _CAT_COLORS.get(self.node.category, _CAT_COLORS["other"])
        if self.node.kind in _KIND_COLORS:
            border, fill = _KIND_COLORS[self.node.kind]
        rect = QRectF(1, 1, _NODE_W - 2, _NODE_H - 2)

        pen = QPen(QColor(border))
        pen.setWidth(3 if (self.isSelected() or self._selected) else 1.5)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(fill)))

        if self.node.kind == "if":
            # 菱形感:用圓角但較粗框 + 標記
            painter.drawRoundedRect(rect, 14, 14)
        elif self.node.kind == "loop":
            painter.drawRoundedRect(rect, 22, 22)
        else:
            painter.drawRoundedRect(rect, 9, 9)

        # 標題
        painter.setPen(QColor(S.INK))
        f = QFont(); f.setBold(True); f.setPointSize(10)
        painter.setFont(f)
        prefix = ""
        if self.node.kind == "if":
            prefix = "◇ "
        elif self.node.kind == "loop":
            prefix = "↻ "
        painter.drawText(QRectF(12, 6, _NODE_W - 24, 22),
                         Qt.AlignVCenter | Qt.AlignLeft,
                         prefix + self.node.title)
        # 副標(label)
        painter.setPen(QColor(S.MUTED))
        f2 = QFont(); f2.setPointSize(9)
        painter.setFont(f2)
        sub = self.node.subtitle or "(無標籤)"
        painter.drawText(QRectF(12, 28, _NODE_W - 24, 24),
                         Qt.AlignVCenter | Qt.AlignLeft, sub)

    def mousePressEvent(self, event):
        self.clicked.emit(self.node.index)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        # 通知 scene/page:拖拉結束,可能要依 y 重排
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        page = getattr(view, "_graph_page", None) if view else None
        if page is not None:
            page._on_node_drag_finished()


class GraphPage(QWidget):
    """流程圖視覺化編輯器。可用 GraphPage(store) 單獨建立(供測試)。"""

    flows_changed = Signal()

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.flow: Flow | None = None
        self.model: GraphModel | None = None
        self._node_items: list[_NodeItem] = []     # index 對應 flow.steps
        self._current_index: int = -1
        self._goto_pick: list[int] = []            # 連 goto 模式下已選的兩個 index

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, _, _ = page_header(
            "流程圖編輯器",
            "把流程以節點圖呈現:順序箭頭、if 略過邊、loop 回邊、goto 錯誤跳轉。"
            "點節點可編輯、拖拉可重排,底層永遠是扁平步驟串列。")
        root.addWidget(header)

        # ---- 頂列:選 flow + 工具列 ---- #
        topcard = Card(margins=(14, 12, 14, 12))
        topbar = QHBoxLayout()
        topbar.setSpacing(8)
        flow_lbl = QLabel("流程:")
        flow_lbl.setObjectName("FieldLabel")
        topbar.addWidget(flow_lbl)
        self.combo_flow = QComboBox()
        self.combo_flow.setMinimumWidth(200)
        topbar.addWidget(self.combo_flow)
        self.btn_reload = QPushButton("↻ 重新載入")
        self.btn_reload.setObjectName("Ghost")
        topbar.addWidget(self.btn_reload)
        topbar.addStretch(1)
        self.btn_add = QPushButton("＋ 新增節點")
        self.btn_del = QPushButton("刪除")
        self.btn_del.setObjectName("Danger")
        self.btn_goto = QPushButton("🔗 連 goto")
        self.btn_goto.setObjectName("Ghost")
        self.btn_goto.setCheckable(True)
        self.btn_save = QPushButton("💾 存檔")
        for b in (self.btn_add, self.btn_del, self.btn_goto, self.btn_save):
            topbar.addWidget(b)
        topcard.body.addLayout(topbar)
        root.addWidget(topcard)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(14)

        # ---- 左:畫布 ---- #
        left = Card(margins=(8, 8, 8, 8))
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing, True)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.view._graph_page = self            # 讓 node item 找得到 page
        self.view.wheelEvent = self._view_wheel_event
        left.body.addWidget(self.view, 1)
        hint = QLabel("滾輪縮放 · 空白處拖曳平移 · 拖動節點放開後依垂直位置重排")
        hint.setObjectName("PageHint")
        left.body.addWidget(hint)
        split.addWidget(left)

        # ---- 右:選定 step 編輯面板(重用 editor 欄位邏輯)---- #
        self.panel = self._build_panel()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.panel)
        scroll.setMinimumWidth(300)
        split.addWidget(scroll)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([720, 320])
        root.addWidget(split, 1)

        # ---- 訊號 ---- #
        self.combo_flow.currentIndexChanged.connect(self._on_flow_changed)
        self.btn_reload.clicked.connect(self._reload_current)
        self.btn_add.clicked.connect(self._add_node)
        self.btn_del.clicked.connect(self._delete_node)
        self.btn_goto.toggled.connect(self._on_goto_toggled)
        self.btn_save.clicked.connect(self._save)

        self.refresh()

    # ------------------------------------------------------------------ #
    # 右側編輯面板(重用 editor 的欄位語意)
    # ------------------------------------------------------------------ #
    def _build_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        gb = QGroupBox("選定步驟")
        form = QFormLayout(gb)
        self.cb_action = QComboBox()
        self.cb_action.setEditable(True)
        self.ed_label = QLineEdit()
        self.cb_on_error = QComboBox()
        self.cb_on_error.addItems(_ON_ERROR)
        self.ed_goto = QLineEdit()
        self.ed_goto.setPlaceholderText("on_error=goto: 時填目標 step id")
        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(0, 600000)
        self.sp_timeout.setSingleStep(1000)
        self.sp_timeout.setSuffix(" ms")
        form.addRow("動作 action", self.cb_action)
        form.addRow("標籤 label", self.ed_label)
        form.addRow("錯誤處理 on_error", self.cb_on_error)
        form.addRow("goto 目標", self.ed_goto)
        form.addRow("逾時 timeout", self.sp_timeout)
        lay.addWidget(gb)

        # 控制流參數(if / loop)— 直接編輯關鍵 params
        gb_ctrl = QGroupBox("控制流參數(if / loop)")
        cform = QFormLayout(gb_ctrl)
        self.ed_if_var = QLineEdit()
        self.cb_if_op = QComboBox()
        self.cb_if_op.addItems(["eq", "ne", "contains", "empty", "not_empty", "gt", "lt"])
        self.ed_if_value = QLineEdit()
        self.sp_skip = QSpinBox(); self.sp_skip.setRange(0, 999)
        self.sp_loop_count = QSpinBox(); self.sp_loop_count.setRange(0, 99999)
        self.sp_body = QSpinBox(); self.sp_body.setRange(0, 999)
        cform.addRow("if var", self.ed_if_var)
        cform.addRow("if op", self.cb_if_op)
        cform.addRow("if value", self.ed_if_value)
        cform.addRow("if skip_count", self.sp_skip)
        cform.addRow("loop count", self.sp_loop_count)
        cform.addRow("loop body_count", self.sp_body)
        lay.addWidget(gb_ctrl)

        # 其餘 params(只讀預覽,複雜編輯仍可去表單編輯器)
        gb_params = QGroupBox("其他參數 params(預覽)")
        play = QVBoxLayout(gb_params)
        self.params_preview = QPlainTextEdit()
        self.params_preview.setReadOnly(True)
        self.params_preview.setFixedHeight(120)
        play.addWidget(self.params_preview)
        lay.addWidget(gb_params)

        self.btn_apply = QPushButton("套用到此節點")
        self.btn_apply.clicked.connect(self._apply_panel)
        lay.addWidget(self.btn_apply)

        lay.addStretch(1)
        w.setEnabled(False)
        self._panel = w
        return w

    # ------------------------------------------------------------------ #
    # flow 載入 / 刷新
    # ------------------------------------------------------------------ #
    def refresh(self):
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
        if self.combo_flow.count() > 0:
            self.load_flow(self.combo_flow.currentText())
        else:
            self.flow = None
            self.scene.clear()
            self._node_items = []
            self._panel.setEnabled(False)

    def _on_flow_changed(self, _idx):
        self.load_flow(self.combo_flow.currentText())

    def _reload_current(self):
        self.load_flow(self.combo_flow.currentText())

    def load_flow(self, name: str):
        """載入指定 flow 並重畫整張圖。"""
        if not name:
            return
        d = self.store.load_flow(name)
        if not d:
            return
        self.flow = Flow.from_dict(d)
        self._populate_action_combo()
        self._current_index = -1
        self._goto_pick = []
        self._panel.setEnabled(False)
        self.rebuild_graph()

    def _populate_action_combo(self):
        self.cb_action.blockSignals(True)
        self.cb_action.clear()
        engine = self.flow.engine if self.flow else None
        self.cb_action.addItems(ops.all_actions(engine))
        self.cb_action.blockSignals(False)

    # ------------------------------------------------------------------ #
    # 重建圖(Flow → GraphModel → Qt items)
    # ------------------------------------------------------------------ #
    def rebuild_graph(self):
        self.scene.clear()
        self._node_items = []
        if not self.flow:
            return
        self.model = build_graph_model(self.flow)

        # 節點:由上而下垂直佈局
        for node in self.model.nodes:
            item = _NodeItem(node)
            y = _TOP + node.index * (_NODE_H + _V_GAP)
            item.setPos(_LEFT, y)
            item.clicked.connect(self._on_node_clicked)
            self.scene.addItem(item)
            self._node_items.append(item)

        # 邊
        for edge in self.model.edges:
            self._draw_edge(edge)

        # 場景範圍
        if self._node_items:
            self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-120, -40, 160, 60))

        # 重畫後恢復選取標記
        if 0 <= self._current_index < len(self._node_items):
            self._node_items[self._current_index].set_marked(True)

    def _draw_edge(self, edge: GraphEdge):
        if not (0 <= edge.src < len(self._node_items)
                and 0 <= edge.dst < len(self._node_items)):
            return
        src = self._node_items[edge.src]
        dst = self._node_items[edge.dst]
        color, dashed = _EDGE_STYLE.get(edge.kind, _EDGE_STYLE[EDGE_SEQ])

        if edge.kind == EDGE_SEQ:
            p1, p2 = src.bottom_center(), dst.top_center()
            path = QPainterPath(p1)
            path.lineTo(p2)
        else:
            # 特殊邊:走右側弧線,避免和順序邊重疊
            p1 = src.right_center()
            p2 = dst.right_center()
            bulge = 70 + 18 * abs(edge.dst - edge.src)
            path = QPainterPath(p1)
            c1 = QPointF(p1.x() + bulge, p1.y())
            c2 = QPointF(p2.x() + bulge, p2.y())
            path.cubicTo(c1, c2, p2)

        pen = QPen(QColor(color))
        pen.setWidthF(2.0)
        if dashed:
            pen.setStyle(Qt.DashLine)
        line_item = QGraphicsPathItem(path)
        line_item.setPen(pen)
        line_item.setZValue(1)
        self.scene.addItem(line_item)

        # 箭頭(在終點)
        self._add_arrow_head(path, color)

        # 標籤
        if edge.text:
            txt = QGraphicsSimpleTextItem(edge.text)
            txt.setBrush(QBrush(QColor(color)))
            f = QFont(); f.setPointSize(8); f.setBold(True)
            txt.setFont(f)
            mid = path.pointAtPercent(0.5)
            txt.setPos(mid.x() + 6, mid.y() - 8)
            txt.setZValue(3)
            self.scene.addItem(txt)

    def _add_arrow_head(self, path: QPainterPath, color: str):
        end = path.pointAtPercent(1.0)
        near = path.pointAtPercent(0.97)
        line = QLineF(near, end)
        if line.length() == 0:
            return
        angle = line.angle()
        import math
        rad = math.radians(angle)
        size = 9
        # 兩個翼點
        left = QPointF(
            end.x() - size * math.cos(rad - math.radians(25)),
            end.y() + size * math.sin(rad - math.radians(25)))
        right = QPointF(
            end.x() - size * math.cos(rad + math.radians(25)),
            end.y() + size * math.sin(rad + math.radians(25)))
        poly = QPolygonF([end, left, right])
        from PySide6.QtWidgets import QGraphicsPolygonItem
        head = QGraphicsPolygonItem(poly)
        head.setBrush(QBrush(QColor(color)))
        head.setPen(QPen(QColor(color)))
        head.setZValue(1)
        self.scene.addItem(head)

    # ------------------------------------------------------------------ #
    # 互動:點節點 / goto / 拖拉重排
    # ------------------------------------------------------------------ #
    def _on_node_clicked(self, index: int):
        if self.btn_goto.isChecked():
            self._goto_pick.append(index)
            if len(self._goto_pick) >= 2:
                self._apply_goto(self._goto_pick[0], self._goto_pick[1])
                self._goto_pick = []
                self.btn_goto.setChecked(False)
            return
        # 先把目前面板收回上一個節點
        if 0 <= self._current_index < len(self.flow.steps):
            self._commit_panel(self._current_index)
        self._select_index(index)

    def _select_index(self, index: int):
        for i, it in enumerate(self._node_items):
            it.set_marked(i == index)
        self._current_index = index
        if self.flow and 0 <= index < len(self.flow.steps):
            self._panel.setEnabled(True)
            self._load_panel(self.flow.steps[index])
        else:
            self._panel.setEnabled(False)

    def _on_goto_toggled(self, on: bool):
        self._goto_pick = []
        if on:
            self.btn_goto.setText("選來源 → 目標…")
        else:
            self.btn_goto.setText("🔗 連 goto")

    def _apply_goto(self, src_index: int, dst_index: int):
        if not self.flow:
            return
        if src_index == dst_index:
            QMessageBox.information(self, "連 goto", "來源與目標不可相同。")
            return
        src_step = self.flow.steps[src_index]
        dst_step = self.flow.steps[dst_index]
        ops.update_step_basic(src_step, on_error=f"goto:{dst_step.id}")
        self.rebuild_graph()
        self._select_index(src_index)

    def _on_node_drag_finished(self):
        """任一節點被拖放後:依目前各節點 y 座標重排 flat steps。"""
        if not self.flow or not self._node_items:
            return
        # 目前各節點(index 對應 flow.steps)依 y 排序 → 新順序的原 index 串列
        order = sorted(range(len(self._node_items)),
                       key=lambda i: self._node_items[i].pos().y())
        if order == list(range(len(self._node_items))):
            # 沒有順序變化:只把節點對齊回格線
            self._relayout_positions()
            return
        # 記住目前選取的 step_id,重排後重新定位選取
        sel_id = None
        if 0 <= self._current_index < len(self.flow.steps):
            sel_id = self.flow.steps[self._current_index].id
        reorder_by_y(self.flow, order)
        self.rebuild_graph()
        if sel_id is not None:
            for i, s in enumerate(self.flow.steps):
                if s.id == sel_id:
                    self._select_index(i)
                    break

    def _relayout_positions(self):
        """把節點對齊回標準格線(不改順序)。"""
        for it in self._node_items:
            y = _TOP + it.node.index * (_NODE_H + _V_GAP)
            it.setPos(_LEFT, y)

    # ------------------------------------------------------------------ #
    # 工具列:新增 / 刪除節點
    # ------------------------------------------------------------------ #
    def _add_node(self):
        if not self.flow:
            QMessageBox.information(self, "尚未選擇流程", "請先在上方選一條流程。")
            return
        if 0 <= self._current_index < len(self.flow.steps):
            self._commit_panel(self._current_index)
        default_action = ops.all_actions(self.flow.engine)[0]
        at = self._current_index + 1 if self._current_index >= 0 else None
        ops.add_step(self.flow, action=default_action, at=at)
        new_index = at if at is not None else len(self.flow.steps) - 1
        self.rebuild_graph()
        self._select_index(new_index)

    def _delete_node(self):
        if not self.flow or self._current_index < 0:
            return
        idx = self._current_index
        if ops.delete_step(self.flow, idx):
            self._current_index = -1
            self.rebuild_graph()
            if self.flow.steps:
                self._select_index(min(idx, len(self.flow.steps) - 1))
            else:
                self._panel.setEnabled(False)

    # ------------------------------------------------------------------ #
    # 面板 ↔ step
    # ------------------------------------------------------------------ #
    def _load_panel(self, step):
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

        p = step.params or {}
        self.ed_if_var.setText(str(p.get("var", "")))
        self.cb_if_op.setCurrentText(str(p.get("op", "eq")))
        self.ed_if_value.setText(str(p.get("value", "")))
        self.sp_skip.setValue(int(p.get("skip_count", 0) or 0))
        self.sp_loop_count.setValue(int(p.get("count", 0) or 0))
        self.sp_body.setValue(int(p.get("body_count", p.get("count_body", 0)) or 0))

        # 其他 params 預覽(排除控制流鍵)
        ctrl_keys = {"var", "op", "value", "skip_count", "count", "body_count", "_secret"}
        others = {k: v for k, v in p.items() if k not in ctrl_keys}
        import json
        self.params_preview.setPlainText(
            json.dumps(others, ensure_ascii=False, indent=2) if others else "{}")

    def _commit_panel(self, index: int):
        """把面板值寫回 flow.steps[index](走 flow_edit_ops)。"""
        if not self.flow or not (0 <= index < len(self.flow.steps)):
            return
        step = self.flow.steps[index]
        on_error = self.cb_on_error.currentText()
        if on_error == "goto:":
            on_error = "goto:" + self.ed_goto.text().strip()
        ops.update_step_basic(
            step,
            action=self.cb_action.currentText().strip() or step.action,
            label=self.ed_label.text(),
            on_error=on_error,
            timeout_ms=self.sp_timeout.value(),
        )
        # 控制流參數:合併進現有 params(保留其他鍵)
        params = dict(step.params or {})
        action = step.action
        if action == "flow.if":
            params["var"] = self.ed_if_var.text().strip()
            params["op"] = self.cb_if_op.currentText()
            params["value"] = self.ed_if_value.text()
            params["skip_count"] = int(self.sp_skip.value())
        elif action == "flow.loop":
            cnt = int(self.sp_loop_count.value())
            if cnt > 0:
                params["count"] = cnt
            params["body_count"] = int(self.sp_body.value())
        ops.set_params(step, params)

    def _apply_panel(self):
        if self._current_index < 0:
            return
        self._commit_panel(self._current_index)
        self.rebuild_graph()
        self._select_index(self._current_index)

    # ------------------------------------------------------------------ #
    # 存檔
    # ------------------------------------------------------------------ #
    def _save(self):
        if not self.flow:
            QMessageBox.information(self, "尚未選擇流程", "沒有可存的流程。")
            return
        if 0 <= self._current_index < len(self.flow.steps):
            self._commit_panel(self._current_index)
        try:
            ops.save_flow_to_store(self.flow, self.store)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "存檔失敗", f"{type(e).__name__}: {e}")
            return
        self.flows_changed.emit()
        QMessageBox.information(
            self, "已存檔",
            f"流程「{self.flow.name}」已存回({len(self.flow.steps)} 步)。")

    # ------------------------------------------------------------------ #
    # 縮放
    # ------------------------------------------------------------------ #
    def _view_wheel_event(self, event):
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 1 / 1.15
        self.view.scale(factor, factor)
