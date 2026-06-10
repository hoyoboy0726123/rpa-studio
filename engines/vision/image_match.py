# -*- coding: utf-8 -*-
"""CV 影像比對(image_match)— spec §4。

用 mss(或 pillow ImageGrab 退路)截螢幕 → OpenCV matchTemplate
(TM_CCOEFF_NORMED)找 anchor 小圖,回傳「中心螢幕座標」或 None。

API:
  locate(anchor_path, confidence=0.85, region=None) -> (x, y) | None
  wait_locate(anchor_path, timeout=10, confidence=0.85, region=None,
              stop_event=None) -> (x, y) | None

另提供核心比對函式(可測、不截螢幕):
  locate_in_image(haystack, anchor, confidence=0.85, multi_scale=False)
      -> (cx, cy, score) | None
      haystack / anchor 可為檔案路徑、PIL.Image、或 numpy 陣列(BGR/灰階)。

region = (x, y, w, h):只在該螢幕區域內找(找到後座標會加回 region 偏移)。
multi_scale:對 anchor 做多尺度縮放(處理 DPI / 縮放差異),較慢,預設關。

所有失敗(截不到、讀不到圖、cv2 缺)一律 graceful:回 None + log。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("vision.image_match")

# 多尺度預設縮放因子(由近到遠)
_DEFAULT_SCALES = (1.0, 0.9, 1.1, 0.8, 1.25, 0.75, 1.5)


# ----------------------------------------------------------------- lazy imports
def _cv2():
    import cv2  # type: ignore
    return cv2


def _np():
    import numpy as np  # type: ignore
    return np


# ----------------------------------------------------------------- 影像載入工具
def _to_bgr(src):
    """把 path / PIL.Image / numpy 陣列統一轉成 OpenCV BGR uint8 陣列。

    失敗回 None(graceful)。
    """
    np = _np()
    cv2 = _cv2()

    # numpy 陣列
    if isinstance(src, np.ndarray):
        arr = src
        if arr.ndim == 2:  # 灰階 -> BGR
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        if arr.ndim == 3 and arr.shape[2] == 4:  # RGBA/BGRA -> BGR
            return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        return arr

    # 路徑字串
    if isinstance(src, str):
        try:
            # 用 numpy + imdecode 讀,支援中文/含空格路徑(imread 對非 ASCII 路徑會失敗)
            with open(src, "rb") as f:
                buf = np.frombuffer(f.read(), dtype=np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if img is None:
                log.warning("imdecode 失敗(非影像或損毀): %s", src)
            return img
        except Exception as e:  # noqa: BLE001
            log.warning("讀取影像失敗 %s: %s", src, e)
            return None

    # PIL.Image
    try:
        from PIL import Image  # type: ignore
        if isinstance(src, Image.Image):
            rgb = src.convert("RGB")
            arr = np.array(rgb)  # RGB
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    except Exception as e:  # noqa: BLE001
        log.warning("PIL 影像轉換失敗: %s", e)
        return None

    log.warning("不支援的影像來源型別: %r", type(src))
    return None


# ------------------------------------------------------------------- 截螢幕
def _grab_screen(region=None):
    """截全螢幕或指定 region=(x,y,w,h),回傳 (bgr_array, offset_xy)。

    優先用 mss;失敗退到 PIL.ImageGrab。失敗回 (None, (0,0))。
    offset_xy 為截圖左上角在螢幕的座標(region 才非 0)。
    """
    np = _np()
    cv2 = _cv2()
    off = (int(region[0]), int(region[1])) if region else (0, 0)

    # 1) mss
    try:
        import mss  # type: ignore
        with mss.mss() as sct:
            if region:
                x, y, w, h = (int(v) for v in region)
                mon = {"left": x, "top": y, "width": w, "height": h}
            else:
                mon = sct.monitors[0]  # 所有螢幕的虛擬框
                off = (mon["left"], mon["top"])
            raw = sct.grab(mon)
            arr = np.array(raw)  # BGRA
            bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
            return bgr, off
    except Exception as e:  # noqa: BLE001
        log.debug("mss 截圖失敗,改試 ImageGrab: %s", e)

    # 2) PIL ImageGrab 退路
    try:
        from PIL import ImageGrab  # type: ignore
        if region:
            x, y, w, h = (int(v) for v in region)
            bbox = (x, y, x + w, y + h)
            img = ImageGrab.grab(bbox=bbox)
        else:
            img = ImageGrab.grab(all_screens=True)
        bgr = _to_bgr(img)
        return bgr, off
    except Exception as e:  # noqa: BLE001
        log.warning("截螢幕失敗(mss 與 ImageGrab 皆不可用): %s", e)
        return None, off


# ----------------------------------------------------------- 核心模板比對(可測)
def locate_in_image(haystack, anchor, confidence: float = 0.85,
                    multi_scale: bool = False):
    """在 haystack 內找 anchor,回傳 (cx, cy, score) 或 None。

    cx, cy 為命中區塊在 haystack 內的「中心座標」(像素)。
    用 cv2.matchTemplate + TM_CCOEFF_NORMED;multi_scale 時對 anchor 縮放後取最佳。
    任何失敗一律 graceful 回 None。
    """
    try:
        cv2 = _cv2()
    except Exception as e:  # noqa: BLE001
        log.warning("OpenCV 不可用,影像比對降級為 None: %s", e)
        return None

    big = _to_bgr(haystack)
    small = _to_bgr(anchor)
    if big is None or small is None:
        return None

    bh, bw = big.shape[:2]
    scales = _DEFAULT_SCALES if multi_scale else (1.0,)

    best = None  # (score, cx, cy)
    for sc in scales:
        if sc == 1.0:
            tmpl = small
        else:
            nw = max(1, int(round(small.shape[1] * sc)))
            nh = max(1, int(round(small.shape[0] * sc)))
            interp = cv2.INTER_AREA if sc < 1.0 else cv2.INTER_CUBIC
            tmpl = cv2.resize(small, (nw, nh), interpolation=interp)

        th, tw = tmpl.shape[:2]
        # anchor 比 haystack 還大 -> 此尺度跳過
        if th > bh or tw > bw:
            continue
        try:
            res = cv2.matchTemplate(big, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
        except Exception as e:  # noqa: BLE001
            log.warning("matchTemplate 失敗(scale=%s): %s", sc, e)
            continue
        if best is None or max_val > best[0]:
            cx = int(max_loc[0] + tw / 2)
            cy = int(max_loc[1] + th / 2)
            best = (float(max_val), cx, cy)

    if best is None:
        return None
    score, cx, cy = best
    if score < confidence:
        log.debug("最佳分數 %.3f < confidence %.3f,視為未命中", score, confidence)
        return None
    return (cx, cy, score)


# ------------------------------------------------------------------- 公開 API
def locate(anchor_path: str, confidence: float = 0.85, region=None,
           multi_scale: bool = False):
    """截螢幕找 anchor,回傳中心「螢幕座標」(x, y) 或 None。

    region=(x,y,w,h):只在該螢幕區域比對(命中座標會加回 region 偏移)。
    """
    screen, off = _grab_screen(region)
    if screen is None:
        return None
    hit = locate_in_image(screen, anchor_path, confidence=confidence,
                          multi_scale=multi_scale)
    if hit is None:
        return None
    cx, cy, score = hit
    sx, sy = cx + off[0], cy + off[1]
    log.debug("locate 命中 anchor=%s @ (%d,%d) score=%.3f", anchor_path, sx, sy, score)
    return (int(sx), int(sy))


def locate_score(anchor_path: str, confidence: float = 0.85, region=None,
                 multi_scale: bool = False):
    """同 locate(),但一律回傳實際比對分數供日誌記錄。

    回傳:
      (x, y, score)        命中且 score >= confidence(座標為螢幕座標)
      (None, None, score)  有比對到最佳分數但未達門檻(score 為實際最佳分數)
      (None, None, None)   無法比對(截圖/讀圖/cv2 失敗)
    """
    screen, off = _grab_screen(region)
    if screen is None:
        return (None, None, None)
    # 用 confidence=0 取得「最佳分數」,再由本函式套門檻,讓未命中時也能回報分數。
    hit = locate_in_image(screen, anchor_path, confidence=0.0,
                          multi_scale=multi_scale)
    if hit is None:
        return (None, None, None)
    cx, cy, score = hit
    if score < confidence:
        return (None, None, float(score))
    return (int(cx + off[0]), int(cy + off[1]), float(score))


def wait_locate(anchor_path: str, timeout: float = 10, confidence: float = 0.85,
                region=None, stop_event=None, poll_interval: float = 0.4,
                multi_scale: bool = False):
    """輪詢等到 anchor 出現或逾時;可被 stop_event 中斷。

    回傳中心螢幕座標 (x, y) 或 None(逾時 / 被中斷 / 視覺層不可用)。
    """
    deadline = time.time() + float(timeout)
    while True:
        if stop_event is not None and stop_event.is_set():
            log.debug("wait_locate 被 stop_event 中斷")
            return None
        pt = locate(anchor_path, confidence=confidence, region=region,
                    multi_scale=multi_scale)
        if pt is not None:
            return pt
        if time.time() >= deadline:
            return None
        # 分段 sleep,讓 stop_event 能即時中斷
        remain = deadline - time.time()
        time.sleep(min(poll_interval, max(0.0, remain)))
