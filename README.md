# RPA Studio

一個 **PySide6 桌面 RPA 工具**：用「流程設定（JSON）」描述「開啟視窗/網頁 → 導覽 → 填表 → 抓資料/下載/寄信」，
用滑鼠**錄製**就能產生流程，支援**網頁 + 桌面雙引擎**、**多層定位自癒（self-healing）**、**OCR 文字辨識**、
**憑證保管**與 **Windows 排程**。一般使用者不必寫程式即可建立自動化。

> 📘 **完整手把手操作手冊**：見 [`RPA_Studio_操作SOP.docx`](RPA_Studio_操作SOP.docx)
> （含介面導覽、錄製/編輯/執行、OCR 框選、排程、憑證，以及「每個動作每個參數怎麼填」的完整參考）。

---

## 核心特色

- **GUI**：PySide6 原生桌面（深色側欄 + 卡片式內容），含螢幕透明 overlay、元素/區域框選器、錄製狀態燈。
- **雙引擎**：
  - **Web** — Playwright（auto-wait + role/text/css/xpath 定位，內建 codegen 錄製轉譯）。
  - **Desktop** — pywinauto UIA，**四層 fallback：UIA → win32 → 影像比對(CV) → 座標**。
- **流程模型**：扁平 JSON step + 多定位器（primary + fallbacks + fingerprint）。
- **動作擴充**：`@action("web.click")` registry，新增能力零侵入。
- **視覺層**：OpenCV 影像模板比對（`wait_image` / `image_click`）+ RapidOCR（`ocr_read`，框選區域辨識文字並當變數傳遞）。
- **流程控制**：`set_var` / `if` / `loop`（含巢狀）/ `prompt_user` / `pause_for_human`（MFA 人工暫停）/ `wait_file`（檔案觸發）/ `http`。
- **資料/通訊**：Excel/CSV 讀寫、拆檔、差異比對；Email 寄送/回覆、SharePoint 上傳/整理。
- **自癒 self-healing**：primary + fallback 全失敗時，用 fingerprint 在當前畫面評分挑最像候選並替換，
  **記 `heal_logs` 供人審核、不默默改檔**（門檻可調、可開關）。
- **憑證 Vault**：keyring + Fernet 加密檔 fallback；**secret 名稱進 flow、值不落地**（不進 flow JSON、不進指令列）。
- **儲存/日誌**：SQLite（flows / runs / step_logs / heal_logs）+ 失敗截圖。

---

## 安裝與啟動

需求：**Windows 10 / 11**，**Python 3.10+**。

### 方式 A — 一鍵啟動（最簡單，給一般使用者）
雙擊 **`start.bat`**。首次會用標準的 **`python -m venv` + `pip`** 自動建立虛擬環境、安裝依賴、
下載 Playwright Chromium，之後雙擊直接開。**不需要安裝 uv 或任何額外工具**——只要電腦有 Python 3.10–3.13。

### 方式 B — 使用 uv（開發者選用，安裝最快）
[uv](https://docs.astral.sh/uv/) 是高速 Python 套件/環境管理器。

```powershell
uv sync                         # 依 pyproject.toml 建立 .venv 並安裝全部依賴
uv run playwright install chromium
uv run python main.py
```

或不用 `uv sync`、直接對既有 venv 安裝：

```powershell
uv venv
uv pip install -r requirements.txt
uv run playwright install chromium
uv run python main.py
```

### 方式 C — 傳統 pip
```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python main.py
```

> `requirements.txt` 與 `pyproject.toml` 的依賴清單保持一致——pip 使用者讀前者，uv 使用者讀後者。

---

## 無人值守（unattended）+ 服務帳號

`run_cli.py` **不依賴 PySide6**，可在沒裝 GUI 套件的無人值守機器上跑
（wiring 走 `core/headless.py` 的 `run_flow_headless()`，只 import `core.*` / `engines.*`，完全不碰 `ui.*` / Qt）。

```powershell
python run_cli.py --flow <name> [--file <path>] [--var k=v ...] [--unattended] [--service-account <secret_name>]
```

| 參數 | 說明 |
|---|---|
| `--flow <name>` | 跑 Store 內某條 flow |
| `--file <path>` | 直接跑某個 flow JSON |
| `--var k=v`（可多次） | 覆寫流程變數（優先於 flow 預設值） |
| `--unattended` | 無人值守：`flow.pause_for_human` **不等人、立即繼續**（避免卡在 MFA / 人工暫停） |
| `--service-account <secret_name>` | 把對應 Vault secret 注入 `ctx.extra['service_account']`，供登入步驟取用 |

**`--unattended` 的前提**：無人值守機器沒有人能完成 MFA / OTP，所以 `--unattended` 會讓
`flow.pause_for_human` 記一筆 `[UNATTENDED]` 警告後立即繼續。要真正跑完，目標系統必須用
**服務帳號 / 免 MFA** 入口（本工具**不繞過 MFA**）。排程頁產生的 `schtasks` 指令**自動帶 `--unattended`**。

---

## 架構

```
rpa_studio/
├── main.py              PySide6 進入點
├── run_cli.py           無人值守 CLI（不依賴 Qt）
├── core/                引擎/UI 無關的契約層
│   ├── schema.py        Flow / Step 資料模型 + JSON I/O
│   ├── registry.py      @action 註冊表 + ActionContext / ActionResult
│   ├── runner.py        執行引擎：分派 / retry / timeout / 截圖 / log / on_error / 控制流
│   ├── variables.py     VarStore（{var} 替換 + 時間 placeholder）
│   ├── vault.py         憑證保管（keyring + 加密檔）
│   ├── store.py         SQLite（flows / runs / step_logs / heal_logs）
│   ├── heal.py          self-healing 評分與替換
│   ├── engine_api.py    get_session(engine) 工廠（解耦 UI 與引擎）
│   ├── scheduler.py     Windows schtasks 整合
│   └── headless.py      run_flow_headless()：不依賴 PySide6 的執行 wiring（CLI 共用）
├── engines/
│   ├── web/             Playwright 引擎 + web.* actions + codegen 錄製
│   ├── desktop/         pywinauto UIA 引擎 + desktop.* actions + 錄製器 + 多層定位
│   ├── vision/          OpenCV 影像比對 + RapidOCR
│   ├── flow/            flow.* 控制流動作
│   ├── data/            Excel/CSV 讀寫、拆檔、差異比對
│   ├── comms/           Email（Outlook/SMTP）+ SharePoint
│   └── triggers/        檔案觸發 watcher
├── ui/                  PySide6 視窗（流程清單/編輯/流程圖/執行/排程/憑證/日誌）+ overlay
└── flows/               範例流程 JSON
```

### UI 與引擎如何解耦
UI 層（`ui/`）**不直接 import** 任何 web/desktop 引擎；執行時由 `ui/run_worker.py` 透過
`core.engine_api.get_session()` 取得會話，內部才 **lazy import** 引擎。因此引擎未安裝 / import 失敗時，
UI 仍可正常啟動瀏覽各頁；按「執行」失敗會以友善訊息寫進日誌並回報 `status='failed'`，**不會 crash**。

---

## 驗證（offscreen，免 venv）

```powershell
set QT_QPA_PLATFORM=offscreen
set PYTHONIOENCODING=utf-8
python tests/test_ui_smoke.py
```

`tests/` 內含各層 smoke 測試（UI / 錄製 / 重播 / 視覺 / 資料 / 通訊 / 自癒 / 排程 / 無人值守）。

---

## 安全須知

- 帳號密碼一律以 **Vault secret 名稱**引用，實際值不進 flow JSON、不進指令列、不進日誌。
- 本機資料（`*.db`、`logs/`、`recordings/*_anchors/`、`venv/`、`.vault_key`）已被 `.gitignore` 排除，**請勿提交**。
- 本工具**不繞過 MFA**；無人值守需搭配服務帳號 / 免 MFA 入口。

## 授權

[MIT License](LICENSE) © 2026 Michael
