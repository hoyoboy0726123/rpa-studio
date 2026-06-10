# -*- coding: utf-8 -*-
"""桌面錄製器 — pynput 監聽鍵鼠,點擊時「一次抓多組定位器」。

核心契約(docs/phase2_spec.md §2):每個點擊步驟同時抓並寫入
  - primary    = {"strategy":"uia", "value": <uia spec JSON 字串>}
  - fallbacks  = [{"strategy":"image","value":"anchor_NNNN.png"},
                  {"strategy":"coord","value":"x,y"}]
  - fingerprint= {uia:{name,control_type,auto_id,class_name,window_title},
                  anchor:"anchor_NNNN.png", coord:"x,y", text:..., window_title:...}

回放時 resolver 依 UIA → win32 → image(CV) → coord 逐層 fallback,任一成功即用。

真實錄製流程(無法在 headless 測試環境驗證的部分):
  recorder = DesktopRecorder("my_flow", "recordings/my_flow_anchors")
  recorder.start()           # pynput 全域監聽鍵鼠;使用者實際操作目標程式
  ... 使用者點擊/打字 ...      # 每次點擊 → from_point 抓 UIA + 裁 anchor + 記座標
  flow = recorder.stop()     # F9 或外部呼叫;回傳 flow dict(engine='desktop')

pynput 的即時全域鍵鼠事件、pywinauto.from_point、實際螢幕截圖都需要互動桌面,
因此 **單元測試只覆蓋「從一次點擊事件產生 step + anchor」的純函式**
(capture_click_step / _crop_anchor),全域監聽本身於真實環境才能驗。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time

from core.schema import Flow, Step, new_id


# --------------------------------------------------------------------------- #
# 純函式:防鬼影 (anti-ghost) — 排除工具自己的視窗 / overlay 區域
# --------------------------------------------------------------------------- #
def _normalize_rect(rect) -> tuple[int, int, int, int] | None:
    """把 (left, top, right, bottom) 正規化成 left<=right、top<=bottom 的 tuple。

    容錯:None / 長度不足 / 無法轉 int → 回 None(視為無效排除框,忽略之)。
    """
    if not rect:
        return None
    try:
        l, t, r, b = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    except (TypeError, ValueError, IndexError):
        return None
    if r < l:
        l, r = r, l
    if b < t:
        t, b = b, t
    return (l, t, r, b)


def point_in_rect(x: int, y: int, rect) -> bool:
    """點 (x,y) 是否落在 rect=(left,top,right,bottom) 內(含邊界)。"""
    nr = _normalize_rect(rect)
    if nr is None:
        return False
    l, t, r, b = nr
    return l <= int(x) <= r and t <= int(y) <= b


def point_in_excluded_rects(x: int, y: int, excluded_rects) -> bool:
    """點 (x,y) 是否落在任一排除矩形內。

    excluded_rects 為 rect 的可疊代物(每個 rect = (left,top,right,bottom))。
    None / 空 → 一律回 False(無排除區 = 不擋任何點)。
    這是防鬼影的核心判斷:用於決定某次點擊事件是否該被略過。
    """
    if not excluded_rects:
        return False
    for rect in excluded_rects:
        if point_in_rect(x, y, rect):
            return True
    return False


def mask_excluded_in_crop(crop, crop_origin: tuple[int, int],
                          excluded_rects, fill=(0, 0, 0)):
    """把落在裁切框內的「排除區」塗黑,避免工具自己的 overlay 被截進 anchor。

    參數:
      crop         : 已裁好的 PIL.Image(anchor 區塊)。
      crop_origin  : 該裁切框在「螢幕座標系」的左上角 (ox, oy)。
      excluded_rects: 螢幕座標系的排除矩形清單。
      fill         : 覆蓋顏色(預設黑)。
    回傳同一張(就地修改後的)crop。無排除區 / 無交集 → 原樣回傳。

    抽成純函式以便單元測試:餵一張小圖 + 一個與之相交的排除框,
    斷言交集區域被填成 fill 色(即 anchor 不含排除區內容)。
    """
    if not excluded_rects:
        return crop
    try:
        from PIL import ImageDraw
    except Exception:  # noqa: BLE001
        return crop

    ox, oy = int(crop_origin[0]), int(crop_origin[1])
    cw, ch = crop.size
    draw = ImageDraw.Draw(crop)
    for rect in excluded_rects:
        nr = _normalize_rect(rect)
        if nr is None:
            continue
        l, t, r, b = nr
        # 轉成裁切框的本地座標,再夾到 [0, cw/ch]
        ll = max(0, l - ox)
        tt = max(0, t - oy)
        rr = min(cw, r - ox)
        bb = min(ch, b - oy)
        if rr > ll and bb > tt:
            draw.rectangle([ll, tt, rr - 1, bb - 1], fill=fill)
    return crop


# --------------------------------------------------------------------------- #
# 純函式:UIA 元素 → uia spec dict / fingerprint
# --------------------------------------------------------------------------- #
def uia_element_to_spec(elem: dict) -> dict:
    """把抓到的 UIA 元素資訊整理成 spec dict(只留非空欄位)。

    elem 期望含:name / control_type / auto_id / class_name / window_title。
    """
    spec: dict = {}
    for k in ("name", "control_type", "auto_id", "class_name", "window_title"):
        v = (elem or {}).get(k)
        if v:
            spec[k] = v
    return spec


def warmup_uia() -> None:
    """錄製開始前先暖機 UIA:第一次 from_point 會冷啟動 COM(~0.4s),
    暖機後同一執行緒的後續呼叫只要 ~30ms,才能在滑鼠 hook callback 內同步擷取
    而不超過 Windows 低階 hook 逾時(~300ms)。失敗忽略。"""
    try:
        from pywinauto.uia_element_info import UIAElementInfo
        UIAElementInfo.from_point(0, 0)
    except Exception:
        pass


def grab_uia_at(x: int, y: int) -> dict:
    """用輕量的 UIAElementInfo.from_point(x,y) 抓該座標元素資訊(暖機後 ~30ms)。

    回傳 dict(name/control_type/auto_id/class_name/window_title);
    失敗則回 {}(讓錄製仍可只靠 image/coord)。
    用 UIAElementInfo 而非 Desktop(backend='uia').from_point:後者每次較重,
    在 hook callback 內同步呼叫會拖到逾時 → 只錄到第一步;前者暖機後夠快。
    """
    try:
        from pywinauto.uia_element_info import UIAElementInfo
        info = UIAElementInfo.from_point(int(x), int(y))
        if info is None:
            return {}
        # 往上走到頂層視窗取標題(parent 鏈到 root 前一層)
        window_title = ""
        try:
            top = info
            while getattr(top, "parent", None) is not None and \
                    getattr(top.parent, "parent", None) is not None:
                top = top.parent
            window_title = getattr(top, "name", "") or ""
        except Exception:
            window_title = getattr(info, "name", "") or ""
        return {
            "name": getattr(info, "name", "") or "",
            "control_type": getattr(info, "control_type", "") or "",
            "auto_id": getattr(info, "automation_id", "") or "",
            "class_name": getattr(info, "class_name", "") or "",
            "window_title": window_title or "",
        }
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# 純函式:裁 anchor PNG
# --------------------------------------------------------------------------- #
def _crop_anchor(screenshot, x: int, y: int, out_path: str, size: int = 100,
                 excluded_rects=None, crop_origin=None):
    """從一張 PIL.Image(整螢幕截圖)裁出以 (x,y) 為中心、size×size 的 anchor。

    screenshot 為 PIL Image;座標為(已扣 monitor offset 的)截圖座標。
    寫出 out_path(PNG)。回傳實際寫出的檔案路徑。

    防鬼影:excluded_rects(螢幕座標系)+ crop_origin(此裁切框左上角的螢幕座標)
    一起給時,會把落在排除區的部分塗黑(避免工具自己的 overlay 被截進 anchor)。
    crop_origin 未給時則以截圖座標近似(單螢幕、offset=0 時等價)。
    抽成獨立函式以便單元測試(餵假截圖即可)。
    """
    half = size // 2
    w, h = screenshot.size
    left = max(0, x - half)
    top = max(0, y - half)
    right = min(w, x + half)
    bottom = min(h, y + half)
    crop = screenshot.crop((left, top, right, bottom))

    if excluded_rects:
        # 裁切框在螢幕座標系的左上角:優先用呼叫端給的 crop_origin,
        # 否則退而用「截圖座標的 left/top」近似(單螢幕無 offset 時相同)。
        origin = crop_origin if crop_origin is not None else (left, top)
        crop = mask_excluded_in_crop(crop, origin, excluded_rects)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    crop.save(out_path, "PNG")
    return out_path


def _grab_screen():
    """抓整螢幕截圖回傳 PIL.Image(優先 mss,退到 pillow ImageGrab)。"""
    try:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            mon = sct.monitors[0]  # 全部螢幕的聯集
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            # mss 的 monitors[0] 可能有左上 offset;校正成以該 offset 為原點
            img._rpa_offset = (mon["left"], mon["top"])  # type: ignore[attr-defined]
            return img
    except Exception:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        img._rpa_offset = (0, 0)  # type: ignore[attr-defined]
        return img


def _grab_region_anchor(x: int, y: int, out_path: str, size: int = 100,
                        excluded_rects=None) -> str:
    """只截「點擊點周圍 size×size 小區域」存成 anchor(快,適合在 hook callback 同步跑)。

    比全螢幕截圖快得多 → 點擊當下同步擷取也不會拖到 hook 逾時。
    """
    half = size // 2
    left = max(0, int(x) - half)
    top = max(0, int(y) - half)
    img = None
    try:
        import pyautogui
        img = pyautogui.screenshot(region=(left, top, size, size))
    except Exception:
        try:
            import mss
            from PIL import Image
            with mss.mss() as sct:
                raw = sct.grab({"left": left, "top": top, "width": size, "height": size})
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception:
            return out_path
    if excluded_rects:
        img = mask_excluded_in_crop(img, (left, top), excluded_rects)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# --------------------------------------------------------------------------- #
# 純函式:一次點擊 → 一個 click Step(+ 寫 anchor)
# --------------------------------------------------------------------------- #
def build_click_step(uia_elem: dict, x: int, y: int, anchor_name: str,
                     button: str = "left") -> Step:
    """把「一次點擊」的抓取結果組成一個 desktop.click Step。

    依 §2:primary(uia) + fallbacks(image, coord) + fingerprint。
    這是錄製器的核心轉換,刻意做成純函式以便單元測試。
    """
    spec = uia_element_to_spec(uia_elem)
    coord = f"{int(x)},{int(y)}"

    primary = {"strategy": "uia", "value": json.dumps(spec, ensure_ascii=False)}
    fallbacks = [
        {"strategy": "image", "value": anchor_name},
        {"strategy": "coord", "value": coord},
    ]
    fingerprint = {
        "uia": {
            "name": spec.get("name", ""),
            "control_type": spec.get("control_type", ""),
            "auto_id": spec.get("auto_id", ""),
            "class_name": spec.get("class_name", ""),
            "window_title": spec.get("window_title", ""),
        },
        "anchor": anchor_name,
        "coord": coord,
        "text": spec.get("name", ""),
        "window_title": spec.get("window_title", ""),
    }
    target = {"primary": primary, "fallbacks": fallbacks, "fingerprint": fingerprint}

    name_hint = spec.get("name") or spec.get("control_type") or coord
    params = {} if button == "left" else {"button": button}
    return Step(id=new_id(), action="desktop.click",
                label=f"click {name_hint}", target=target, params=params)


def capture_click_step(x: int, y: int, anchor_dir: str, index: int,
                       button: str = "left",
                       uia_grabber=grab_uia_at,
                       screen_grabber=None,
                       excluded_rects=None) -> Step:
    """完整「抓一次點擊」:抓 UIA + 裁 anchor PNG + 組 Step。

    參數 uia_grabber / screen_grabber 可注入,測試時傳假的(回傳 mock 元素 / 假截圖),
    即可在 headless 驗整條「一次抓多組定位器」邏輯而不需真桌面。

    excluded_rects(防鬼影):螢幕座標系的排除矩形清單(工具自己的視窗 / overlay)。
    裁 anchor 時會把落在這些區域的部分塗黑,避免把工具自己的浮層截進 anchor。
    """
    anchor_name = f"anchor_{index:04d}.png"
    anchor_path = os.path.join(anchor_dir, anchor_name)

    uia_elem = {}
    try:
        uia_elem = uia_grabber(x, y) or {}
    except Exception:
        uia_elem = {}

    # 截 anchor:預設只截小區域(快,適合同步在 hook 內跑);
    # 若呼叫端注入 screen_grabber(測試)則走「全螢幕 + 裁切」相容路徑。
    try:
        if screen_grabber is None:
            _grab_region_anchor(int(x), int(y), anchor_path,
                                excluded_rects=excluded_rects)
        else:
            shot = screen_grabber()
            off = getattr(shot, "_rpa_offset", (0, 0))
            half = 100 // 2
            crop_origin = (int(x) - half, int(y) - half)
            _crop_anchor(shot, int(x) - off[0], int(y) - off[1], anchor_path,
                         excluded_rects=excluded_rects, crop_origin=crop_origin)
    except Exception:
        # anchor 截不到也不擋錄製(image fallback 會找不到檔,但 uia/coord 仍可用)
        pass

    return build_click_step(uia_elem, x, y, anchor_name, button=button)


# --------------------------------------------------------------------------- #
# DesktopRecorder:pynput 全域監聽
# --------------------------------------------------------------------------- #
class DesktopRecorder:
    """桌面錄製器。

    用法:
      rec = DesktopRecorder("my_flow", "recordings/my_flow_anchors")
      rec.start()          # 開始全域監聽(非阻塞;另起 listener thread)
      ... 使用者操作 ...
      flow = rec.stop()    # 回傳 flow dict(engine='desktop')

    F9 為統一停止鍵:錄製中按 F9 會自動觸發 stop()(設定 stop_event)。
    連續打字會合併成單一 desktop.type step(遇到點擊/特殊鍵/停止才 flush)。
    """

    def __init__(self, flow_name: str, anchor_dir: str, stop_event=None,
                 excluded_rects=None, self_hwnd=None):
        self.flow_name = flow_name
        self.anchor_dir = anchor_dir
        self.stop_event = stop_event or threading.Event()
        self.steps: list[Step] = []
        self._anchor_index = 0
        self._typing_buffer: list[str] = []
        self._mouse_listener = None
        self._kbd_listener = None
        self._running = False
        # 事件佇列 + 消費者執行緒:hook callback 只入列(極輕),重活(UIA/截圖)在 consumer 做。
        # 這是修正「只錄到第一個動作 / F9 停不了」的關鍵:Windows 低階 hook callback
        # 若 >300ms(LowLevelHooksTimeout)會被系統移除,UIA from_point + 全螢幕截圖必超時。
        self._queue: "queue.Queue" = queue.Queue()
        self._consumer = None
        # 防鬼影:落在這些螢幕矩形內的點擊會被略過,裁 anchor 時也會塗黑該區。
        # (工具自己的視窗 / overlay 區;list of (left, top, right, bottom))
        self.excluded_rects: list = list(excluded_rects or [])
        # 自己的視窗 handle:start 時最小化、stop 時還原(避免錄到工具自己)。
        self.self_hwnd = self_hwnd
        self._minimizer = None
        os.makedirs(self.anchor_dir, exist_ok=True)
        # 除錯日誌:錄製過程的關鍵事件寫檔,真實環境失敗時可據此判讀(非當機也留證)。
        self._dlog_path = os.path.join("logs", "recorder_debug.log")

    def _dlog(self, msg: str):
        try:
            os.makedirs("logs", exist_ok=True)
            with open(self._dlog_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} [{self.flow_name}] {msg}\n")
        except Exception:
            pass

    # ----------------------------------------------------------- typing buffer
    def _flush_typing(self):
        """把累積的連續打字 flush 成一個 desktop.type step。"""
        if not self._typing_buffer:
            return
        text = "".join(self._typing_buffer)
        self._typing_buffer = []
        if text == "":
            return
        self.steps.append(Step(id=new_id(), action="desktop.type",
                               label=f"type {text[:20]}",
                               params={"text": text}))

    # --------------------------------------------------------- events(滑鼠按下「當下同步」擷取)
    # ⚠️ 必須在點擊當下抓 UIA 元素 —— 延遲到背景才抓會抓到「UI 變化後」的錯元素
    #    (例:點開始功能表項目後,選單關閉,座標下露出後面視窗 → 抓錯)。
    #    用輕量 UIAElementInfo.from_point(暖機後 ~30ms)+ 小區域截圖,夠快不超 hook 逾時。
    def _on_click(self, x, y, button, pressed):
        if not pressed:
            return
        if self.stop_event.is_set():
            return False  # 停止 listener
        # 防鬼影:點到工具自己的視窗 / overlay 區 → 直接略過。
        if point_in_excluded_rects(int(x), int(y), self.excluded_rects):
            self._dlog(f"SKIP click {int(x)},{int(y)} (in excluded {self.excluded_rects})")
            return
        # 同步擷取(此刻 UI 還是按下當下的狀態)
        self._flush_typing()
        self._anchor_index += 1
        try:
            step = capture_click_step(int(x), int(y), self.anchor_dir, self._anchor_index,
                                      button=getattr(button, "name", "left"),
                                      excluded_rects=self.excluded_rects)
            self.steps.append(step)
            _nm = (((step.target or {}).get("fingerprint") or {}).get("uia") or {}).get("name", "")
            self._dlog(f"step desktop.click @{x},{y} name={_nm!r} (total={len(self.steps)})")
        except Exception as e:  # noqa: BLE001
            self._dlog(f"capture click FAILED @{x},{y}: {type(e).__name__}: {e}")

    def _on_press(self, key):
        if self.stop_event.is_set():
            return False
        try:
            from pynput import keyboard as _kb
            if key == _kb.Key.f9:           # F9 統一停止
                self.stop_event.set()
                self._dlog("F9 pressed -> stop_event set")
                return False
            if hasattr(key, "char") and key.char is not None:
                self._typing_buffer.append(key.char)
                return
            self._flush_typing()
            special = self._special_key_token(key, _kb)
            if special:
                self.steps.append(Step(id=new_id(), action="desktop.send_keys",
                                       label=f"key {special}", params={"keys": special}))
        except Exception:
            pass

    # ----------------------------------------------- 消費者:在獨立執行緒做重活
    def _process_event(self, ev):
        """處理單一事件(click 抓 UIA+anchor / char 累積 / special 記 send_keys)。
        由 consumer 執行緒呼叫(單執行緒,故 steps/typing_buffer 無需鎖)。"""
        kind = ev[0]
        if kind == "click":
            _, x, y, btn = ev
            self._flush_typing()
            self._anchor_index += 1
            try:
                step = capture_click_step(x, y, self.anchor_dir, self._anchor_index,
                                          button=btn, excluded_rects=self.excluded_rects)
                self.steps.append(step)
                self._dlog(f"step desktop.click @{x},{y} (total={len(self.steps)})")
            except Exception as e:
                self._dlog(f"capture click FAILED @{x},{y}: {type(e).__name__}: {e}")
        elif kind == "char":
            self._typing_buffer.append(ev[1])
        elif kind == "special":
            self._flush_typing()
            self.steps.append(Step(id=new_id(), action="desktop.send_keys",
                                   label=f"key {ev[1]}", params={"keys": ev[1]}))
        # kind == "stop":不處理,讓 _consume 迴圈自然結束

    def _consume(self):
        """消費者迴圈:從佇列取事件做重活,直到 stop_event 設定且佇列清空。"""
        while True:
            if self.stop_event.is_set() and self._queue.empty():
                break
            try:
                ev = self._queue.get(timeout=0.15)
            except queue.Empty:
                continue
            try:
                self._process_event(ev)
            except Exception:
                pass
        self._flush_typing()

    def _drain_pending(self):
        """同步處理目前佇列內所有事件(供測試 / stop 收尾用,不需執行緒)。"""
        while True:
            try:
                ev = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._process_event(ev)
            except Exception:
                pass

    @staticmethod
    def _special_key_token(key, _kb) -> str | None:
        """把 pynput 特殊鍵轉成 pywinauto.send_keys 語法 token。"""
        mapping = {
            _kb.Key.enter: "{ENTER}",
            _kb.Key.tab: "{TAB}",
            _kb.Key.backspace: "{BACKSPACE}",
            _kb.Key.esc: "{ESC}",
            _kb.Key.delete: "{DELETE}",
            _kb.Key.space: " ",
        }
        return mapping.get(key)

    # ------------------------------------------------------------------ control
    def _minimize_self(self):
        """錄製開始前最小化自己的視窗(graceful;失敗只記 log)。

        最小化前先把自己的視窗矩形加進 excluded_rects(防鬼影:即使最小化前
        的瞬間或還原後使用者誤點到,仍會被排除)。
        """
        if self.self_hwnd is None:
            return
        try:
            from core import window as _win
            # 不把主視窗矩形加入 excluded_rects:既然要最小化,視窗就不在畫面上,
            # 排除它只會誤殺落在原區域的目標 App 點擊(本 bug 根因)。只最小化即可。
            self._minimizer = _win.WindowMinimizer(self.self_hwnd)
            self._minimizer.__enter__()
        except Exception:
            # 最小化失敗不擋錄製
            self._minimizer = None

    def _restore_self(self):
        """錄製結束後還原自己的視窗(graceful)。"""
        mz = self._minimizer
        self._minimizer = None
        if mz is None:
            return
        try:
            mz.__exit__(None, None, None)
        except Exception:
            pass

    def start(self):
        """開始全域監聽(非阻塞)。需要互動桌面 + pynput。

        若建構時給了 self_hwnd,會先最小化自己的視窗(避免錄到工具自己)。
        """
        if self._running:
            return
        self._minimize_self()
        warmup_uia()   # 暖機 UIA,讓點擊當下的同步 from_point 夠快(避免第一步冷啟動拖慢)
        from pynput import mouse, keyboard
        self._running = True
        self.stop_event.clear()
        self._mouse_listener = mouse.Listener(on_click=self._on_click)
        self._kbd_listener = keyboard.Listener(on_press=self._on_press)
        self._mouse_listener.start()
        self._kbd_listener.start()
        self._consumer = None   # 已改為「按下當下同步擷取」,不再用背景 consumer
        self._dlog(f"START listeners (同步擷取) excluded_rects={self.excluded_rects} "
                   f"self_hwnd={self.self_hwnd}")

    def stop(self) -> dict:
        """停止監聽,flush 打字,回傳 flow dict(engine='desktop')。"""
        self._dlog(f"STOP requested (steps so far={len(self.steps)})")
        self.stop_event.set()
        self._running = False
        for lst in (self._mouse_listener, self._kbd_listener):
            try:
                if lst is not None:
                    lst.stop()
            except Exception:
                pass
        self._mouse_listener = self._kbd_listener = None
        # 等消費者把佇列剩餘事件(含最後一次點擊)處理完;沒有 consumer(如測試)則同步收尾。
        if self._consumer is not None and self._consumer.is_alive():
            try:
                self._consumer.join(timeout=5.0)
            except Exception:
                pass
        else:
            self._drain_pending()
        self._consumer = None
        self._flush_typing()
        # 還原自己的視窗(若 start 時有最小化)。
        self._restore_self()

        flow = Flow(name=self.flow_name, engine="desktop")
        flow.steps = list(self.steps)
        return flow.to_dict()

    def wait(self, poll: float = 0.2):
        """阻塞直到 stop_event 被設(例如使用者按 F9)。供 CLI 同步等待用。"""
        while not self.stop_event.is_set():
            time.sleep(poll)
        return self.stop()
