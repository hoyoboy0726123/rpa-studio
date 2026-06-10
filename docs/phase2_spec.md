# Phase 2 整合契約 — 錄製器 + 多層定位(含 CV 影像比對 + OCR)

三方(CV/OCR 層、錄製器、UI)都要遵守以下格式,確保「錄製時抓多組定位器 → 回放逐層 fallback」能對接。

## 1. Target 多定位器格式(寫進 Step.target)

```jsonc
{
  "primary":   {"strategy": "...", "value": "..."},
  "fallbacks": [ {"strategy": "...", "value": "..."}, ... ],
  "fingerprint": {                      // 給日後 self-healing / debug 用
    "uia": {"name": "...", "control_type": "...", "auto_id": "...",
            "class_name": "...", "window_title": "..."},
    "anchor": "anchor_0001.png",        // 元素周圍小截圖檔名(image 策略用)
    "coord": "x,y",
    "text": "..."
  }
}
```

- **strategy 值域**
  - web:`role | text | testid | css | xpath | coord`
  - desktop:`uia | win32 | image | coord`
- **image 策略**:`value` = anchor PNG 檔名(存在該 flow 的 anchor 目錄)。
- **coord 策略**:`value` = `"x,y"`(螢幕座標,最後手段)。

## 2. 桌面錄製要抓的「多組定位器」(關鍵:不要只存一種)

桌面錄製每個點擊動作,**同時**抓並寫入:
- `primary = {"strategy":"uia", "value": <uia spec 的 JSON 字串>}`
- `fallbacks = [{"strategy":"image","value":"anchor_NNNN.png"}, {"strategy":"coord","value":"x,y"}]`
- `fingerprint = {uia:{...}, anchor:"anchor_NNNN.png", coord:"x,y", window_title:"..."}`

回放定位 fallback 順序(desktop):**UIA → win32 → image(CV)→ coord**,任一層成功即用。

## 3. Anchor 儲存慣例

- 目錄:`recordings/<flow_name>_anchors/anchor_NNNN.png`
- 內容:點擊點周圍約 100x100 的螢幕截圖(pillow / mss 截圖後裁切)。
- Flow JSON 只存檔名;resolver 從「該 flow 的 anchor 目錄」找檔。

## 4. Vision 層 API(engines/vision/)

```python
# engines/vision/image_match.py
def locate(anchor_path: str, confidence: float = 0.85, region=None) -> tuple[int,int] | None:
    """在(整個螢幕或 region)用 OpenCV 模板比對找 anchor,回傳中心螢幕座標或 None。region=(x,y,w,h)。"""

def wait_locate(anchor_path: str, timeout: float = 10, confidence: float = 0.85,
                region=None, stop_event=None) -> tuple[int,int] | None:
    """輪詢等到出現或逾時;可被 stop_event 中斷。"""

# engines/vision/ocr.py
def read_region(x: int, y: int, w: int, h: int) -> str:
    """截該螢幕區域做 OCR(rapidocr-onnxruntime,中英混排),回傳文字。lazy 載入模型。"""

def read_image(path: str) -> str: ...
```

OCR 後端 lazy 載入;載入失敗要 graceful(回空字串 + 記 log),不可讓整個工具崩。

## 5. 新增 desktop 動作(engines/desktop/actions.py,@action)

- `desktop.wait_image`  params: `{anchor, timeout, confidence}` → 等影像出現
- `desktop.image_click`  params: `{anchor, confidence, button}` → CV 定位後點擊
- `desktop.ocr_read`  params: `{x, y, w, h, var}` → OCR 區域寫入變數

## 6. 錄製器 API

```python
# engines/web/recorder.py
def record_web(url: str, out_flow_path: str) -> str:
    """啟動 Playwright codegen 錄 web 操作,把產生的動作轉成我們的 flow JSON,存檔回傳路徑。
    建議:shell 呼叫 `playwright codegen --target python -o <tmp> <url>`,再把輸出解析成 web.* steps。"""

# engines/desktop/recorder.py
class DesktopRecorder:
    def __init__(self, flow_name: str, anchor_dir: str, stop_event=None): ...
    def start(self): ...   # pynput 監聽鍵鼠;點擊時抓 UIA(pywinauto from_point)+ 裁 anchor + 座標
    def stop(self) -> dict: ...  # 回傳 flow dict(engine="desktop", steps 帶多定位器 target),並存 anchor PNG
```

連續打字要合併成 `desktop.type`;F9 統一停止。

## 7. UI 整合(ui/pages/record_page.py)

- 選 web / desktop 錄製;web 要 URL;按「開始錄製」→ overlay 顯示 RECORDING 紅燈 → 操作 → F9/停止 → 預覽抓到的 steps → 存成 flow(進 Store + 寫 flows/ 或 recordings/)。
- 用既有 `ui/overlay.py` 的 StatusOverlay(RECORDING)與 ElementPicker。
