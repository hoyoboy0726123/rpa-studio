# -*- coding: utf-8 -*-
"""comms.* 動作註冊 — email.* / sharepoint.*。

每個動作簽名 `def fn(ctx, step) -> ActionResult`,不需要 ctx.engine。
params 字串值的 {var} 替換由 runner 在呼叫前處理(與 flow.* 一致)。

機密一律走 Vault:flow JSON 只放 **secret 名稱**,實際值在 action 內以
ctx.vault.get_secret(name) 取出,不落地、不回填進 ActionResult / step log。

Vault secret 約定:
  email.send (SMTP) :
      params.smtp = {host, port, user_secret, pass_secret, use_tls, use_ssl}
      user_secret / pass_secret 為 Vault secret 名稱。
  sharepoint.*      :
      params.client_id 直接給,或 params.client_id_secret 走 Vault。
      (client_id 屬公開資訊,可直接放;但允許走 Vault 以便集中管理。)
"""
from __future__ import annotations

from core.registry import action, ActionResult
from . import email_send as _email
from . import sharepoint as _sp


# --------------------------------------------------------------------------- #
# 共用:從 Vault 取值(secret 名稱 -> 值);取不到回 default
# --------------------------------------------------------------------------- #
def _secret(ctx, name, default=""):
    if not name or ctx.vault is None:
        return default
    try:
        v = ctx.vault.get_secret(name)
        return v if v is not None else default
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------- #
# email.send
# --------------------------------------------------------------------------- #
@action("email.send")
def email_send(ctx, step) -> ActionResult:
    """寄信:Outlook COM 優先,無 Outlook 自動降級 SMTP。

    params:
      to, cc, bcc        : 收件人(str 以 ; 或 , 分隔 / list)
      subject, body      : 主旨 / 內文
      html               : 內文是否為 HTML(預設 True)
      attachments        : 本機檔案路徑(str / list)
      save_as_draft      : 只存草稿不寄(預設 False)
      prefer             : 'outlook' | 'smtp' | 'auto'(預設 auto)
      smtp               : {host, port, user_secret, pass_secret, use_tls, use_ssl,
                            from_addr, from_name} — SMTP 路徑用,帳密走 Vault
      result_var         : 把結果訊息寫進此變數(選用)
    """
    p = step.params or {}
    spec = _email.normalize_spec(
        to=p.get("to"), cc=p.get("cc"), bcc=p.get("bcc"),
        subject=p.get("subject", ""), body=p.get("body", ""),
        html=p.get("html", True), attachments=p.get("attachments"),
        from_addr=(p.get("smtp") or {}).get("from_addr", ""),
        from_name=(p.get("smtp") or {}).get("from_name", ""),
    )
    save_as_draft = bool(p.get("save_as_draft", False))
    prefer = str(p.get("prefer", "auto")).lower()
    result_var = p.get("result_var")

    info = ""
    ok = False

    # ---- Outlook 優先(prefer outlook/auto 且偵測得到) ---- #
    tried_outlook = False
    if prefer in ("outlook", "auto"):
        if prefer == "outlook" or _email.outlook_available():
            tried_outlook = True
            ok, info = _email.send_via_outlook(spec, save_as_draft=save_as_draft)
            if ok:
                if result_var:
                    ctx.vars.set(result_var, info)
                return ActionResult(ok=True, value=info)
            ctx.log(f"email.send: Outlook 路徑失敗,嘗試降級 SMTP。{info}")

    # ---- SMTP fallback ---- #
    if prefer in ("smtp", "auto") or tried_outlook:
        smtp_p = p.get("smtp") or {}
        cfg = _email.SmtpConfig(
            host=smtp_p.get("host", "localhost"),
            port=int(smtp_p.get("port", 25) or 25),
            user=_secret(ctx, smtp_p.get("user_secret"), smtp_p.get("user", "")),
            password=_secret(ctx, smtp_p.get("pass_secret"), ""),
            use_tls=bool(smtp_p.get("use_tls", False)),
            use_ssl=bool(smtp_p.get("use_ssl", False)),
            timeout=float(smtp_p.get("timeout", 30) or 30),
        )
        ok, info = _email.send_via_smtp(spec, cfg)
        if result_var:
            ctx.vars.set(result_var, info)
        return ActionResult(ok=ok, value=info if ok else None,
                            error="" if ok else info)

    # prefer=outlook 但無 Outlook 且沒降級條件
    if result_var:
        ctx.vars.set(result_var, info)
    return ActionResult(ok=ok, value=info if ok else None,
                        error="" if ok else (info or "email.send 未能寄出"))


# --------------------------------------------------------------------------- #
# email.reply
# --------------------------------------------------------------------------- #
@action("email.reply")
def email_reply(ctx, step) -> ActionResult:
    """回覆既有信件(Outlook COM)。

    params:
      entry_id / entry_id_var : 原信 EntryID(直接給 或 從變數取)
      body                    : 回覆內文
      reply_all               : 是否回覆全部(預設 False)
      html                    : 內文是否 HTML(預設 True)
      attachments             : 附件(str / list)
      save_as_draft           : 只存草稿不寄(預設 False)
      result_var              : 結果訊息寫進此變數(選用)
    """
    p = step.params or {}
    entry_id = p.get("entry_id")
    if not entry_id and p.get("entry_id_var"):
        entry_id = ctx.vars.get(p.get("entry_id_var"))

    ok, info = _email.reply_via_outlook(
        entry_id=entry_id,
        body=p.get("body", ""),
        reply_all=bool(p.get("reply_all", False)),
        html=bool(p.get("html", True)),
        attachments=_email._as_list(p.get("attachments")),
        save_as_draft=bool(p.get("save_as_draft", False)),
    )
    if p.get("result_var"):
        ctx.vars.set(p["result_var"], info)
    return ActionResult(ok=ok, value=info if ok else None,
                        error="" if ok else info)


# --------------------------------------------------------------------------- #
# SharePoint / OneDrive 共用:組 GraphAuth + 取 token + 建 client
# --------------------------------------------------------------------------- #
def _build_client(ctx, p):
    """回傳 (client_or_None, error)。token 失敗 / 無 msal 都 graceful。"""
    client_id = p.get("client_id") or _secret(ctx, p.get("client_id_secret"), "")
    auth = _sp.GraphAuth(
        client_id=client_id,
        tenant=p.get("tenant", "common"),
        scopes=p.get("scopes") or list(_sp.DEFAULT_SCOPES),
        cache_path=p.get("cache_path", ".msal_cache.bin"),
        prompt_cb=(ctx.extra or {}).get("device_code_cb"),
    )
    token, err = auth.acquire_token(log=ctx.log)
    if not token:
        return None, err
    return _sp.GraphClient(token, drive=p.get("drive", "me")), ""


@action("sharepoint.mkdir")
def sharepoint_mkdir(ctx, step) -> ActionResult:
    """建立資料夾(沿路徑逐段建)。params: folder_path, client_id/tenant/drive..."""
    p = step.params or {}
    client, err = _build_client(ctx, p)
    if client is None:
        return ActionResult(ok=False, error=err)
    ok, info = client.mkdir(p.get("folder_path", ""))
    if p.get("result_var"):
        ctx.vars.set(p["result_var"], info)
    return ActionResult(ok=ok, value=info if ok else None,
                        error="" if ok else str(info))


@action("sharepoint.upload")
def sharepoint_upload(ctx, step) -> ActionResult:
    """上傳檔案(小檔直傳 / 大檔分塊)。

    params: local_path, remote_folder, remote_name, conflict(replace|rename|fail),
            client_id/tenant/drive..., result_var
    """
    p = step.params or {}
    if not p.get("local_path"):
        return ActionResult(ok=False, error="sharepoint.upload 缺少 local_path")
    client, err = _build_client(ctx, p)
    if client is None:
        return ActionResult(ok=False, error=err)
    ok, info = client.upload(
        local_path=p.get("local_path"),
        remote_folder=p.get("remote_folder", ""),
        remote_name=p.get("remote_name", ""),
        conflict=p.get("conflict", "replace"),
    )
    if p.get("result_var"):
        ctx.vars.set(p["result_var"], info)
    return ActionResult(ok=ok, value=info if ok else None,
                        error="" if ok else str(info))


@action("sharepoint.delete_old")
def sharepoint_delete_old(ctx, step) -> ActionResult:
    """依名稱樣式汰舊。dry_run 預設 True(只列不刪)。

    params: folder_path, name_contains, name_regex, dry_run, client_id/tenant/drive...
    """
    p = step.params or {}
    client, err = _build_client(ctx, p)
    if client is None:
        return ActionResult(ok=False, error=err)
    ok, info = client.delete_old(
        folder_path=p.get("folder_path", ""),
        name_contains=p.get("name_contains", ""),
        name_regex=p.get("name_regex", ""),
        dry_run=bool(p.get("dry_run", True)),
    )
    if p.get("result_var"):
        ctx.vars.set(p["result_var"], info)
    return ActionResult(ok=ok, value=info if ok else None,
                        error="" if ok else str(info))


@action("sharepoint.share_link")
def sharepoint_share_link(ctx, step) -> ActionResult:
    """建立 view-only 分享連結。

    params: item_path, scope(anonymous|organization), client_id/tenant/drive...,
            result_var(把連結寫進變數)
    """
    p = step.params or {}
    client, err = _build_client(ctx, p)
    if client is None:
        return ActionResult(ok=False, error=err)
    link, err2 = client.share_link(
        item_path=p.get("item_path", ""),
        scope=p.get("scope", "anonymous"),
    )
    if link is None:
        return ActionResult(ok=False, error=err2)
    if p.get("result_var"):
        ctx.vars.set(p["result_var"], link)
    return ActionResult(ok=True, value=link)
