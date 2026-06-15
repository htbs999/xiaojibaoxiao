"""
OCR module - recognize expense amounts from images
轻量方案：Tesseract + OpenCV 图像预处理
适用于微信/支付宝支付截图等常见报销单据
"""

import os
import re
import sys as _sys
import cv2
import numpy as np
import pytesseract

# 设置 Tesseract 可执行文件路径（Docker 环境下通常在此）
pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

# 调试日志路径（与原代码保持一致）
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


def _preprocess_image(image_path):
    """
    图像预处理：提升 OCR 识别率
    - 转灰度
    - 放大（对小字票据友好）
    - 去噪
    - OTSU 二值化
    """
    img = cv2.imread(image_path)
    if img is None:
        return None
    # 1. 转灰度
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # 2. 放大 2 倍（提高小字识别率）
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    # 3. 中值去噪
    denoised = cv2.medianBlur(gray, 3)
    # 4. OTSU 二值化
    _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def _extract_amount(text):
    """
    从 OCR 文本中提取最可能的金额数字。
    支持常见格式：
    - ¥123.45
    - 合计：123.45
    - 123.45 元
    - 3,454.06
    - 12345（纯数字）
    """
    if not text:
        return None

    # 预处理：统一空格、全角符号
    text = text.replace(" ", "").replace("　", "")

    # 模式列表（按优先级从高到低）
    patterns = [
        # 1. 带关键字（合计、总计、金额等） + 货币符号
        r'(?:合计|总计|总额|金额|小计|实付|消费)[：:\s]*[￥¥]?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)',
        # 2. 货币符号开头 + 数字（可能带千位分隔符）
        r'[￥¥]\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)',
        # 3. 数字 + 元/圆
        r'(\d{1,3}(?:,\d{3})*(?:\.\d{1,2}))\s*(?:元|圆)',
        # 4. 标准两位小数（最常见的金额格式）
        r'(\d+\.\d{2})',
        # 5. 纯整数（可能很大，但限制在合理范围）
        r'(\d{2,6})',
    ]

    best = None
    for pat in patterns:
        matches = re.findall(pat, text)
        for m in matches:
            try:
                # 去除千位分隔符
                clean = m.replace(',', '')
                val = float(clean)
                # 金额合理范围：0.01 ~ 999999
                if 0.01 <= val <= 999999:
                    # 取最大值（通常金额是最大的数字）
                    if best is None or val > best:
                        best = val
            except ValueError:
                continue
    return round(best, 2) if best else None


def recognize_amount_from_image(image_path, progress_callback=None):
    """
    识别图片中的金额。
    返回字典：
    {
        "success": bool,
        "amount": float or None,
        "raw_text": str,
        "engine": "tesseract"
    }
    """
    if not os.path.exists(image_path):
        return {"success": False, "error": "File not found", "amount": None, "raw_text": "", "engine": "tesseract"}

    try:
        if progress_callback:
            progress_callback(10, "读取图片...")

        # 图像预处理
        processed = _preprocess_image(image_path)
        if processed is None:
            return {"success": False, "error": "无法读取图片", "amount": None, "raw_text": "", "engine": "tesseract"}

        if progress_callback:
            progress_callback(40, "正在识别文字...")

        # Tesseract 配置：中文+英文，PSM 6（假设统一文本块），限制字符集以提高数字识别准确率
        custom_config = (
            r'--psm 6 '
            r'-c tessedit_char_whitelist=0123456789.,¥￥元圆整ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:/ '
        )
        text = pytesseract.image_to_string(
            processed,
            lang='chi_sim+eng',
            config=custom_config
        )

        if progress_callback:
            progress_callback(70, "提取金额...")

        amount = _extract_amount(text)

        if progress_callback:
            progress_callback(100, "完成")

        _log(f"[OCR] amount={amount} raw={text.strip()[:300]}")

        return {
            "success": True,
            "amount": amount,
            "raw_text": text.strip(),
            "engine": "tesseract"
        }

    except Exception as e:
        _log(f"[OCR] error: {e}")
        return {"success": False, "error": str(e), "amount": None, "raw_text": "", "engine": "tesseract"}


def is_ocr_available():
    """检查 Tesseract 是否可用"""
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def init_ocr_engine():
    """预初始化（Tesseract 无需预加载，此函数仅为兼容原接口）"""
    return is_ocr_available()