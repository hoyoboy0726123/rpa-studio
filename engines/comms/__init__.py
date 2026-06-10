# -*- coding: utf-8 -*-
"""通訊動作引擎 (comms engine):Email + SharePoint/OneDrive。

對齊參考 RPA 的「寄信」與「雲端上傳」能力,但為原創實作:

  - email_send.py  : Outlook COM 優先,SMTP fallback;HTML 內文 / 附件 / 草稿 / 回覆。
  - sharepoint.py  : MS Graph + msal device-code flow;mkdir / upload / delete_old / share_link。
  - actions.py     : 把上述能力以 @action 註冊成 email.* / sharepoint.* 動作。

設計原則(與 vision 引擎一致):
  - 重相依(pywin32 / msal / requests)一律 **lazy import**,import 本套件本身不該爆。
  - 缺 Outlook / 缺 msal / 無網路一律 **graceful**:回 ActionResult(ok=False, error=...)
    或自動降級(Outlook -> SMTP),不可讓整個工具崩。
  - 帳密 / token 一律走 Vault(secret 名稱),不落地、不進 flow JSON。
"""
from . import actions  # noqa: F401  匯入即註冊 email.* / sharepoint.*
