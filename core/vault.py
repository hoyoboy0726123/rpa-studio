# -*- coding: utf-8 -*-
"""憑證保管 Vault — Robocorp Vault 式抽象:同一 API、可換後端。
優先 OS keyring(Windows Credential Manager),fallback 本機 Fernet 加密檔。
secret 名稱寫進 flow,實際值絕不入 flow JSON / 不進 SQLite 明碼 / 不進 git。
"""
from __future__ import annotations
import os
import json
import base64

SERVICE = "rpa_studio"


class Vault:
    def __init__(self, base_dir: str = "."):
        self.base_dir = base_dir
        self._enc_path = os.path.join(base_dir, ".secrets.enc")
        self._key_path = os.path.join(base_dir, ".vault_key")

    # ---- keyring 後端 ---- #
    def _keyring(self):
        try:
            import keyring
            return keyring
        except Exception:
            return None

    # ---- Fernet 加密檔後端 ---- #
    def _fernet(self):
        from cryptography.fernet import Fernet
        if not os.path.exists(self._key_path):
            key = Fernet.generate_key()
            with open(self._key_path, "wb") as f:
                f.write(key)
            try:
                os.chmod(self._key_path, 0o600)
            except Exception:
                pass
        with open(self._key_path, "rb") as f:
            return Fernet(f.read())

    def _load_enc(self) -> dict:
        if not os.path.exists(self._enc_path):
            return {}
        try:
            token = open(self._enc_path, "rb").read()
            data = self._fernet().decrypt(token)
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    def _save_enc(self, d: dict) -> None:
        token = self._fernet().encrypt(json.dumps(d).encode("utf-8"))
        with open(self._enc_path, "wb") as f:
            f.write(token)

    # ---- 公開 API ---- #
    def set_secret(self, name: str, value: str) -> None:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.set_password(SERVICE, name, value)
                return
            except Exception:
                pass
        d = self._load_enc()
        d[name] = value
        self._save_enc(d)

    def get_secret(self, name: str) -> str | None:
        kr = self._keyring()
        if kr is not None:
            try:
                v = kr.get_password(SERVICE, name)
                if v is not None:
                    return v
            except Exception:
                pass
        return self._load_enc().get(name)

    def delete_secret(self, name: str) -> None:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.delete_password(SERVICE, name)
            except Exception:
                pass
        d = self._load_enc()
        if name in d:
            del d[name]
            self._save_enc(d)

    def list_names(self) -> list:
        # keyring 無法列舉;以加密檔記錄的名稱為準(僅供 UI 顯示,值不外洩)
        return sorted(self._load_enc().keys())
