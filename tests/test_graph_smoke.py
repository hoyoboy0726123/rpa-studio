# -*- coding: utf-8 -*-
"""Offscreen smoke test:流程圖視覺化編輯器 (GraphPage)。

驗證(全部不點 UI):
  A. 純函式 build_graph_model:給一條含 flow.if(skip_count)、flow.loop(body_count)、
     on_error=goto 的 flow,斷言:
        - 節點數 == steps 數
        - 順序邊數 == steps-1
        - 產生 if 略過邊 / loop 回邊 / goto 邊各一,且來源 / 目標索引正確
  B. GraphPage:用 GraphPage(store) 建立、載入該 flow,斷言畫布 node item 數 == steps 數,
     且內部 GraphModel 與純函式一致。
  C. 編輯操作(走 flow_edit_ops):
        - 拖拉重排(reorder_by_y)→ flat steps 順序正確
        - 新增 / 刪除節點 → steps 結構正確
        - 設 goto(_apply_goto)→ 來源 step.on_error == goto:<目標 id>

執行:
  QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 python tests/test_graph_smoke.py
"""
from __future__ import annotations
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication

from core.schema import Flow
from core.store import Store
from ui import flow_edit_ops as ops
from ui.pages.graph_page import (
    GraphPage, build_graph_model, reorder_by_y,
    EDGE_SEQ, EDGE_SKIP, EDGE_LOOP, EDGE_GOTO,
)


def _make_flow() -> Flow:
    """造一條含 if(skip)、loop(body)、goto 的 flow。

    steps:
      0 web.goto
      1 flow.if  skip_count=2     -> 不成立跳到 index 3
      2 web.fill (被 if 略過的 body 之一)
      3 flow.loop body_count=2    -> body = steps[4], steps[5]
      4 web.click
      5 web.fill                  -> body 最後一步,回邊指回 index 3
      6 web.screenshot  on_error=goto:<steps[0].id>
    """
    return Flow.from_dict({
        "name": "graph_demo",
        "engine": "web",
        "steps": [
            {"id": "s0", "action": "web.goto", "label": "開首頁"},
            {"id": "s1", "action": "flow.if", "label": "判斷",
             "params": {"var": "x", "op": "eq", "value": "1", "skip_count": 2}},
            {"id": "s2", "action": "web.fill", "label": "條件內步驟"},
            {"id": "s3", "action": "flow.loop", "label": "迴圈",
             "params": {"count": 3, "body_count": 2}},
            {"id": "s4", "action": "web.click", "label": "body-1"},
            {"id": "s5", "action": "web.fill", "label": "body-2"},
            {"id": "s6", "action": "web.screenshot", "label": "收尾",
             "on_error": "goto:s0"},
        ],
    })


# =========================================================================== #
# A. 純函式 build_graph_model
# =========================================================================== #
def test_build_graph_model():
    flow = _make_flow()
    model = build_graph_model(flow)

    assert len(model.nodes) == len(flow.steps) == 7, len(model.nodes)
    # 節點 index 對應 steps 順序
    assert [nd.step_id for nd in model.nodes] == [s.id for s in flow.steps]
    # if / loop 節點 kind 正確
    assert model.nodes[1].kind == "if"
    assert model.nodes[3].kind == "loop"
    assert model.nodes[0].category == "web"

    # 順序邊:steps-1 條,且相鄰
    seq = model.edges_of(EDGE_SEQ)
    assert len(seq) == 6, len(seq)
    assert all(e.dst == e.src + 1 for e in seq)

    # if 略過邊:從 index 1 跳到 1+1+2 = 4
    skip = model.edges_of(EDGE_SKIP)
    assert len(skip) == 1, skip
    assert skip[0].src == 1 and skip[0].dst == 4, (skip[0].src, skip[0].dst)

    # loop 回邊:body 最後一步(3+2=5)回到 loop 節點 3
    loop = model.edges_of(EDGE_LOOP)
    assert len(loop) == 1, loop
    assert loop[0].src == 5 and loop[0].dst == 3, (loop[0].src, loop[0].dst)

    # goto 邊:index 6 → index 0(s0)
    goto = model.edges_of(EDGE_GOTO)
    assert len(goto) == 1, goto
    assert goto[0].src == 6 and goto[0].dst == 0, (goto[0].src, goto[0].dst)

    print("[OK] A. build_graph_model:節點數=7、順序邊=6、if略過/loop回邊/goto各1 且索引正確。")


def test_skip_clamp():
    """skip_count 超過尾端時夾到最後一個節點。"""
    flow = Flow.from_dict({
        "name": "clamp", "engine": "web",
        "steps": [
            {"id": "a", "action": "flow.if", "params": {"skip_count": 99}},
            {"id": "b", "action": "web.click"},
        ],
    })
    model = build_graph_model(flow)
    skip = model.edges_of(EDGE_SKIP)
    assert len(skip) == 1 and skip[0].dst == 1, skip
    print("[OK] A. skip_count 超界 → 略過邊夾到最後一個節點。")


# =========================================================================== #
# B. GraphPage 建立 + 載入
# =========================================================================== #
def test_graph_page_load(store):
    flow = _make_flow()
    ops.save_flow_to_store(flow, store)

    page = GraphPage(store)
    page.load_flow("graph_demo")

    assert page.flow is not None
    assert len(page._node_items) == len(flow.steps) == 7, len(page._node_items)
    # 內部 model 與純函式一致
    m2 = build_graph_model(page.flow)
    assert len(m2.edges_of(EDGE_SKIP)) == 1
    assert len(m2.edges_of(EDGE_LOOP)) == 1
    assert len(m2.edges_of(EDGE_GOTO)) == 1
    print(f"[OK] B. GraphPage 載入 graph_demo:{len(page._node_items)} 個節點 item。")
    return page


# =========================================================================== #
# C. 編輯操作(走 flow_edit_ops / 純函式)
# =========================================================================== #
def test_reorder_by_y():
    flow = _make_flow()
    ids_before = [s.id for s in flow.steps]
    # 把最後一步移到最前面:order = [6,0,1,2,3,4,5]
    order = [6, 0, 1, 2, 3, 4, 5]
    reorder_by_y(flow, order)
    assert [s.id for s in flow.steps] == [ids_before[i] for i in order]
    # 非法排列(缺項)→ 不動
    snapshot = [s.id for s in flow.steps]
    reorder_by_y(flow, [0, 0, 1])
    assert [s.id for s in flow.steps] == snapshot
    print("[OK] C. reorder_by_y:合法排列重排 flat steps、非法排列安全不動。")


def test_add_delete():
    flow = _make_flow()
    n0 = len(flow.steps)
    ops.add_step(flow, action="web.wait", label="新步", at=2)
    assert len(flow.steps) == n0 + 1
    assert flow.steps[2].action == "web.wait"
    assert ops.delete_step(flow, 2) is True
    assert len(flow.steps) == n0
    assert flow.steps[2].id == "s2"
    print("[OK] C. add_step / delete_step:插入索引 2 再刪除,結構復原。")


def test_apply_goto(page):
    """透過 GraphPage._apply_goto 設定 on_error=goto。"""
    # page 載入的是 graph_demo;把 index 2 (s2) 設成 goto 到 index 5 (s5)
    page._apply_goto(2, 5)
    src = page.flow.steps[2]
    dst = page.flow.steps[5]
    assert src.on_error == f"goto:{dst.id}", src.on_error
    # 重建後應多一條 goto 邊(原本 1 條,加新的共 2 條)
    model = build_graph_model(page.flow)
    gotos = model.edges_of(EDGE_GOTO)
    assert len(gotos) == 2, [(g.src, g.dst) for g in gotos]
    assert any(g.src == 2 and g.dst == 5 for g in gotos)
    print("[OK] C. _apply_goto:來源 step.on_error=goto:<目標 id>,圖多一條 goto 邊。")


def main():
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    tmpdir = tempfile.mkdtemp(prefix="rpa_graph_")
    store = Store(os.path.join(tmpdir, "graph.db"))

    test_build_graph_model()
    test_skip_clamp()
    page = test_graph_page_load(store)
    test_reorder_by_y()
    test_add_delete()
    test_apply_goto(page)
    print("\nALL GRAPH SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
