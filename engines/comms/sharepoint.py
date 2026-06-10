# -*- coding: utf-8 -*-
"""SharePoint / OneDrive — Microsoft Graph + msal device-code flow。

對齊參考 RPA 的「雲端上傳」能力,原創實作:

  認證 (GraphAuth):
    - msal PublicClientApplication + **device-code flow**:
      使用者拿一組 code 到 https://microsoft.com/devicelogin 完成登入,
      無頭 / 排程環境也能跑(不需內嵌瀏覽器)。
    - token 走 msal **SerializableTokenCache** 持久化到檔案(預設專案根 .msal_cache.bin),
      下次啟動先試 silent(refresh token),省得重複登入。
    - client_id / tenant / cache 路徑由上層自 Vault / 設定取好傳入;本模組不碰 Vault。

  檔案操作 (GraphClient):
    - mkdir(folder_path)            : 沿路徑逐段建立資料夾(已存在則略過)。
    - upload(local, remote_folder, remote_name, conflict):
        小檔(< 4MB)PUT /content 直傳;大檔走 createUploadSession 分塊上傳。
    - delete_old(folder, name_contains/name_regex, dry_run): 依名稱樣式汰舊。
    - share_link(item_path, scope): 建立 view-only 分享連結。

設計原則:
  - msal / requests **lazy import**;無套件時 ensure_token()/各操作回友善錯誤,**不在 import 期爆**。
  - 純函式(URL 組裝 / chunk 切片 / 樣式比對)抽出來,離線可單元測試。
  - 真正打 Graph 需有效 O365 帳號 -> 端到端只能在真環境測。
"""
from __future__ import annotations

import os
import re
import json
import math
from dataclasses import dataclass, field

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/{tenant}"
DEFAULT_SCOPES = ["Files.ReadWrite.All", "Sites.ReadWrite.All"]
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024          # 4 MB:超過走 upload session
UPLOAD_CHUNK = 5 * 1024 * 1024                 # 5 MB / 塊(須為 320KiB 倍數)


# --------------------------------------------------------------------------- #
# lazy import
# --------------------------------------------------------------------------- #
def _import_msal():
    try:
        import msal  # type: ignore
        return msal
    except Exception:
        return None


def _import_requests():
    try:
        import requests  # type: ignore
        return requests
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 純函式:URL / drive 端點組裝(離線可測)
# --------------------------------------------------------------------------- #
def normalize_remote_path(path: str) -> str:
    """把使用者給的遠端路徑正規化:統一用 '/'、去頭尾斜線。"""
    if not path:
        return ""
    p = str(path).replace("\\", "/").strip()
    return p.strip("/")


def drive_root_url(drive: str = "me") -> str:
    """回傳 drive 根端點。drive='me' -> /me/drive;否則視為 driveId -> /drives/{id}。"""
    if not drive or drive == "me":
        return f"{GRAPH_ROOT}/me/drive"
    return f"{GRAPH_ROOT}/drives/{drive}"


def item_by_path_url(remote_path: str, drive: str = "me") -> str:
    """以路徑定位 driveItem 的端點。空路徑 -> root。"""
    root = drive_root_url(drive)
    rp = normalize_remote_path(remote_path)
    if not rp:
        return f"{root}/root"
    return f"{root}/root:/{rp}"


def upload_content_url(remote_path: str, drive: str = "me") -> str:
    """小檔直傳端點:PUT .../root:/{path}:/content。"""
    root = drive_root_url(drive)
    rp = normalize_remote_path(remote_path)
    return f"{root}/root:/{rp}:/content"


def create_session_url(remote_path: str, drive: str = "me") -> str:
    """大檔上傳 session 端點:POST .../root:/{path}:/createUploadSession。"""
    root = drive_root_url(drive)
    rp = normalize_remote_path(remote_path)
    return f"{root}/root:/{rp}:/createUploadSession"


def plan_chunks(file_size: int, chunk: int = UPLOAD_CHUNK):
    """把檔案大小切成 (start, end_inclusive, length) 區段(純函式)。

    Content-Range 用閉區間 end,故 end = start + length - 1。
    """
    if file_size <= 0:
        return []
    n = math.ceil(file_size / chunk)
    out = []
    for i in range(n):
        start = i * chunk
        length = min(chunk, file_size - start)
        out.append((start, start + length - 1, length))
    return out


def content_range_header(start: int, end: int, total: int) -> str:
    return f"bytes {start}-{end}/{total}"


def name_matches(name: str, name_contains: str = "", name_regex: str = "") -> bool:
    """檔名是否符合汰舊樣式(純函式)。

    - name_regex 優先(re.search,大小寫敏感由樣式自理)。
    - 否則 name_contains 子字串比對(大小寫不敏感)。
    - 兩者皆空 -> False(避免誤刪全部)。
    """
    if name_regex:
        try:
            return re.search(name_regex, name) is not None
        except re.error:
            return False
    if name_contains:
        return name_contains.lower() in (name or "").lower()
    return False


# --------------------------------------------------------------------------- #
# 認證:msal device-code flow + token cache
# --------------------------------------------------------------------------- #
@dataclass
class GraphAuth:
    client_id: str = ""
    tenant: str = "common"
    scopes: list = field(default_factory=lambda: list(DEFAULT_SCOPES))
    cache_path: str = ".msal_cache.bin"
    # device-code 提示回呼:fn(verification_uri, user_code, message)。
    # 無回呼(headless)時把訊息寫進 log 即可,使用者仍須自行完成裝置登入。
    prompt_cb: object = None

    def _build_app(self, msal):
        cache = msal.SerializableTokenCache()
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                cache.deserialize(open(self.cache_path, "r", encoding="utf-8").read())
            except Exception:
                pass
        authority = DEFAULT_AUTHORITY.format(tenant=self.tenant or "common")
        app = msal.PublicClientApplication(
            self.client_id, authority=authority, token_cache=cache,
        )
        return app, cache

    def _save_cache(self, cache) -> None:
        if not self.cache_path:
            return
        try:
            if cache.has_state_changed:
                with open(self.cache_path, "w", encoding="utf-8") as fh:
                    fh.write(cache.serialize())
        except Exception:
            pass

    def acquire_token(self, log=None):
        """取得 access token。

        回傳 (token_or_None, error_or_empty)。
          1. 無 msal -> graceful 友善錯誤。
          2. 先試 acquire_token_silent(快取/refresh)。
          3. 失敗則走 device-code flow:透過 prompt_cb 顯示 code,輪詢直到完成。
        """
        msal = _import_msal()
        if msal is None:
            return None, "msal 未安裝:pip install msal 後才能連 Microsoft Graph"
        if not self.client_id:
            return None, "缺少 client_id(Azure AD 應用程式 ID),無法認證"

        def _log(m):
            if callable(log):
                log(m)

        try:
            app, cache = self._build_app(msal)
        except Exception as e:  # noqa: BLE001
            return None, f"建立 msal app 失敗: {type(e).__name__}: {e}"

        # ---- 1) silent ---- #
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(self.scopes, account=accounts[0])
            if result and "access_token" in result:
                self._save_cache(cache)
                return result["access_token"], ""

        # ---- 2) device-code flow ---- #
        try:
            flow = app.initiate_device_flow(scopes=self.scopes)
        except Exception as e:  # noqa: BLE001
            return None, f"啟動 device-code flow 失敗: {type(e).__name__}: {e}"
        if "user_code" not in flow:
            return None, f"device-code flow 回應異常: {json.dumps(flow, ensure_ascii=False)}"

        msg = flow.get("message", "")
        uri = flow.get("verification_uri", "https://microsoft.com/devicelogin")
        code = flow.get("user_code", "")
        _log(f"[Graph 裝置登入] 請至 {uri} 輸入代碼: {code}")
        if callable(self.prompt_cb):
            try:
                self.prompt_cb(uri, code, msg)
            except Exception:
                pass

        try:
            result = app.acquire_token_by_device_flow(flow)  # 阻塞輪詢直到完成/逾時
        except Exception as e:  # noqa: BLE001
            return None, f"device-code 取 token 失敗: {type(e).__name__}: {e}"

        if result and "access_token" in result:
            self._save_cache(cache)
            return result["access_token"], ""
        err = result.get("error_description") or result.get("error") or "unknown"
        return None, f"取 token 失敗: {err}"


# --------------------------------------------------------------------------- #
# Graph 檔案操作
# --------------------------------------------------------------------------- #
class GraphClient:
    """以 access token 對 OneDrive / SharePoint drive 做檔案操作。

    drive: 'me' -> 個人 OneDrive(/me/drive);或傳 SharePoint 的 driveId。
    """

    def __init__(self, token: str, drive: str = "me", session=None):
        self.token = token
        self.drive = drive
        requests = _import_requests()
        if session is not None:
            self.session = session
        elif requests is not None:
            self.session = requests.Session()
        else:
            self.session = None

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.token}"}
        if extra:
            h.update(extra)
        return h

    def _ensure(self):
        if self.session is None:
            return False, "requests 未安裝:無法呼叫 Microsoft Graph"
        return True, ""

    # ---- mkdir:逐段建立 ---- #
    def mkdir(self, folder_path: str):
        """沿路徑逐段建立資料夾(已存在則略過)。回傳 (ok, info_or_error)。"""
        ok, err = self._ensure()
        if not ok:
            return False, err
        rp = normalize_remote_path(folder_path)
        if not rp:
            return True, "root 已存在"
        segments = rp.split("/")
        parent = ""  # 相對 root 的已建路徑
        for seg in segments:
            parent_url = item_by_path_url(parent, self.drive) + (":/children" if parent else "/children")
            payload = {
                "name": seg,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "replace",
            }
            try:
                resp = self.session.post(parent_url, headers=self._headers(
                    {"Content-Type": "application/json"}),
                    data=json.dumps(payload), timeout=30)
            except Exception as e:  # noqa: BLE001
                return False, f"mkdir '{seg}' 請求失敗: {type(e).__name__}: {e}"
            if resp.status_code not in (200, 201, 409):
                return False, f"mkdir '{seg}' 失敗 HTTP {resp.status_code}: {resp.text[:200]}"
            parent = f"{parent}/{seg}" if parent else seg
        return True, f"已建立資料夾: {rp}"

    # ---- upload ---- #
    def upload(self, local_path: str, remote_folder: str = "",
               remote_name: str = "", conflict: str = "replace"):
        """上傳本機檔案。小檔直傳、大檔分塊。回傳 (ok, info_or_error)。

        conflict: replace | rename | fail(Graph @microsoft.graph.conflictBehavior)。
        """
        ok, err = self._ensure()
        if not ok:
            return False, err
        if not os.path.isfile(local_path):
            return False, f"本機檔案不存在: {local_path}"
        name = remote_name or os.path.basename(local_path)
        remote_path = "/".join([p for p in (normalize_remote_path(remote_folder), name) if p])
        size = os.path.getsize(local_path)

        if size < SIMPLE_UPLOAD_LIMIT:
            return self._upload_small(local_path, remote_path)
        return self._upload_large(local_path, remote_path, size, conflict)

    def _upload_small(self, local_path: str, remote_path: str):
        url = upload_content_url(remote_path, self.drive)
        try:
            with open(local_path, "rb") as fh:
                resp = self.session.put(url, headers=self._headers(
                    {"Content-Type": "application/octet-stream"}),
                    data=fh, timeout=120)
        except Exception as e:  # noqa: BLE001
            return False, f"小檔上傳請求失敗: {type(e).__name__}: {e}"
        if resp.status_code in (200, 201):
            return True, f"已上傳(直傳): {remote_path}"
        return False, f"小檔上傳失敗 HTTP {resp.status_code}: {resp.text[:200]}"

    def _upload_large(self, local_path: str, remote_path: str, size: int, conflict: str):
        sess_url = create_session_url(remote_path, self.drive)
        body = {"item": {"@microsoft.graph.conflictBehavior": conflict}}
        try:
            r = self.session.post(sess_url, headers=self._headers(
                {"Content-Type": "application/json"}),
                data=json.dumps(body), timeout=30)
        except Exception as e:  # noqa: BLE001
            return False, f"建立上傳 session 失敗: {type(e).__name__}: {e}"
        if r.status_code not in (200, 201):
            return False, f"建立上傳 session 失敗 HTTP {r.status_code}: {r.text[:200]}"
        upload_url = r.json().get("uploadUrl")
        if not upload_url:
            return False, "上傳 session 回應缺 uploadUrl"

        try:
            with open(local_path, "rb") as fh:
                for start, end, length in plan_chunks(size):
                    fh.seek(start)
                    chunk = fh.read(length)
                    headers = {
                        "Content-Length": str(length),
                        "Content-Range": content_range_header(start, end, size),
                    }
                    cr = self.session.put(upload_url, headers=headers,
                                          data=chunk, timeout=300)
                    if cr.status_code not in (200, 201, 202):
                        return False, (f"分塊上傳失敗 [{start}-{end}] "
                                       f"HTTP {cr.status_code}: {cr.text[:200]}")
        except Exception as e:  # noqa: BLE001
            return False, f"分塊上傳請求失敗: {type(e).__name__}: {e}"
        return True, f"已上傳(分塊 {len(plan_chunks(size))} 塊): {remote_path}"

    # ---- list children(供 delete_old 用) ---- #
    def list_children(self, folder_path: str):
        """列出資料夾下的項目。回傳 (items_or_None, error)。"""
        ok, err = self._ensure()
        if not ok:
            return None, err
        rp = normalize_remote_path(folder_path)
        base = item_by_path_url(rp, self.drive)
        url = (base + ":/children") if rp else (base + "/children")
        items = []
        try:
            while url:
                resp = self.session.get(url, headers=self._headers(), timeout=30)
                if resp.status_code != 200:
                    return None, f"列目錄失敗 HTTP {resp.status_code}: {resp.text[:200]}"
                data = resp.json()
                items.extend(data.get("value", []))
                url = data.get("@odata.nextLink")
        except Exception as e:  # noqa: BLE001
            return None, f"列目錄請求失敗: {type(e).__name__}: {e}"
        return items, ""

    def delete_old(self, folder_path: str, name_contains: str = "",
                   name_regex: str = "", dry_run: bool = True):
        """依名稱樣式汰舊。回傳 (ok, info_dict_or_error)。

        dry_run=True(預設)只列出將被刪的項目,不真的刪 — 安全第一。
        """
        items, err = self.list_children(folder_path)
        if items is None:
            return False, err
        matched = [it for it in items
                   if name_matches(it.get("name", ""), name_contains, name_regex)]
        names = [it.get("name", "") for it in matched]
        if dry_run:
            return True, {"dry_run": True, "would_delete": names, "count": len(names)}

        deleted, errors = [], []
        for it in matched:
            item_id = it.get("id")
            url = f"{drive_root_url(self.drive)}/items/{item_id}"
            try:
                resp = self.session.delete(url, headers=self._headers(), timeout=30)
                if resp.status_code in (200, 204):
                    deleted.append(it.get("name", ""))
                else:
                    errors.append(f"{it.get('name')}: HTTP {resp.status_code}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{it.get('name')}: {type(e).__name__}: {e}")
        return (len(errors) == 0), {"dry_run": False, "deleted": deleted,
                                    "errors": errors, "count": len(deleted)}

    def share_link(self, item_path: str, scope: str = "anonymous"):
        """為項目建立 view-only 分享連結。回傳 (link_or_None, error)。

        scope: anonymous(任何人有連結即可看)| organization(僅組織內)。
        """
        ok, err = self._ensure()
        if not ok:
            return None, err
        rp = normalize_remote_path(item_path)
        url = item_by_path_url(rp, self.drive) + ":/createLink"
        payload = {"type": "view", "scope": scope}
        try:
            resp = self.session.post(url, headers=self._headers(
                {"Content-Type": "application/json"}),
                data=json.dumps(payload), timeout=30)
        except Exception as e:  # noqa: BLE001
            return None, f"建立分享連結請求失敗: {type(e).__name__}: {e}"
        if resp.status_code not in (200, 201):
            return None, f"建立分享連結失敗 HTTP {resp.status_code}: {resp.text[:200]}"
        link = (resp.json().get("link") or {}).get("webUrl")
        if not link:
            return None, "分享連結回應缺 webUrl"
        return link, ""
