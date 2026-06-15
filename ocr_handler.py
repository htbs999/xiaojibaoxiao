"""
OCR module - optimized for large bold numbers (e.g., WeChat/Alipay payment screenshots)
策略：放大、锐化、裁剪上半部分、自适应二值化 + 单行模式识别
"""

import os
import re
import sys as _sys
import cv2
import numpy as np
import pytesseract

pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

if getattr(_sys, 'frozen', False):
    _LOG_PATH = os.path.join(os.path.dirname(_sys.executable), "ocr_debug.log")
else:
    import tempfile
    _LOG_PATH = os.path.join(tempfile.gettempdir(), "wechat_expense_ocr_debug.log")

def _log(msg):
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def preprocess_for_amount(image_path):
    """
    专门针对微信/支付宝账单中大号加粗金额的预处理：
    1. 裁剪上半部分（假设金额在顶部）
    2. 放大2倍（分离粘连字符）
    3. 锐化（增强边缘）
    4. 自适应二值化（增强对比度）
    """
    img = cv2.imread(image_path)
    if img is None:
        return None

    h, w, _ = img.shape

    # 裁剪上半部分：取高度 1/3 的区域（金额通常在上方）
    img_cropped = img[0:int(h/3), :]

    # 放大2倍
    scale = 2.0
    new_w = int(w * scale)
    new_h = int(img_cropped.shape[0] * scale)
    img_scaled = cv2.resize(img_cropped, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

    # 转灰度
    gray = cv2.cvtColor(img_scaled, cv2.COLOR_BGR2GRAY)

    # 锐化
    kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
    sharp = cv2.filter2D(gray, -1, kernel)

    # 自适应二值化（反转，使文字为白色，背景为黑色）
    binary = cv2.adaptiveThreshold(
        sharp, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        blockSize=11,
        C=2
    )

    return binary


def extract_max_number(text):
    """从OCR结果中提取最大的合理金额数字"""
    if not text:
        return None

    # 清洗：去空格，保留数字、小数点、负号
    text = re.sub(r'[^\d.\-]', '', text)

    # 查找所有可能的数字（包括负数）
    numbers = re.findall(r'-?\d+\.?\d*', text)
    valid_numbers = []
    for num_str in numbers:
        try:
            val = float(num_str)
            # 过滤：金额一般在 0.01 ~ 999999 之间，负数可能是退款（取绝对值）
            if 0.01 <= abs(val) <= 999999:
                valid_numbers.append(abs(val))
        except ValueError:
            continue

    if not valid_numbers:
        return None

    # 返回最大值（通常金额是最大的数字）
    return round(max(valid_numbers), 2)


def recognize_amount_from_image(image_path, progress_callback=None):
    """
    识别图片中的金额（优化版）
    """
    if not os.path.exists(image_path):
        return {"success": False, "error": "File not found", "amount": None, "raw_text": "", "engine": "tesseract"}

    try:
        if progress_callback:
            progress_callback(10, "预处理图片...")

        processed = preprocess_for_amount(image_path)
        if processed is None:
            return {"success": False, "error": "无法读取图片", "amount": None, "raw_text": "", "engine": "tesseract"}

        if progress_callback:
            progress_callback(50, "OCR 识别中...")

        # 关键配置：单行模式 + 严格字符白名单（只识别数字、小数点、负号）
        config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789.-'
        text = pytesseract.image_to_string(processed, config=config).strip()

        if progress_callback:
            progress_callback(80, f"原始识别结果: {text}")

        amount = extract_max_number(text)

        if progress_callback:
            progress_callback(100, "完成")

        _log(f"[OCR] amount={amount} raw={text[:300]}")

        return {
            "success": True,
            "amount": amount,
            "raw_text": text,
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