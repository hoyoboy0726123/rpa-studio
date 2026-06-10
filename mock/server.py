# -*- coding: utf-8 -*-
"""假內網 (mock intranet) — 純 stdlib http.server,不依賴 flask。

路由:
  GET  /            -> 轉到 /login
  GET  /login       -> 登入表單(role=button 名稱「登入」、testid 輸入框,方便定位)
  POST /login       -> 設 session cookie,302 轉到 /home
  GET  /home        -> 登入後首頁(含查詢與匯出入口)
  GET  /query?sn=X  -> 已知序號回欄位 HTML(穩定 id);未知序號回明顯「查無資料 / Not Found」
  GET  /export?month=YYYY-MM -> 即時以 openpyxl 產生小 .xlsx 並觸發下載

helper:
  start(port) -> threading.Thread   (背景啟動,daemon)
  stop()                            (關閉最近一次 start 的伺服器)
也可建立 MockServer 實例自行管理多個埠。
"""
from __future__ import annotations
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 已知序號資料庫(供 /query 比對)。鍵為序號,值為三欄資訊。
KNOWN_SERIALS = {
    "SN12345": {"產品狀態": "正常出貨", "出貨日": "2026-05-12", "負責人": "王小明"},
    "SN67890": {"產品狀態": "維修中", "出貨日": "2026-04-30", "負責人": "李小華"},
    "ABC-001": {"產品狀態": "已退貨", "出貨日": "2026-03-01", "負責人": "陳大同"},
}


def _html(body: str) -> bytes:
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Mock Intranet</title></head><body>" + body +
            "</body></html>").encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    # 安靜模式:不要把每個 request 印到 stderr(測試輸出乾淨)
    def log_message(self, *a, **k):
        return

    # ---- 回應小工具 ---- #
    def _send_html(self, body: str, status: int = 200, headers=None):
        data = _html(body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, set_cookie: str | None = None):
        self.send_response(302)
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    # ---- GET ---- #
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/login"):
            return self._send_html(self._login_form())
        if path == "/home":
            return self._send_html(self._home())
        if path == "/query":
            return self._query(qs)
        if path == "/export":
            return self._export(qs)
        return self._send_html("<h1>404</h1>", status=404)

    # ---- POST ---- #
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)  # 不驗證帳密,讀掉 body 即可
            return self._redirect("/home", set_cookie="session=ok; Path=/")
        return self._send_html("<h1>404</h1>", status=404)

    # ---- 頁面 ---- #
    def _login_form(self) -> str:
        return (
            "<h1>登入</h1>"
            "<form method='POST' action='/login'>"
            "<input data-testid='username' name='username' placeholder='帳號'><br>"
            "<input data-testid='password' name='password' type='password' "
            "placeholder='密碼'><br>"
            "<button type='submit'>登入</button>"
            "</form>"
        )

    def _home(self) -> str:
        return (
            "<h1>首頁</h1>"
            "<p data-testid='welcome'>歡迎,已登入</p>"
            "<form method='GET' action='/query'>"
            "<input data-testid='sn' name='sn' placeholder='序號'>"
            "<button type='submit'>查詢</button>"
            "</form>"
            "<form method='GET' action='/export'>"
            "<select data-testid='month' name='month'>"
            "<option value='2026-05'>2026-05</option>"
            "<option value='2026-04'>2026-04</option>"
            "</select>"
            "<button type='submit'>匯出</button>"
            "</form>"
        )

    def _query(self, qs: dict):
        sn = (qs.get("sn", [""])[0] or "").strip()
        info = KNOWN_SERIALS.get(sn)
        if info is None:
            body = (
                f"<h1>查詢結果</h1>"
                f"<p id='serial'>{sn}</p>"
                f"<p id='result' data-testid='result'>查無資料 / Not Found</p>"
            )
            return self._send_html(body, status=200)
        body = (
            "<h1>查詢結果</h1>"
            f"<p id='serial'>{sn}</p>"
            f"<div id='status' data-testid='status'>{info['產品狀態']}</div>"
            f"<div id='ship_date' data-testid='ship_date'>{info['出貨日']}</div>"
            f"<div id='owner' data-testid='owner'>{info['負責人']}</div>"
            # 同時提供一個表格版(供 web.scrape_table 測試)
            "<table id='detail'><thead><tr>"
            "<th>產品狀態</th><th>出貨日</th><th>負責人</th></tr></thead>"
            "<tbody><tr>"
            f"<td>{info['產品狀態']}</td><td>{info['出貨日']}</td>"
            f"<td>{info['負責人']}</td>"
            "</tr></tbody></table>"
        )
        return self._send_html(body)

    def _export(self, qs: dict):
        month = (qs.get("month", ["2026-05"])[0] or "2026-05").strip()
        data = _build_xlsx(month)
        filename = f"report_{month}.xlsx"
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _build_xlsx(month: str) -> bytes:
    """用 openpyxl 即時產生一個小 .xlsx(回 bytes)。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "report"
    ws.append(["序號", "產品狀態", "出貨日", "負責人"])
    for sn, info in KNOWN_SERIALS.items():
        ws.append([sn, info["產品狀態"], info["出貨日"], info["負責人"]])
    ws["F1"] = f"資料月份: {month}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class MockServer:
    """可程式化啟動/關閉的 mock 伺服器(指定 port,0 表示隨機可用埠)。"""

    def __init__(self, port: int = 0, host: str = "127.0.0.1"):
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self.host = host
        self.port = self._httpd.server_address[1]  # 取實際綁定埠
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass


# ---- 模組級 helper(對應規格的 start/stop 介面) ---- #
_singleton: MockServer | None = None


def start(port: int = 0) -> threading.Thread:
    """啟動 mock server 於指定 port(0=隨機),回傳背景 thread。"""
    global _singleton
    _singleton = MockServer(port=port)
    return _singleton.start()


def stop() -> None:
    """關閉最近一次 start() 啟動的 mock server。"""
    global _singleton
    if _singleton is not None:
        _singleton.stop()
        _singleton = None


def current() -> MockServer | None:
    """取得目前的 singleton(供測試讀 base_url/port)。"""
    return _singleton
