"""
OCR module - 微信/支付宝截图金额识别（改进版）
"""
import os
import re
import cv2
import numpy as np
import pytesseract

pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

import sys, tempfile
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


# ─────────────────────────────────────────────
# 1. 智能定位：找到"¥"或"转账金额"所在行，再裁剪
# ─────────────────────────────────────────────
def _locate_amount_region(img):
    """
    先用宽松OCR扫全图，找到含¥/元/金额关键词的行，
    返回该行附近的 ROI (y1,y2,x1,x2)，找不到返回 None。
    """
    h, w = img.shape[:2]
    # 缩小到合理尺寸，加快速度
    scale = min(1.0, 1200 / max(h, w))
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    # 宽松二值化（正常黑字白底）
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 用 psm 6（多行）+ chi_sim+eng 双语识别定位关键词
    data = pytesseract.image_to_data(
        bw,
        config='--oem 3 --psm 6',
        lang='chi_sim+eng',
        output_type=pytesseract.Output.DICT
    )

    keywords = ['¥', '￥', '元', '金额', '付款', '收款', '转账', 'CNY']
    found_rows = []
    for i, word in enumerate(data['text']):
        if any(kw in word for kw in keywords) and int(data['conf'][i]) > 0:
            top = int(data['top'][i] / scale)
            ht  = int(data['height'][i] / scale)
            found_rows.append(top + ht // 2)

    if not found_rows:
        return None

    # 取第一个关键词位置，向下扩展一段作为ROI
    cy = found_rows[0]
    margin = int(h * 0.12)
    y1 = max(0, cy - margin)
    y2 = min(h, cy + margin * 2)
    x1 = int(w * 0.1)
    x2 = int(w * 0.9)
    return y1, y2, x1, x2


# ─────────────────────────────────────────────
# 2. 图像预处理（针对截图优化）
# ─────────────────────────────────────────────
def _preprocess(crop_bgr, scale=3.0):
    """
    放大 → 去噪 → 灰度 → 二值化（白底黑字，Tesseract 友好）
    """
    h, w = crop_bgr.shape[:2]
    enlarged = cv2.resize(crop_bgr, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_CUBIC)

    # 轻度去噪，保留边缘
    denoised = cv2.fastNlMeansDenoisingColored(enlarged, None, 6, 6, 7, 21)

    gray = cv2.cvtColor(denoised, cv2.COLOR_BGR2GRAY)

    # OTSU 自适应阈值（白底黑字，不反色）
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 轻微膨胀，修复笔画断裂
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    binary = cv2.dilate(binary, kernel, iterations=1)

    return binary


def _preprocess_dark_bg(crop_bgr, scale=3.0):
    """深色背景版本（微信红包等）"""
    h, w = crop_bgr.shape[:2]
    enlarged = cv2.resize(crop_bgr, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    # 反色，使文字变黑
    inv = cv2.bitwise_not(gray)
    _, binary = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def _is_dark_background(crop_bgr):
    """判断截图是否深色背景（红包/夜间模式）"""
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    return np.mean(gray) < 100


# ─────────────────────────────────────────────
# 3. 金额提取（更精准的正则 + 上下文感知）
# ─────────────────────────────────────────────
# 金额前置标记（用于上下文匹配）
_AMOUNT_PREFIX = re.compile(
    r'[¥￥]\s*(-?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|-?\d+(?:\.\d{1,2})?)'
)
# 标准小数金额
_AMOUNT_DECIMAL = re.compile(r'(?<!\d)(\d{1,6}\.\d{2})(?!\d)')
# 退化：整数金额（仅在前两种都失败时用）
_AMOUNT_INT = re.compile(r'(?<!\d)(\d{2,5})(?!\d)')

# 黑名单：常见误匹配（年份、手机号片段等）
_BLACKLIST = re.compile(r'^(20\d{2}|19\d{2}|0\d{3,})$')


def _extract_amount(text):
    if not text:
        return None

    clean = text.replace(' ', '').replace('\n', '').replace('\r', '')
    _log(f"[extract] clean text: {clean[:200]}")

    # 优先级1：带 ¥ 前缀的金额（最可信）
    for m in _AMOUNT_PREFIX.finditer(clean):
        val_str = m.group(1).replace(',', '')
        try:
            val = abs(float(val_str))
            if 0.01 <= val <= 999999:
                _log(f"[extract] ¥-prefix match: {val}")
                return round(val, 2)
        except ValueError:
            continue

    # 优先级2：带小数点的金额（xx.xx 格式）
    candidates = []
    for m in _AMOUNT_DECIMAL.finditer(clean):
        val_str = m.group(1)
        try:
            val = float(val_str)
            if 0.01 <= val <= 999999 and not _BLACKLIST.match(val_str):
                candidates.append(val)
        except ValueError:
            continue
    if candidates:
        result = round(max(candidates), 2)
        _log(f"[extract] decimal match: {result} from {candidates}")
        return result

    # 优先级3：纯整数（风险最高，仅兜底）
    int_candidates = []
    for m in _AMOUNT_INT.finditer(clean):
        val_str = m.group(1)
        if _BLACKLIST.match(val_str):
            continue
        try:
            val = int(val_str)
            if 1 <= val <= 99999:
                int_candidates.append(val)
        except ValueError:
            continue
    if int_candidates:
        result = float(max(int_candidates))
        _log(f"[extract] int fallback: {result}")
        return result

    return None


# ─────────────────────────────────────────────
# 4. 主识别函数
# ─────────────────────────────────────────────
def recognize_amount_from_image(image_path, progress_callback=None):
    if not os.path.exists(image_path):
        return {"success": False, "error": "File not found",
                "amount": None, "raw_text": "", "engine": "tesseract"}

    def _progress(pct, msg):
        if progress_callback:
            progress_callback(pct, msg)
        _log(f"[progress {pct}%] {msg}")

    try:
        img = cv2.imread(image_path)
        if img is None:
            return {"success": False, "error": "Cannot read image",
                    "amount": None, "raw_text": "", "engine": "tesseract"}

        dark_bg = _is_dark_background(img)
        preprocess_fn = _preprocess_dark_bg if dark_bg else _preprocess

        # ── 策略1：智能定位 ROI ──────────────────────
        _progress(10, "智能定位金额区域...")
        roi = _locate_amount_region(img)
        if roi:
            y1, y2, x1, x2 = roi
            crop = img[y1:y2, x1:x2]
            processed = preprocess_fn(crop, scale=3.0)

            # chi_sim 识别（支持 ¥ 符号）
            cfg = '--oem 3 --psm 7'
            raw = pytesseract.image_to_string(processed, config=cfg, lang='chi_sim+eng').strip()
            amount = _extract_amount(raw)
            _log(f"[S1-chi] raw={raw[:100]}, amount={amount}")

            # 如果 chi_sim 失败，同区域用纯数字白名单再试一次
            if amount is None:
                cfg_num = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789.,¥￥'
                raw2 = pytesseract.image_to_string(processed, config=cfg_num).strip()
                amount = _extract_amount(raw2)
                raw = raw2
                _log(f"[S1-num] raw={raw[:100]}, amount={amount}")

            if amount is not None:
                _progress(100, "完成")
                return {"success": True, "amount": amount, "raw_text": raw, "engine": "tesseract"}

        # ── 策略2：固定比例裁剪（兜底）────────────────
        _progress(50, "固定区域识别...")
        h, w = img.shape[:2]
        # 多个候选区域（适配不同 App 布局）
        candidate_regions = [
            (int(h*0.10), int(h*0.45), int(w*0.15), int(w*0.85)),  # 通用
            (int(h*0.20), int(h*0.55), int(w*0.20), int(w*0.80)),  # 偏下
            (0,           int(h*0.35), 0,           w),             # 全宽顶部
        ]
        for region in candidate_regions:
            y1, y2, x1, x2 = region
            crop = img[y1:y2, x1:x2]
            processed = preprocess_fn(crop, scale=2.5)
            cfg = '--oem 3 --psm 6'
            raw = pytesseract.image_to_string(processed, config=cfg, lang='chi_sim+eng').strip()
            amount = _extract_amount(raw)
            _log(f"[S2] region={region}, raw={raw[:100]}, amount={amount}")
            if amount is not None:
                _progress(100, "完成")
                return {"success": True, "amount": amount, "raw_text": raw, "engine": "tesseract"}

        _progress(90, "未提取到金额")
        return {"success": True, "amount": None, "raw_text": raw if 'raw' in locals() else "",
                "engine": "tesseract"}

    except Exception as e:
        _log(f"[OCR] error: {e}")
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