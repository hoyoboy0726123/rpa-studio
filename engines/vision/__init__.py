# -*- coding: utf-8 -*-
"""Vision 引擎:CV 影像比對 (image_match) + OCR (ocr)。

桌面引擎的 fallback 定位層之一(UIA → win32 → image(CV)→ coord)。
本套件「純視覺」、與 pywinauto 無關:截螢幕 → OpenCV 模板比對 → 螢幕座標;
或截區域 → RapidOCR 辨識文字。

設計原則:
  - 重相依(cv2 / mss / rapidocr)一律 lazy import,import 本套件本身不該爆。
  - 載入 / 辨識失敗一律 graceful(回 None 或空字串 + log),不可讓整個工具崩。
"""
from .image_match import locate, wait_locate, locate_in_image  # noqa: F401
from .ocr import read_region, read_image  # noqa: F401
