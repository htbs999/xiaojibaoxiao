"""
OCR module - optimized for large bold numbers (e.g., WeChat/Alipay payment screenshots)
策略：裁剪、放大、锐化、自适应二值化 + 正则提取金额
"""

import os
import re
import cv2
import numpy as np
import pytesseract

pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

# 调试日志（保留原项目的风格）
if getattr(__import__('sys'), 'frozen', False):
    _LOG_PATH = os.path.join(os.path.dirname(__import__('sys').executable), "ocr_debug.log")
else:
    import tempfile
    _LOG_PATH = os.path.join(tempfile.gettempdir(), "wechat_expense_ocr_debug.log")

def _log(msg):
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _preprocess_for_amount(image_path):
    """
    专门针对微信/支付宝账单中大号加粗金额的预处理：
    1. 裁剪上半部分中间区域（假设金额在顶部中央）
    2. 放大3倍（分离粘连字符）
    3. 锐化（增强边缘）
    4. 自适应二值化（增强对比度）
    """
    img = cv2.imread(image_path)
    if img is None:
        return None

    h, w, _ = img.shape

    # 裁剪：取高度 15%~40%，宽度 25%~75%（金额通常位于此区域）
    y_start = int(h * 0.15)
    y_end   = int(h * 0.40)
    x_start = int(w * 0.25)
    x_end   = int(w * 0.75)
    img_crop = img[y_start:y_end, x_start:x_end]

    # 放大3倍
    scale = 3.0
    new_w = int(img_crop.shape[1] * scale)
    new_h = int(img_crop.shape[0] * scale)
    img_scaled = cv2.resize(img_crop, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

    # 锐化
    kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
    img_sharp = cv2.filter2D(img_scaled, -1, kernel)

    # 转灰度 + 自适应二值化（黑底白字）
    gray = cv2.cvtColor(img_sharp, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        blockSize=11,
        C=2
    )
    return binary


def _extract_amount(text):
    """
    从 OCR 结果中提取最可能的金额数字。
    优先匹配带小数点或千分位逗号的数字，排除纯整数（如单号、时间）。
    """
    if not text:
        return None

    # 清洗：去除空格和换行
    clean = text.replace(" ", "").replace("\n", "")

    # 模式：匹配 -1,234.56 或 1234.56 或 -100.00 等格式
    # 注意：金额必须包含小数点（至少一位小数）
    pattern = r'-?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?'

    matches = re.findall(pattern, clean)
    candidates = []
    for m in matches:
        # 移除逗号
        num_str = m.replace(',', '')
        try:
            val = float(num_str)
            # 过滤不合理值：0.01 ~ 99999.99
            if 0.01 <= abs(val) <= 99999.99:
                candidates.append(abs(val))
        except ValueError:
            continue

    if not candidates:
        return None

    # 返回最大值（通常金额是最大的数字）
    return round(max(candidates), 2)


def recognize_amount_from_image(image_path, progress_callback=None):
    """
    主识别函数（与 app.py 中原有接口完全兼容）
    """
    if not os.path.exists(image_path):
        return {"success": False, "error": "File not found", "amount": None, "raw_text": "", "engine": "tesseract"}

    try:
        if progress_callback:
            progress_callback(10, "预处理图片...")

        processed = _preprocess_for_amount(image_path)
        if processed is None:
            return {"success": False, "error": "无法读取图片", "amount": None, "raw_text": "", "engine": "tesseract"}

        if progress_callback:
            progress_callback(50, "OCR 识别中...")

        # 配置：单行模式 + 严格数字白名单（只识别数字、小数点、负号、逗号）
        config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789.-,'
        raw_text = pytesseract.image_to_string(processed, config=config).strip()

        if progress_callback:
            progress_callback(80, f"原始识别结果: {raw_text}")

        amount = _extract_amount(raw_text)

        if progress_callback:
            progress_callback(100, "完成")

        _log(f"[OCR] amount={amount} raw={raw_text[:300]}")

        return {
            "success": True,
            "amount": amount,
            "raw_text": raw_text,
            "engine": "tesseract"
        }

    except Exception as e:
        _log(f"[OCR] error: {e}")
        return {"success": False, "error": str(e), "amount": None, "raw_text": "", "engine": "tesseract"}


def is_ocr_available():
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False

def init_ocr_engine():
    return is_ocr_available()