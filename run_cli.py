# -*- coding: utf-8 -*-
"""Headless CLI 入口:供 Windows 工作排程器無人值守呼叫。

用法:
    python run_cli.py --flow "<flow name>"                 # 跑 Store 內某條 flow
    python run_cli.py --file flows/sample.json              # 直接跑某個 flow JSON
    python run_cli.py --flow X --var key=value --var k2=v2  # 覆寫變數
    python run_cli.py --flow X --unattended                 # 無人值守:不卡在 MFA 暫停
    python run_cli.py --flow X --service-account svc_login   # 注入服務帳號 secret

設計重點:
- **不依賴 PySide6 / ui.***。本檔只 import core.* / engines.*,因此可在「沒有安裝
  PySide6 的無人值守機器」上執行。實際 wiring 走 core.headless.run_flow_headless()。
- attended(有人值守、UI)仍走 ui.run_worker;CLI 是另一條獨立路徑,不互相牽連。

無人值守(--unattended)前提
----------------------------
無人值守要能真正跑完,前提是目標系統用 **服務帳號 / 免 MFA** 登入(對應先前評估的
結論:本工具不做「MFA 自動繞過」)。--unattended 會讓 flow.pause_for_human **不等人、
立即繼續**;若該系統仍強制 MFA,登入步驟會失敗——這是預期行為,請改用 IT 服務帳號或
免 MFA 的內部入口。服務帳號密碼存進 Vault(以 secret 名稱引用),用 --service-account
注入,登入步驟以 secret_ref 或 ctx.extra['service_account'] 取用,密碼不落地、不進指令列。
"""
from __future__ import annotations
import os
import sys
import argparse
import threading

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.schema import Flow
from core.store import Store
from core.vault import Vault
from core.headless import run_flow_headless
from core.actions_bootstrap import register_builtin_actions  # 註冊 flow/data/comms 動作
register_builtin_actions()


def _parse_vars(pairs) -> dict:
    """把多個 'key=value' 解析成 dict。value 可含 '='(只切第一個)。"""
    out: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"--var 需為 key=value 格式,收到:{p!r}")
        k, v = p.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"--var 的 key 不可空:{p!r}")
        out[k] = v
    return out


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="RPA Studio headless runner")
    ap.add_argument("--flow", help="Store 內的 flow 名稱")
    ap.add_argument("--file", help="直接執行某個 flow JSON 檔")
    ap.add_argument("--var", action="append", default=[], metavar="k=v",
                    help="覆寫流程變數(可多次):--var key=value")
    # headless 預設為 true;保留 --headless 旗標供明示(CLI 本就無 GUI)。
    ap.add_argument("--headless", dest="headless", action="store_true", default=True,
                    help="不開 GUI 執行(預設,CLI 本就 headless)")
    ap.add_argument("--unattended", action="store_true", default=False,
                    help="無人值守:flow.pause_for_human 不等人、立即繼續(避免卡在 MFA)")
    ap.add_argument("--service-account", dest="service_account", default=None,
                    metavar="SECRET_NAME",
                    help="服務帳號 secret 名稱;把對應 Vault secret 注入 ctx.extra 供登入步驟取用")
    return ap


def main(argv=None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)

    try:
        overrides = _parse_vars(args.var)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    store = Store(os.path.join(_ROOT, "rpa_studio.db"))
    vault = Vault(_ROOT)

    if args.file:
        flow = Flow.load(args.file)
    elif args.flow:
        d = store.load_flow(args.flow)
        if not d:
            print(f"找不到流程:{args.flow}", file=sys.stderr)
            return 2
        flow = Flow.from_dict(d)
    else:
        ap.error("需指定 --flow 或 --file")
        return 2

    result = run_flow_headless(
        flow, store=store, vault=vault,
        stop_event=threading.Event(),
        overrides=overrides,
        log=lambda s: print(s),
        unattended=args.unattended,
        service_account=args.service_account,
    )
    print(f"=== {result.status} (ok={result.steps_ok} failed={result.steps_failed}) ===")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
