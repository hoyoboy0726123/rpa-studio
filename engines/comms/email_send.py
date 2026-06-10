# -*- coding: utf-8 -*-
"""Email 寄送 — Outlook COM 優先,SMTP fallback。

兩條路徑共用同一份「訊息規格」(EmailSpec),由上層 action 組好後交給本模組:

  Outlook COM (win32com):
    - 直接驅動本機已登入的 Outlook,免帳密(走使用者既有 profile)。
    - 支援 HTML 內文 / 純文字、附件、cc/bcc、存草稿(不寄)、回覆既有信(reply / reply_all)。
    - win32com 為 **lazy import**:無 pywin32 / 無 Outlook 時不在 import 期爆,
      由 send_via_outlook() 回 (False, reason)。

  SMTP fallback (smtplib + email.mime):
    - 無 Outlook 時用 SMTP;host / port / user / password 由上層自 Vault 取好再傳入。
    - 支援 TLS(starttls)/ SSL(SMTP_SSL)/ 純文字連線。

純函式 build_mime_message() 不碰任何網路 / COM,單元測試可直接驗 MIME 結構。
"""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import formataddr, make_msgid, formatdate


# --------------------------------------------------------------------------- #
# 訊息規格 — Outlook / SMTP 兩條路徑共用
# --------------------------------------------------------------------------- #
@dataclass
class EmailSpec:
    """一封信的完整規格(與傳輸方式無關)。"""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    html: bool = True                       # body 是否為 HTML
    attachments: list[str] = field(default_factory=list)  # 本機檔案路徑
    from_addr: str = ""                     # SMTP 寄件者(Outlook 用既有 profile,忽略)
    from_name: str = ""                     # 顯示名稱(選用)


def _as_list(v) -> list[str]:
    """把 'a@x;b@y' / 'a@x,b@y' / ['a@x'] 一律正規化成乾淨的 list。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        items = list(v)
    else:
        items = str(v).replace(";", ",").split(",")
    return [s.strip() for s in items if s and str(s).strip()]


def normalize_spec(
    to=None, cc=None, bcc=None, subject="", body="", html=True,
    attachments=None, from_addr="", from_name="",
) -> EmailSpec:
    """把鬆散的 action params 正規化成 EmailSpec(純函式,可單元測試)。"""
    return EmailSpec(
        to=_as_list(to),
        cc=_as_list(cc),
        bcc=_as_list(bcc),
        subject=str(subject or ""),
        body=str(body or ""),
        html=bool(html),
        attachments=_as_list(attachments),
        from_addr=str(from_addr or ""),
        from_name=str(from_name or ""),
    )


# --------------------------------------------------------------------------- #
# 純函式:組 MIME 訊息(SMTP 路徑用;可離線單元測試)
# --------------------------------------------------------------------------- #
def build_mime_message(spec: EmailSpec):
    """依 EmailSpec 組出 email.mime 物件。

    - html=True -> MIMEText(subtype='html');否則 'plain'。
    - 有附件 -> 包成 multipart/mixed,內文在前、附件在後。
    - 回傳 (msg, all_recipients):all_recipients = to + cc + bcc(SMTP sendmail 用)。
    """
    subtype = "html" if spec.html else "plain"
    text_part = MIMEText(spec.body, subtype, "utf-8")

    if spec.attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(text_part)
        for path in spec.attachments:
            _attach_file(msg, path)
    else:
        msg = text_part

    if spec.from_addr:
        msg["From"] = formataddr((spec.from_name or "", spec.from_addr))
    if spec.to:
        msg["To"] = ", ".join(spec.to)
    if spec.cc:
        msg["Cc"] = ", ".join(spec.cc)
    msg["Subject"] = spec.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    # 注意:Bcc 不寫進 header(避免洩露),只放進信封收件人。

    all_recipients = list(spec.to) + list(spec.cc) + list(spec.bcc)
    return msg, all_recipients


def _attach_file(msg, path: str) -> None:
    """把一個本機檔案掛成附件;檔不存在則 raise FileNotFoundError(讓上層回 ok=False)。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"附件不存在: {path}")
    with open(path, "rb") as fh:
        data = fh.read()
    part = MIMEApplication(data, Name=os.path.basename(path))
    part["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
    msg.attach(part)


# --------------------------------------------------------------------------- #
# Outlook COM 路徑
# --------------------------------------------------------------------------- #
def _import_win32com():
    """lazy import win32com;失敗回 None(無 pywin32 環境不該在 import 期爆)。"""
    try:
        import win32com.client  # type: ignore
        return win32com.client
    except Exception:
        return None


def outlook_available() -> bool:
    """偵測能否驅動 Outlook(有 pywin32 且能起 Outlook.Application)。"""
    client = _import_win32com()
    if client is None:
        return False
    try:
        client.Dispatch("Outlook.Application")
        return True
    except Exception:
        return False


def send_via_outlook(spec: EmailSpec, save_as_draft: bool = False):
    """用 Outlook COM 寄信 / 存草稿。

    回傳 (ok: bool, info_or_error: str)。
      - 無 pywin32 / 無 Outlook -> (False, reason),上層可據此降級 SMTP。
      - save_as_draft=True -> 只 .Save() 不 .Send()。
    """
    client = _import_win32com()
    if client is None:
        return False, "pywin32(win32com) 未安裝,無法使用 Outlook COM"
    try:
        app = client.Dispatch("Outlook.Application")
    except Exception as e:  # noqa: BLE001
        return False, f"無法啟動 Outlook(可能未安裝/未登入): {type(e).__name__}: {e}"

    try:
        mail = app.CreateItem(0)  # 0 = olMailItem
        mail.Subject = spec.subject
        if spec.html:
            mail.HTMLBody = spec.body
        else:
            mail.Body = spec.body
        if spec.to:
            mail.To = "; ".join(spec.to)
        if spec.cc:
            mail.CC = "; ".join(spec.cc)
        if spec.bcc:
            mail.BCC = "; ".join(spec.bcc)
        for path in spec.attachments:
            if not os.path.isfile(path):
                return False, f"附件不存在: {path}"
            mail.Attachments.Add(os.path.abspath(path))
        if save_as_draft:
            mail.Save()
            return True, "draft saved (Outlook)"
        mail.Send()
        return True, "sent (Outlook)"
    except Exception as e:  # noqa: BLE001
        return False, f"Outlook 寄送失敗: {type(e).__name__}: {e}"


def reply_via_outlook(entry_id: str, body: str, reply_all: bool = False,
                      html: bool = True, attachments=None,
                      save_as_draft: bool = False):
    """回覆既有信件(以 EntryID 取得原信)。

    回傳 (ok, info_or_error)。entry_id 通常來自 Outlook 收信動作回填的變數。
    """
    client = _import_win32com()
    if client is None:
        return False, "pywin32(win32com) 未安裝,無法使用 Outlook COM"
    if not entry_id:
        return False, "reply 缺少 entry_id"
    try:
        app = client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        original = ns.GetItemFromID(entry_id)
    except Exception as e:  # noqa: BLE001
        return False, f"無法取得原始信件(entry_id={entry_id}): {type(e).__name__}: {e}"

    try:
        reply = original.ReplyAll() if reply_all else original.Reply()
        if html:
            # 把新內文放在原信引用之前(append 在前面)
            reply.HTMLBody = (body or "") + (reply.HTMLBody or "")
        else:
            reply.Body = (body or "") + (reply.Body or "")
        for path in (attachments or []):
            if os.path.isfile(path):
                reply.Attachments.Add(os.path.abspath(path))
        if save_as_draft:
            reply.Save()
            return True, "reply draft saved (Outlook)"
        reply.Send()
        return True, "reply sent (Outlook)"
    except Exception as e:  # noqa: BLE001
        return False, f"Outlook 回覆失敗: {type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# SMTP 路徑
# --------------------------------------------------------------------------- #
@dataclass
class SmtpConfig:
    host: str = "localhost"
    port: int = 25
    user: str = ""
    password: str = ""
    use_tls: bool = False     # starttls
    use_ssl: bool = False     # SMTP_SSL
    timeout: float = 30.0


def send_via_smtp(spec: EmailSpec, cfg: SmtpConfig):
    """用 SMTP 寄信。回傳 (ok, info_or_error)。

    - 帳密由上層自 Vault 取好放進 cfg,本函式不碰 Vault。
    - use_ssl -> SMTP_SSL;否則 SMTP,use_tls 時 starttls。
    - 有 user/password 才 login(本機 debugging server 通常不需登入)。
    """
    if not spec.to and not spec.cc and not spec.bcc:
        return False, "SMTP 寄送缺少收件人"
    try:
        msg, recipients = build_mime_message(spec)
    except Exception as e:  # noqa: BLE001 — 附件不存在等
        return False, f"組信件失敗: {type(e).__name__}: {e}"

    sender = spec.from_addr or cfg.user or "no-reply@localhost"
    try:
        if cfg.use_ssl:
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        try:
            server.ehlo()
            if cfg.use_tls and not cfg.use_ssl:
                server.starttls()
                server.ehlo()
            if cfg.user and cfg.password:
                server.login(cfg.user, cfg.password)
            server.sendmail(sender, recipients, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True, f"sent (SMTP {cfg.host}:{cfg.port}, {len(recipients)} recipient(s))"
    except Exception as e:  # noqa: BLE001
        return False, f"SMTP 寄送失敗: {type(e).__name__}: {e}"
