# -*- coding: utf-8 -*-
"""OCR(ocr)— spec §4。

用 rapidocr-onnxruntime(中英混排),lazy 單例載入模型。
  read_region(x, y, w, h) -> str  : 截該螢幕區域做 OCR
  read_image(path) -> str          : 對影像檔做 OCR

載入 / 辨識失敗一律 graceful:回空字串 + 記 log,不可崩。
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("vision.ocr")

# rapidocr 單例 + 鎖(模型載入較重,只載一次)
_ENGINE = None
_ENGINE_FAILED = False          # 載入過且失敗 -> 不再重試,直接降級
_LOCK = threading.Lock()


def _get_engine():
    """lazy 取得 RapidOCR 單例;載入失敗回 None(並記住,避免反覆重試)。"""
    global _ENGINE, _ENGINE_FAILED
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_FAILED:
        return None
    with _LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _ENGINE_FAILED:
            return None
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            _ENGINE = RapidOCR()
            log.info("RapidOCR 載入成功")
            return _ENGINE
        except Exception as e:  # noqa: BLE001
            _ENGINE_FAILED = True
            log.warning("RapidOCR 載入失敗,OCR 降級為空字串: %s", e)
            return None


def _run_ocr(img_bgr_or_path) -> str:
    """對影像(BGR numpy 陣列或路徑)跑 OCR,回傳串接後文字。失敗回空字串。"""
    engine = _get_engine()
    if engine is None:
        return ""
    try:
        result, _elapse = engine(img_bgr_or_path)
    except Exception as e:  # noqa: BLE001
        log.warning("OCR 辨識失敗: %s", e)
        return ""
    if not result:
        return ""
    # rapidocr 回傳 [[box, text, score], ...]
    lines = []
    for item in result:
        try:
            lines.append(str(item[1]))
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(lines)


def read_image(path: str) -> str:
    """對影像檔做 OCR,回傳辨識文字(中英混排)。失敗回空字串。"""
    try:
        import numpy as np  # type: ignore
        import cv2  # type: ignore
        # 支援中文/含空格路徑:open + imdecode(imread 對非 ASCII 路徑會失敗)
        with open(path, "rb") as f:
            buf = np.frombuffer(f.read(), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            log.warning("read_image 讀不到影像: %s", path)
            return ""
        return _run_ocr(img)
    except Exception as e:  # noqa: BLE001
        # 退路:直接把路徑丟給引擎(rapidocr 也能吃路徑)
        log.debug("read_image 自行讀檔失敗,改交由引擎讀路徑: %s", e)
        return _run_ocr(path)


def read_region(x: int, y: int, w: int, h: int) -> str:
    """截該螢幕區域 (x, y, w, h) 做 OCR,回傳文字。失敗回空字串。"""
    try:
        from .image_match import _grab_screen
        screen, _off = _grab_screen((x, y, w, h))
        if screen is None:
            log.warning("read_region 截螢幕失敗")
            return ""
        return _run_ocr(screen)
    except Exception as e:  # noqa: BLE001
        log.warning("read_region 失敗: %s", e)
        return ""
