# -*- coding: utf-8 -*-
"""流程資料模型 (flow data model)。
扁平 JSON step 串列 + 多定位器(primary + fallbacks + fingerprint),
吸收 Selenese 可序列化、n8n 錯誤分支、UI.Vision 多定位器、PAD 變數化。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json
import uuid


def new_id(prefix: str = "s") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass
class Step:
    """一個自動化步驟。
    action : registry key,例 'web.click' / 'desktop.click' / 'flow.if'
    target : 定位資訊 {primary:{strategy,value}, fallbacks:[...], fingerprint:{...}}
             strategy 可為 role|text|testid|css|xpath|uia|image|coord
    params : 動作參數(填值 / url / 區間 / result_var 等),字串值支援 {var} 替換
    secret_ref : 只放 secret 名稱,實際值由 vault 取(不入 flow JSON)
    on_error : abort | continue | goto:<step_id>
    """
    id: str
    action: str
    label: str = ""
    target: dict | None = None
    params: dict = field(default_factory=dict)
    secret_ref: str | None = None
    retry: dict = field(default_factory=lambda: {"times": 0, "interval_ms": 1000})
    timeout_ms: int = 15000
    on_error: str = "abort"

    @staticmethod
    def from_dict(d: dict) -> "Step":
        return Step(
            id=d.get("id") or new_id(),
            action=d["action"],
            label=d.get("label", ""),
            target=d.get("target"),
            params=d.get("params", {}) or {},
            secret_ref=d.get("secret_ref"),
            retry=d.get("retry", {"times": 0, "interval_ms": 1000}),
            timeout_ms=d.get("timeout_ms", 15000),
            on_error=d.get("on_error", "abort"),
        )


@dataclass
class Flow:
    """一條流程。engine 決定用哪個引擎執行(web=Playwright / desktop=pywinauto)。"""
    name: str
    engine: str = "web"          # web | desktop
    version: int = 1
    variables: dict = field(default_factory=dict)   # 預設變數(可被執行期覆寫)
    steps: list = field(default_factory=list)        # list[Step]
    created: str = ""
    modified: str = ""

    @staticmethod
    def from_dict(d: dict) -> "Flow":
        f = Flow(
            name=d.get("name", "untitled"),
            engine=d.get("engine", "web"),
            version=d.get("version", 1),
            variables=d.get("variables", {}) or {},
            created=d.get("created", ""),
            modified=d.get("modified", ""),
        )
        f.steps = [Step.from_dict(s) for s in d.get("steps", [])]
        return f

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def load(path: str) -> "Flow":
        with open(path, "r", encoding="utf-8") as fh:
            return Flow.from_dict(json.load(fh))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)


# 動作型別參考(供 UI 下拉與文件;實作在各引擎以 @action 註冊)
ACTION_CATALOG = {
    "web": [
        "web.goto", "web.click", "web.fill", "web.select", "web.wait",
        "web.wait_for", "web.scrape_table", "web.scrape_field", "web.download",
        "web.press", "web.screenshot",
    ],
    "desktop": [
        "desktop.focus_window", "desktop.click", "desktop.type", "desktop.read",
        "desktop.wait", "desktop.wait_for", "desktop.menu_select", "desktop.send_keys",
        "desktop.wait_image", "desktop.image_click", "desktop.ocr_read",
    ],
    "flow": [
        "flow.set_var", "flow.if", "flow.loop", "flow.call", "flow.prompt_user",
        "flow.pause_for_human", "flow.wait_file", "flow.http",
    ],
    "data": [
        "data.read_table", "data.write_excel", "data.csv_append",
        "excel.split", "excel.split_rules", "excel.diff",
    ],
    "comms": [
        "email.send", "email.reply",
        "sharepoint.mkdir", "sharepoint.upload", "sharepoint.delete_old",
        "sharepoint.share_link",
    ],
}
