import cv2
import numpy as np
import pytesseract
import re
import os

# 尝试从环境变量获取路径，如果没有则使用默认
try:
    TESSERACT_PATH = os.environ.get("TESSERACT_PATH")
    if TESSERACT_PATH:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
except Exception:
    pass

def _log(msg):
    print(f"[OCR-DEBUG] {msg}")

def preprocess_for_amount(image_path):
    """针对金额截图的特殊预处理：裁剪 -> 放大 -> 锐化 -> 二值化"""
    try:
        img = cv2.imread(image_path)
        if img is None:
            return None
            
        h, w, _ = img.shape
        
        # 1. 裁剪：专门针对微信/支付宝账单，只取顶部中间的金额区域
        # 如果你的截图样式固定，这个比例效果最好
        y_start = int(h * 0.15)
        y_end = int(h * 0.40)
        x_start = int(w * 0.25)
        x_end = int(w * 0.75)
        
        img_crop = img[y_start:y_end, x_start:x_end]
        
        # 2. 放大：解决字体加粗粘连问题
        scale = 3.0  # 放大3倍
        new_w = int(img_crop.shape[1] * scale)
        new_h = int(img_crop.shape[0] * scale)
        img_resized = cv2.resize(img_crop, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

        # 3. 锐化：让数字边缘更清晰
        kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
        img_sharp = cv2.filter2D(img_resized, -1, kernel)

        # 4. 二值化：黑底白字
        gray = cv2.cvtColor(img_sharp, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        
        return thresh
        
    except Exception as e:
        _log(f"预处理报错: {e}")
        return None

def extract_amount_by_pattern(text):
    """
    通过正则匹配金额特征来提取，拒绝识别错误的乱码
    """
    # 1. 清洗文本，去掉空格和换行
    clean_text = text.replace(" ", "").replace("\n", "")
    
    # 2. 重点：匹配金额特征
    # \d{1,3}(,\d{3})*(\.\d{2})?  匹配 1,060.00 或 1060.00
    # -\d+\.\d{2}               匹配 -1060.00
    # \d+\.\d{2}                匹配 1060.00
    
    # 优先匹配带千分位逗号或小数的标准金额
    # 注意：微信截图里金额通常是 -1060.00，所以我们也要考虑负号
    patterns = [
        r'-?\d{1,3}(,\d{3})*(\.\d{2})?',  # 匹配 1,234.56 或 1234.56 或 -100.00
        r'-?\d+\.\d{2}'                   # 匹配 1060.00
    ]
    
    candidates = []
    
    for pattern in patterns:
        matches = re.findall(pattern, clean_text)
        for m in matches:
            # m 可能是元组 (如果有分组)，我们要取完整匹配
            full_match = m[0] if isinstance(m, tuple) else m
            
            # 过滤掉不合理的数字
            # 1. 长度限制：正常报销单不会超过 999999.99，也不会少于 0.01
            if len(full_match) > 10: # 排除过长的时间戳或单号
                continue
                
            # 2. 必须包含小数点（防止匹配到纯数字单号）
            if '.' not in full_match:
                continue
                
            # 3. 移除逗号再转浮点数
            try:
                num_str = full_match.replace(',', '')
                num = float(num_str)
                
                # 过滤异常值：比如 0.00 的单据通常没意义，大于 10万 可能是单号
                if 0 < num < 100000:
                    candidates.append((num, full_match))
            except ValueError:
                continue
                
    if not candidates:
        return None
        
    # 3. 排序：如果有多个候选（比如同时识别到了日期和金额），优先选数值最大的那个
    # 通常金额是单据里最大的数字
    candidates.sort(reverse=True)
    return candidates[0][0]

def recognize_with_tesseract(image_path, progress_callback=None):
    try:
        if progress_callback: progress_callback(10, "正在裁剪图片...")
        processed = preprocess_for_amount(image_path)
        
        if processed is None:
            return {"success": False, "error": "图片读取失败", "amount": None}

        if progress_callback: progress_callback(50, "正在识别...")
        
        # 配置：单行模式 + 严格数字白名单
        config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789.-,'
        raw_text = pytesseract.image_to_string(processed, config=config).strip()
        
        if progress_callback: progress_callback(80, f"识别结果: {raw_text}")
        
        # 核心修正：使用正则提取，而不是找最大值
        amount = extract_amount_by_pattern(raw_text)
        
        if amount is None:
            # 备用方案：如果正则没匹配到，尝试找文本里最大的那个符合格式的浮点数
            # 这一步是为了防止正则写漏了
            numbers = re.findall(r'\d+\.\d{2}', raw_text)
            valid_numbers = [float(n) for n in numbers if 0 < float(n) < 100000]
            if valid_numbers:
                amount = max(valid_numbers)
        
        return {
            "success": True,
            "amount": amount,
            "raw_text": raw_text,
            "engine": "tesseract"
        }
        
    except Exception as e:
        _log(f"Tesseract 报错: {str(e)}")
        return {"success": False, "error": str(e), "amount": None, "raw_text": ""}

def is_ocr_available():
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False

def init_ocr_engine():
    return is_ocr_available()