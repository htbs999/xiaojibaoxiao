"""
OCR module - 微信/支付宝截图金额识别（v3，覆盖16种真实截图场景）
"""
import os
import re
import cv2
import numpy as np
import pytesseract

pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

import sys
import tempfile
if getattr(sys, 'frozen', False):
    _LOG_PATH = os.path.join(os.path.dirname(sys.executable), "ocr_debug.log")
else:
    _LOG_PATH = os.path.join(tempfile.gettempdir(), "wechat_expense_ocr_debug.log")

def _log(msg):
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
# 1. 背景检测
# ═══════════════════════════════════════════════════════
def _is_dark_background(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return np.mean(gray) < 100


# ═══════════════════════════════════════════════════════
# 2. 图像预处理（分深色/浅色背景）
# ═══════════════════════════════════════════════════════
def _preprocess(crop_bgr, scale=3.0, dark=False):
    h, w = crop_bgr.shape[:2]
    enlarged = cv2.resize(crop_bgr,
                          (int(w * scale), int(h * scale)),
                          interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    if dark:
        # 深色背景：反色后二值化
        inv = cv2.bitwise_not(gray)
        _, binary = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return binary


# ═══════════════════════════════════════════════════════
# 3. 水印去除（去除彩色文字水印，保留黑/白金额数字）
#    原理：金额是纯黑(白底)或纯白(黑底)，水印是彩色
# ═══════════════════════════════════════════════════════
def _remove_color_watermark(crop_bgr, dark=False):
    """
    通过颜色饱和度过滤去除彩色水印。
    白底：保留低饱和度深色像素（黑色文字）
    黑底：保留低饱和度浅色像素（白色文字）
    """
    hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]  # 饱和度通道

    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    result = gray.copy()

    # 高饱和度区域（水印）→ 替换为背景色
    watermark_mask = saturation > 60  # 饱和度>60认为是彩色水印
    if dark:
        result[watermark_mask] = 0    # 深色背景填黑（等同于背景）
    else:
        result[watermark_mask] = 255  # 浅色背景填白（等同于背景）

    return result


# ═══════════════════════════════════════════════════════
# 4. 连通域定位"最大数字行"
#    改进：用宽高比过滤，排除多行标题文字（宽高比大）
# ═══════════════════════════════════════════════════════
def _find_amount_row(img, dark=False):
    """
    金额数字特征：
    - 字体最大（高度最高）
    - 水平居中
    - 宽高比接近数字字符（单个字符 h > w*0.5）
    
    返回 (y1, y2, x1, x2, char_h) 或 None
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if dark:
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    else:
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    h, w = img.shape[:2]
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)

    min_area = h * w * 0.0002
    max_area = h * w * 0.04

    candidates = []
    for i in range(1, num_labels):
        area   = stats[i, cv2.CC_STAT_AREA]
        ch     = stats[i, cv2.CC_STAT_HEIGHT]
        cw     = stats[i, cv2.CC_STAT_WIDTH]
        cy     = stats[i, cv2.CC_STAT_TOP] + ch // 2
        cx     = stats[i, cv2.CC_STAT_LEFT] + cw // 2

        if not (min_area < area < max_area):
            continue
        # 必须在水平中央区域
        if not (w * 0.1 < cx < w * 0.9):
            continue
        # 宽高比过滤：金额数字字符高度远大于宽度的一半
        # 多行标题整体连通域会很宽，单字符才满足此条件
        if cw > ch * 4:   # 过滤横向超宽的连通域（如分割线）
            continue

        candidates.append((ch, cy, area, cx))

    if not candidates:
        return None

    # 按字高排序，取最高的一批（允许同一行有多个字符）
    candidates.sort(key=lambda x: -x[0])
    tallest_h = candidates[0][0]

    # 同行字符：字高在最大字高 80% 以上，且 y 坐标接近
    same_row = [c for c in candidates if c[0] >= tallest_h * 0.8]
    row_y_values = [c[1] for c in same_row]
    median_y = sorted(row_y_values)[len(row_y_values) // 2]

    # 过滤掉 y 偏差超过 1 个字高的（排除其他行的大字）
    same_row = [c for c in same_row if abs(c[1] - median_y) < tallest_h]

    margin = int(tallest_h * 1.3)
    y1 = max(0, int(median_y - margin))
    y2 = min(h, int(median_y + margin))
    x1 = int(w * 0.03)
    x2 = int(w * 0.97)

    return y1, y2, x1, x2, tallest_h


# ═══════════════════════════════════════════════════════
# 5. 金额正则提取（三优先级 + 黑名单）
# ═══════════════════════════════════════════════════════

# 手机号黑名单（11位数字1开头）
_PHONE_RE   = re.compile(r'1[3-9]\d{9}')
# 年份黑名单
_YEAR_RE    = re.compile(r'20[0-9]{2}')
# 长流水号黑名单（8位以上纯数字）
_SERIAL_RE  = re.compile(r'\d{8,}')

def _clean_blacklist(text):
    """移除手机号、年份、流水单号，避免干扰金额提取"""
    text = _PHONE_RE.sub('', text)
    text = _YEAR_RE.sub('', text)
    text = _SERIAL_RE.sub('', text)
    return text

# 优先级1：¥ 前缀金额（支付成功即时页，如图6）
_PAT_YEN    = re.compile(r'[¥￥]\s*(\d{1,6}(?:\.\d{1,2})?)')
# 优先级2：负号+小数（账单详情页主流格式）
_PAT_NEG    = re.compile(r'-\s*(\d{1,6}\.\d{2})(?!\d)')
# 优先级3：纯小数（支付宝，如图15，无¥无-）
_PAT_DEC    = re.compile(r'(?<!\d)(\d{1,6}\.\d{2})(?!\d)')
# 优先级4：整数兜底（仅用清洗后文本）
_PAT_INT    = re.compile(r'(?<!\d)(\d{2,5})(?!\d)')


def _extract_amount(text):
    if not text:
        return None

    raw = text.replace(' ', '').replace('\n', '').replace('\r', '')
    raw = raw.replace('O', '0').replace('o', '0').replace('l', '1')
    _log(f"[extract] raw: {raw[:300]}")

    # 优先级1：¥前缀（最可信，图6场景）
    for m in _PAT_YEN.finditer(raw):
        val = abs(float(m.group(1)))
        if 0.01 <= val <= 999999:
            _log(f"[extract] ¥-prefix: {val}")
            return round(val, 2)

    # 优先级2：负号小数
    for m in _PAT_NEG.finditer(raw):
        val = abs(float(m.group(1)))
        if 0.01 <= val <= 999999:
            _log(f"[extract] neg-decimal: {val}")
            return round(val, 2)

    # 优先级3：纯小数（先清洗，去掉时间戳里的 10:15:08 这类）
    # 过滤掉时间格式 HH:MM:SS 中的数字
    cleaned_for_dec = re.sub(r'\d{1,2}:\d{2}:\d{2}', '', raw)
    candidates = []
    for m in _PAT_DEC.finditer(cleaned_for_dec):
        val = abs(float(m.group(1)))
        if 0.01 <= val <= 999999:
            candidates.append(val)
    if candidates:
        result = round(max(candidates), 2)
        _log(f"[extract] decimal: {result}")
        return result

    # 优先级4：整数兜底（清洗掉手机号/单号/年份后再匹配）
    cleaned_for_int = _clean_blacklist(raw)
    # 再去掉时间
    cleaned_for_int = re.sub(r'\d{1,2}:\d{2}:\d{2}', '', cleaned_for_int)
    int_candidates = []
    for m in _PAT_INT.finditer(cleaned_for_int):
        val = int(m.group(1))
        if 1 <= val <= 99999:
            int_candidates.append(val)
    if int_candidates:
        result = float(max(int_candidates))
        _log(f"[extract] int fallback: {result}")
        return result

    return None


# ═══════════════════════════════════════════════════════
# 6. 主识别函数
# ═══════════════════════════════════════════════════════
def recognize_amount_from_image(image_path, progress_callback=None):
    if not os.path.exists(image_path):
        return {"success": False, "error": "File not found",
                "amount": None, "raw_text": "", "engine": "tesseract"}

    def _prog(pct, msg):
        _log(f"[{pct}%] {msg}")
        if progress_callback:
            progress_callback(pct, msg)

    try:
        img = cv2.imread(image_path)
        if img is None:
            return {"success": False, "error": "Cannot read image",
                    "amount": None, "raw_text": "", "engine": "tesseract"}

        dark = _is_dark_background(img)
        _log(f"[main] dark={dark}, shape={img.shape}")

        # ── 策略1：连通域定位 + 水印去除 ──────────────
        _prog(10, "连通域定位金额行...")
        roi = _find_amount_row(img, dark=dark)

        if roi:
            y1, y2, x1, x2, char_h = roi
            crop = img[y1:y2, x1:x2]

            # 先尝试去水印再预处理
            for use_watermark_removal in [True, False]:
                if use_watermark_removal:
                    gray_clean = _remove_color_watermark(crop, dark=dark)
                    # 放大
                    scale = max(1.5, 80 / max(char_h, 1))
                    h2, w2 = gray_clean.shape
                    enlarged = cv2.resize(gray_clean,
                                         (int(w2*scale), int(h2*scale)),
                                         interpolation=cv2.INTER_CUBIC)
                    _, processed = cv2.threshold(enlarged, 0, 255,
                                                 cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                else:
                    processed = _preprocess(crop, scale=max(1.5, 80/max(char_h,1)), dark=dark)

                cfg = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789.,-¥￥'
                raw = pytesseract.image_to_string(processed, config=cfg).strip()
                amount = _extract_amount(raw)
                _log(f"[S1 wm={use_watermark_removal}] raw={raw[:100]}, amount={amount}")
                if amount is not None:
                    _prog(100, "完成")
                    return {"success": True, "amount": amount,
                            "raw_text": raw, "engine": "tesseract"}

        # ── 策略2：多区域扫描（兜底）─────────────────
        _prog(50, "多区域扫描...")
        h, w = img.shape[:2]
        regions = [
            (0.15, 0.50),  # 通用账单详情页
            (0.08, 0.40),  # 支付成功即时页（图6，金额偏上）
            (0.20, 0.55),  # 偏下布局
            (0.10, 0.60),  # 宽范围兜底
        ]
        last_raw = ""
        for y1_r, y2_r in regions:
            crop = img[int(h*y1_r):int(h*y2_r), int(w*0.05):int(w*0.95)]
            # 同样先尝试去水印
            for use_wm in [True, False]:
                if use_wm:
                    gray_c = _remove_color_watermark(crop, dark=dark)
                    h2, w2 = gray_c.shape
                    enlarged = cv2.resize(gray_c, (int(w2*2.5), int(h2*2.5)),
                                          interpolation=cv2.INTER_CUBIC)
                    _, processed = cv2.threshold(enlarged, 0, 255,
                                                 cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                else:
                    processed = _preprocess(crop, scale=2.5, dark=dark)

                cfg = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789.,-¥￥'
                raw = pytesseract.image_to_string(processed, config=cfg).strip()
                last_raw = raw
                amount = _extract_amount(raw)
                _log(f"[S2 region={y1_r}-{y2_r} wm={use_wm}] raw={raw[:80]}, amount={amount}")
                if amount is not None:
                    _prog(100, "完成")
                    return {"success": True, "amount": amount,
                            "raw_text": raw, "engine": "tesseract"}

        _prog(90, "未提取到金额")
        return {"success": True, "amount": None,
                "raw_text": last_raw, "engine": "tesseract"}

    except Exception as e:
        _log(f"[error] {e}")
        return {"success": False, "error": str(e),
                "amount": None, "raw_text": "", "engine": "tesseract"}


def is_ocr_available():
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False

def init_ocr_engine():
    return is_ocr_available()