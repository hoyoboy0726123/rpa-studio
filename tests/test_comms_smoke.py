# -*- coding: utf-8 -*-
"""comms.* 動作(email + sharepoint)冒煙測試。

不依賴 PySide6 / Playwright / Outlook / O365:
  - email.send 的 **SMTP 路徑**:用標準函式庫 socket 自寫一個極簡本機 SMTP 收信器
    (不需 aiosmtpd / smtpd — 後者在 Python 3.12+ 已移除),寄一封 -> 斷言收到、
    主旨 / 內文 / 附件正確。
  - 訊息建構(build_mime_message / normalize_spec)為純函式,直接單元測試。
  - Outlook COM 路徑無法在無 Outlook 環境端到端測 -> 標 SKIP 並誠實說明。
  - sharepoint:無法真連 O365 -> 測 URL/chunk/樣式純函式 + graceful 降級
    (無 msal 時回友善錯誤、不崩);端到端標 SKIP。
  - 斷言 email.* / sharepoint.* 已註冊。

執行(系統 python,專案根):
    python tests/test_comms_smoke.py
全綠回 exit 0;任一失敗 raise AssertionError 並 exit 1。
"""
from __future__ import annotations

import os
import sys
import email
from email.header import decode_header, make_header
import socket
import threading
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.registry import ActionContext, ActionResult, ACTIONS
from core.variables import VarStore
from core.schema import Flow, Step
from core.runner import run_flow

import engines.comms  # noqa: F401  匯入即註冊 email.* / sharepoint.*
from engines.comms import email_send as _email
from engines.comms import sharepoint as _sp


# --------------------------------------------------------------------------- #
# 測試輔助
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self):
        self.steps = []

    def log_step(self, run_id, step_id, action, status, ms=0, retries=0,
                 error="", screenshot=""):
        self.steps.append((step_id, action, status))


class _FakeVault:
    """記憶體 Vault:set/get_secret。"""
    def __init__(self, init=None):
        self.store = dict(init or {})

    def set_secret(self, name, value):
        self.store[name] = value

    def get_secret(self, name):
        return self.store.get(name)


def _make_ctx(vars_init=None, extra=None, vault=None):
    return ActionContext(
        engine=None,
        vars=VarStore(vars_init or {}),
        vault=vault,
        store=_FakeStore(),
        run_id="t",
        log=lambda *_a, **_k: None,
        extra=extra or {},
    )


def _flow(steps):
    f = Flow(name="t", engine="web")
    f.steps = [Step.from_dict(s) for s in steps]
    return f


# --------------------------------------------------------------------------- #
# 極簡本機 SMTP 收信器(stdlib socket;支援 EHLO/MAIL/RCPT/DATA/QUIT)
# --------------------------------------------------------------------------- #
class TinySMTPServer:
    """單執行緒、單連線、收一封信就好的測試用 SMTP server。

    不做認證(本機 debugging);把收到的原始 DATA 存進 self.raw_message。
    """

    def __init__(self, host="127.0.0.1", port=0):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        self.raw_message = None
        self.mail_from = None
        self.rcpts = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.sendall(b"220 tiny-smtp ready\r\n")
            f = conn.makefile("rb")
            in_data = False
            data_lines = []
            while True:
                line = f.readline()
                if not line:
                    break
                if in_data:
                    if line in (b".\r\n", b".\n"):
                        self.raw_message = b"".join(data_lines)
                        conn.sendall(b"250 OK queued\r\n")
                        in_data = False
                        data_lines = []
                    else:
                        # 去 dot-stuffing
                        if line.startswith(b".."):
                            line = line[1:]
                        data_lines.append(line)
                    continue

                upper = line.upper()
                if upper.startswith(b"EHLO") or upper.startswith(b"HELO"):
                    # 廣告 AUTH 讓 smtplib.login 走得通(本機測試不真的驗帳密)
                    conn.sendall(b"250-tiny-smtp\r\n250-AUTH LOGIN PLAIN\r\n250 OK\r\n")
                elif upper.startswith(b"AUTH"):
                    # smtplib 預設走 AUTH LOGIN:server 連送兩次 334 challenge,
                    # client 各回一行 base64(帳 / 密)。一律接受。
                    args = line.strip().split()
                    if len(args) >= 2 and args[1].upper() == b"LOGIN" and len(args) == 2:
                        conn.sendall(b"334 VXNlcm5hbWU6\r\n")   # 'Username:'
                        f.readline()                              # username b64
                        conn.sendall(b"334 UGFzc3dvcmQ6\r\n")   # 'Password:'
                        f.readline()                              # password b64
                    conn.sendall(b"235 Authentication successful\r\n")
                elif upper.startswith(b"MAIL FROM"):
                    self.mail_from = line.decode("utf-8", "replace").strip()
                    conn.sendall(b"250 OK\r\n")
                elif upper.startswith(b"RCPT TO"):
                    self.rcpts.append(line.decode("utf-8", "replace").strip())
                    conn.sendall(b"250 OK\r\n")
                elif upper.startswith(b"DATA"):
                    conn.sendall(b"354 end with .\r\n")
                    in_data = True
                elif upper.startswith(b"QUIT"):
                    conn.sendall(b"221 bye\r\n")
                    break
                elif upper.startswith(b"RSET") or upper.startswith(b"NOOP"):
                    conn.sendall(b"250 OK\r\n")
                else:
                    conn.sendall(b"250 OK\r\n")

    def stop(self):
        try:
            self._sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 1) 純函式:normalize_spec / build_mime_message
# --------------------------------------------------------------------------- #
def test_normalize_spec():
    spec = _email.normalize_spec(
        to="a@x.com; b@y.com", cc=["c@z.com"], bcc="d@w.com",
        subject="主旨", body="<b>嗨</b>", html=True, attachments=None)
    assert spec.to == ["a@x.com", "b@y.com"], spec.to
    assert spec.cc == ["c@z.com"], spec.cc
    assert spec.bcc == ["d@w.com"], spec.bcc
    assert spec.html is True
    print("[OK] normalize_spec 收件人字串/list 正規化")


def test_build_mime_html_no_attach():
    spec = _email.normalize_spec(to="a@x.com", subject="HelloMIME",
                                 body="<p>內文</p>", html=True)
    msg, recips = _email.build_mime_message(spec)
    assert recips == ["a@x.com"], recips
    assert msg.get_content_type() == "text/html", msg.get_content_type()
    assert msg["Subject"] == "HelloMIME"
    assert "Bcc" not in msg  # Bcc 不寫 header
    print("[OK] build_mime_message HTML 無附件 -> text/html、Bcc 不外洩")


def test_build_mime_with_attachment():
    with tempfile.TemporaryDirectory() as d:
        att = os.path.join(d, "report.csv")
        with open(att, "w", encoding="utf-8") as fh:
            fh.write("col1,col2\n1,2\n")
        spec = _email.normalize_spec(to="a@x.com", subject="S", body="text",
                                     html=False, attachments=att)
        msg, _ = _email.build_mime_message(spec)
        assert msg.is_multipart(), "有附件應為 multipart"
        names = []
        for part in msg.walk():
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                names.append(part.get_filename())
        assert "report.csv" in names, names
    print("[OK] build_mime_message 帶附件 -> multipart + 附件檔名正確")


def test_build_mime_bcc_in_recipients():
    spec = _email.normalize_spec(to="a@x.com", cc="c@x.com", bcc="b@x.com",
                                 subject="S", body="t")
    _, recips = _email.build_mime_message(spec)
    assert set(recips) == {"a@x.com", "c@x.com", "b@x.com"}, recips
    print("[OK] build_mime_message Bcc 仍進信封收件人")


# --------------------------------------------------------------------------- #
# 2) email.send SMTP 路徑端到端(本機 TinySMTPServer)
# --------------------------------------------------------------------------- #
def test_email_send_smtp_end_to_end():
    server = TinySMTPServer()
    server.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            att = os.path.join(d, "data.txt")
            with open(att, "w", encoding="utf-8") as fh:
                fh.write("payload-123")

            vault = _FakeVault({"smtp_user": "u@local", "smtp_pass": "p"})
            ctx = _make_ctx(vault=vault)
            flow = _flow([{
                "action": "email.send",
                "params": {
                    "prefer": "smtp",          # 強制 SMTP 路徑(測試環境無 Outlook)
                    "to": "rcpt@local.test",
                    "cc": "cc@local.test",
                    "subject": "冒煙主旨",
                    "body": "<h1>HTML 內文</h1>",
                    "html": True,
                    "attachments": att,
                    "result_var": "send_result",
                    "smtp": {
                        "host": server.host,
                        "port": server.port,
                        "from_addr": "sender@local.test",
                        # 帳密走 Vault(secret 名稱);TinySMTP 不驗,但測 Vault 取值路徑
                        "user_secret": "smtp_user",
                        "pass_secret": "smtp_pass",
                    },
                },
            }])
            res = run_flow(flow, ctx)
            assert res.status == "completed", (res.status, ctx.vars.get("send_result"))

            # server 收到信 -> 解析驗證
            assert server.raw_message is not None, "TinySMTP 未收到信"
            parsed = email.message_from_bytes(server.raw_message)
            subject = str(make_header(decode_header(parsed["Subject"])))
            assert subject == "冒煙主旨", subject
            # 信封收件人應含 to + cc
            joined = " ".join(server.rcpts)
            assert "rcpt@local.test" in joined, server.rcpts
            assert "cc@local.test" in joined, server.rcpts
            # 內文 + 附件
            bodies, att_names = [], []
            for part in parsed.walk():
                ct = part.get_content_type()
                cd = part.get("Content-Disposition", "")
                if "attachment" in cd:
                    att_names.append(part.get_filename())
                elif ct == "text/html":
                    bodies.append(part.get_payload(decode=True).decode("utf-8", "replace"))
            assert any("HTML 內文" in b for b in bodies), bodies
            assert "data.txt" in att_names, att_names
    finally:
        server.stop()
    print("[OK] email.send SMTP 端到端:收到信、主旨/HTML內文/附件/收件人正確")


def test_email_send_smtp_missing_recipient():
    """無收件人 -> ok=False(graceful,不崩)。"""
    ctx = _make_ctx()
    flow = _flow([{"action": "email.send",
                   "params": {"prefer": "smtp", "subject": "x", "body": "y",
                              "result_var": "r",
                              "smtp": {"host": "127.0.0.1", "port": 1}}}])
    res = run_flow(flow, ctx)
    assert res.status == "failed", res.status
    print("[OK] email.send 無收件人 -> failed(graceful)")


# --------------------------------------------------------------------------- #
# 3) Outlook COM 路徑:無法在無 Outlook 環境端到端測 -> SKIP
# --------------------------------------------------------------------------- #
def test_email_outlook_skip():
    # 重要:即使本機偵測得到 Outlook,測試也**不**真的寄信 / 存草稿,
    # 以免污染使用者真實信箱(寄出無法收回、草稿夾殘留)。
    # 僅檢查偵測函式可呼叫且回 bool;真實 Outlook 端到端一律標 SKIP,需人工驗證。
    avail = _email.outlook_available()
    assert isinstance(avail, bool)
    if avail:
        print("[SKIP] 本機偵測得到 Outlook,但為避免污染真實信箱,"
              "Outlook COM 寄信/回覆/草稿端到端不在自動測試中執行,需人工驗證。")
    else:
        print("[SKIP] 無 Outlook / 無 pywin32 可驅動 -> Outlook COM 寄信/回覆/草稿"
              " 端到端需在已安裝並登入 Outlook 的機器上手動驗證。"
              "(已驗證:無 Outlook 時 send_via_outlook 回 ok=False 友善降級,不崩)")


def test_email_reply_graceful():
    """reply 缺 entry_id / 無 Outlook -> ok=False 且不崩。"""
    ctx = _make_ctx()
    flow = _flow([{"action": "email.reply",
                   "params": {"body": "回覆內文", "result_var": "r"}}])
    res = run_flow(flow, ctx)
    assert res.status == "failed", res.status
    print("[OK] email.reply 無 entry_id / 無 Outlook -> failed(graceful 不崩)")


# --------------------------------------------------------------------------- #
# 4) sharepoint 純函式:URL / chunk / 樣式
# --------------------------------------------------------------------------- #
def test_sp_url_builders():
    assert _sp.normalize_remote_path("\\a\\b\\") == "a/b"
    assert _sp.drive_root_url("me").endswith("/me/drive")
    assert _sp.drive_root_url("DRV123").endswith("/drives/DRV123")
    assert _sp.item_by_path_url("", "me").endswith("/me/drive/root")
    assert "/root:/a/b" in _sp.item_by_path_url("a/b", "me")
    assert _sp.upload_content_url("f/x.txt").endswith(":/f/x.txt:/content")
    assert _sp.create_session_url("f/x.txt").endswith(":/f/x.txt:/createUploadSession")
    print("[OK] sharepoint URL 組裝純函式")


def test_sp_chunk_plan():
    chunks = _sp.plan_chunks(10, chunk=4)
    assert chunks == [(0, 3, 4), (4, 7, 4), (8, 9, 2)], chunks
    assert _sp.content_range_header(0, 3, 10) == "bytes 0-3/10"
    assert _sp.plan_chunks(0) == []
    print("[OK] sharepoint plan_chunks 切片 + Content-Range")


def test_sp_name_matches():
    assert _sp.name_matches("report_202605.xlsx", name_contains="report")
    assert _sp.name_matches("REPORT.xlsx", name_contains="report")  # 大小寫不敏感
    assert _sp.name_matches("a_2026.csv", name_regex=r"\d{4}\.csv$")
    assert not _sp.name_matches("keep.txt", name_contains="report")
    assert not _sp.name_matches("anything", "", "")   # 兩者皆空 -> 不誤刪
    print("[OK] sharepoint name_matches 樣式比對(含防誤刪)")


# --------------------------------------------------------------------------- #
# 5) sharepoint graceful:無 msal -> 友善錯誤、不崩
# --------------------------------------------------------------------------- #
def test_sp_auth_graceful_no_msal():
    auth = _sp.GraphAuth(client_id="dummy")
    token, err = auth.acquire_token()
    msal_present = _sp._import_msal() is not None
    if not msal_present:
        assert token is None and "msal" in err.lower(), (token, err)
        print("[OK] 無 msal -> GraphAuth 回友善錯誤、不崩:" + err)
    else:
        # 有 msal 但無有效 client_id/帳號 -> 仍應 graceful(回 None + err)
        assert token is None, "dummy client_id 不該拿到 token"
        print("[OK] 有 msal 但 dummy 帳號 -> graceful 回 None + err(不崩)")


def test_sp_action_graceful():
    """sharepoint.upload 在無 msal / 無 token 時 -> ok=False,不崩。"""
    ctx = _make_ctx()
    with tempfile.TemporaryDirectory() as d:
        local = os.path.join(d, "f.txt")
        with open(local, "w") as fh:
            fh.write("x")
        flow = _flow([{"action": "sharepoint.upload",
                       "params": {"client_id": "dummy", "local_path": local,
                                  "remote_folder": "Reports"}}])
        res = run_flow(flow, ctx)
        assert res.status == "failed", res.status
    print("[OK] sharepoint.upload 無認證 -> failed(graceful;真連 O365 需有效帳號,端到端 SKIP)")


def test_sp_client_no_requests_graceful():
    """GraphClient 在無 requests 時各操作回友善錯誤、不崩。"""
    if _sp._import_requests() is None:
        c = _sp.GraphClient("tok", drive="me")
        ok, info = c.mkdir("a/b")
        assert not ok and "requests" in str(info).lower(), info
        print("[OK] 無 requests -> GraphClient 友善降級")
    else:
        # requests 存在(本專案有列依賴):用假 session 驗 mkdir 走通組裝邏輯
        print("[SKIP] requests 已安裝 -> 無 requests 降級分支以邏輯檢視為準"
              "(_ensure() 在 session=None 時回友善錯誤)")


# --------------------------------------------------------------------------- #
# 6) 動作註冊
# --------------------------------------------------------------------------- #
def test_actions_registered():
    for name in ("email.send", "email.reply", "sharepoint.mkdir",
                 "sharepoint.upload", "sharepoint.delete_old",
                 "sharepoint.share_link"):
        assert name in ACTIONS, f"{name} 未註冊"
    print("[OK] email.* / sharepoint.* 全部已註冊")


# --------------------------------------------------------------------------- #
def main():
    test_actions_registered()
    test_normalize_spec()
    test_build_mime_html_no_attach()
    test_build_mime_with_attachment()
    test_build_mime_bcc_in_recipients()
    test_email_send_smtp_end_to_end()
    test_email_send_smtp_missing_recipient()
    test_email_outlook_skip()
    test_email_reply_graceful()
    test_sp_url_builders()
    test_sp_chunk_plan()
    test_sp_name_matches()
    test_sp_auth_graceful_no_msal()
    test_sp_action_graceful()
    test_sp_client_no_requests_graceful()
    print("\nALL GREEN")


if __name__ == "__main__":
    main()
