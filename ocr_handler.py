"""
OCR module - recognize expense amounts from images
策略：区域裁剪 + 投影法定位金额行 + OCR 数字识别
参考：微信/支付宝支付截图布局固定，金额字体最大，可用像素投影定位
"""

import os
import re
import sys as _sys
from threading import Thread
from PIL import Image

# 打包环境下的资源路径获取函数
def _resource_path(relative_path):
    try:
        base_path = _sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base_path, relative_path)


_ocr_engine = None
_engine_type = None

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

def get_ocr_engine():
    global _ocr_engine, _engine_type, _easyocr_init_error
    if _ocr_engine is not None:
        return _ocr_engine, _engine_type

    # 1. PaddleOCR
    try:
        # 设置 PADDLEX_HOME 指向打包后的 paddlex_home 目录
        home = _resource_path("paddlex_home")
        if os.path.isdir(home):
            os.environ["PADDLEX_HOME"] = home
            # 同时设置 PADDLEOCR_HOME 指向 .paddlex 子目录（兼容旧版）
            paddleocr_home = os.path.join(home, ".paddlex")
            if os.path.isdir(paddleocr_home):
                os.environ["PADDLEOCR_HOME"] = paddleocr_home

        from paddleocr import PaddleOCR
        _ocr_engine = PaddleOCR(lang='ch')
        _engine_type = "paddleocr"
        _log(f"[OCR] paddleocr engine ready (PADDLEX_HOME={os.environ.get('PADDLEX_HOME')})")
        return _ocr_engine, _engine_type
    except ImportError:
        pass
    except Exception as e:
        _log(f"[OCR] paddleocr init error: {e}")
        import traceback
        _log(traceback.format_exc())

    return None, None


def _extract_amount_from_paddleocr_bbox(rec_polys: list, texts: list, rec_scores: list, img_height: int) -> float | None:
    """
    利用 PaddleOCR 3.6+ 的 bbox 信息：金额在截图中是最大号加粗字体。
    rec_polys 格式: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]] 四点包围盒。
    取画面中上部（15%~85%）高度最大的文本行，判断是否为金额。
    """
    if not rec_polys or not texts:
        return None

    candidates = []
    for i, poly in enumerate(rec_polys):
        if i >= len(texts):
            break
        text = texts[i]
        conf = rec_scores[i] if i < len(rec_scores) else 0

        # 取 y 范围
        y_coords = [p[1] for p in poly]
        y_top = min(y_coords)
        y_bot = max(y_coords)
        h = y_bot - y_top
        y_frac = y_top / img_height

        if y_frac < 0.15 or y_frac > 0.85:
            continue

        candidates.append((h, text, conf))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)

    for h, text, conf in candidates[:3]:
        m = re.search(r'-?\s*[¥￥]?\s*(\d+\.\d{0,2})', text)
        if m:
            try:
                val = float(m.group(1))
                if 0.01 <= val <= 100000:
                    return val
            except ValueError:
                continue

        m = re.match(r'-?\s*(\d+\.?\d{0,2})\s*$', text.strip())
        if m:
            try:
                val = float(m.group(1))
                if 0.01 <= abs(val) <= 100000:
                    return abs(val)
            except ValueError:
                continue

    return None



# ==================== 金额提取（传统全图 OCR + 正则，作为 fallback） ====================

def _extract_amount(text: str) -> float | None:
    """从 OCR 结果中提取金额数字，支持千位分隔符（如 3,454.06）"""
    if not text:
        return None

    # 预处理：将常见分隔符（空格、点、中文逗号）统一为英文逗号，但保留小数点
    # 例如 "3 454.06" → "3,454.06"  ； "3.454.06" → "3,454.06" （注意区分小数点）
    # 策略：先将所有点替换为逗号，再将最后一个逗号恢复为点（假设最后一个点才是小数点）
    cleaned = text.replace(" ", "")  # 去空格
    # 将点替换为逗号（临时），然后恢复最后一个逗号为点
    cleaned = cleaned.replace(".", ",")
    # 找到最后一个逗号的位置，恢复为点
    last_comma = cleaned.rfind(",")
    if last_comma != -1:
        cleaned = cleaned[:last_comma] + "." + cleaned[last_comma+1:]
    # 此时 cleaned 中的千位分隔符都是逗号，小数点是点

    # 1. 优先匹配带千位分隔符的金额（如 3,454.06 或 ¥3,454.06）
    m = re.search(r'[¥￥]?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{0,2})?)', cleaned)
    if m:
        try:
            val = float(m.group(1).replace(",", ""))
            if 0.01 <= val <= 100000:
                return val
        except ValueError:
            pass

    # 2. 匹配 ¥/￥ 后的数字（无逗号）
    m = re.search(r'[¥￥](\d+\.?\d{0,2})', cleaned)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # 3. 匹配标准小数金额（如 454.06）
    m = re.search(r'(\d+\.\d{2})', cleaned)
    if m:
        try:
            val = float(m.group(1))
            if 0.01 <= val <= 100000:
                return val
        except ValueError:
            pass

    # 4. 匹配任意数字（整数或小数）
    m = re.search(r'(\d+\.?\d*)', cleaned)
    if m:
        try:
            val = float(m.group(1))
            if 0.01 <= val <= 100000:
                return val
        except ValueError:
            pass

    # 5. 兜底：去掉所有非数字字符，尝试解析整数金额
    digits_only = re.sub(r'[^0-9]', '', cleaned)
    if digits_only and len(digits_only) >= 2:
        try:
            val = float(digits_only)
            if 0.01 <= val <= 100000:
                return val
        except ValueError:
            pass

    return None


# ==================== 主识别函数 ====================

def recognize_amount_from_image(image_path: str, progress_callback=None) -> dict:
    """
    识别图片中的金额。
    优先使用「区域裁剪 + 投影定位」精确方案，失败回退到全图 OCR。

    progress_callback(percent, message): 进度回调，percent 0-100
    """
    if not os.path.exists(image_path):
        return {"success": False, "error": "File not found", "amount": None, "raw_text": "", "engine": None}

    engine, engine_type = get_ocr_engine()

    if engine is None:
        return {
            "success": False,
            "error": "No OCR engine installed.",
            "amount": None, "raw_text": "", "engine": None
        }

    # 进度模拟线程（用于 PaddleOCR 无法获取真实进度时）
    def _start_progress(estimated_sec=10):
        if not progress_callback:
            return None, None
        import time as _time
        import threading as _threading
        done = [False]

        def _simulate():
            start = _time.time()
            while not done[0]:
                elapsed = _time.time() - start
                pct = min(95, max(1, int(elapsed / estimated_sec * 100)))
                progress_callback(pct, "OCR 识别中...")
                _time.sleep(0.3)
            progress_callback(100, "识别完成")

        _threading.Thread(target=_simulate, daemon=True).start()
        return done, estimated_sec

    try:
        img = Image.open(image_path).convert("RGB")

        if engine_type == "paddleocr":
            done, _ = _start_progress(estimated_sec=10)
            result = engine.ocr(image_path)
            if done:
                done[0] = True
            if not result or not result[0]:
                return {"success": False, "error": "No text detected", "amount": None, "raw_text": "", "engine": engine_type}

            ocr_result = result[0]

            # PaddleOCR 3.6+ 返回 OCRResult 对象（dict-like），使用 .json 获取结构化数据
            # json 格式: {"res": {"rec_texts": [...], "rec_scores": [...], "rec_polys": [...], ...}}
            try:
                data = ocr_result.json
                inner = data.get("res", data)  # 兼容 {"res": {...}} 或直接顶层
                texts = inner.get("rec_texts", [])
                rec_scores = inner.get("rec_scores", [])
                rec_polys = inner.get("rec_polys", [])
            except Exception:
                # 兼容旧版 list 格式: [[bbox, (text, conf)], ...]
                texts = [line[1][0] for line in ocr_result if line and len(line) >= 2]
                rec_scores = []
                rec_polys = []

            raw_text = "\n".join(texts)

            # 优先：大号字定位（bbox 高度最大 = 金额）
            amount = _extract_amount_from_paddleocr_bbox(rec_polys, texts, rec_scores, img.size[1])
            if amount is None:
                amount = _extract_amount_full(raw_text)
        else:
            return {"success": False, "error": "Unknown engine", "amount": None, "raw_text": "", "engine": None}

        _log(f"[OCR] engine={engine_type} amount={amount} raw={raw_text[:300]}")

        return {
            "success": True,
            "amount": amount,
            "raw_text": raw_text,
            "engine": engine_type
        }

    except Exception as e:
        _log(f"[OCR] error: {e}")
        return {"success": False, "error": str(e), "amount": None, "raw_text": "", "engine": engine_type}


def is_ocr_available() -> bool:
    """检查是否有可用的 OCR 引擎（不会触发模型下载）"""
    try:
        import paddleocr
        return True
    except Exception:
        pass
    return False


def init_ocr_engine():
    """预初始化 OCR 引擎（带超时），调用 get_ocr_engine 但不阻塞"""
    engine, etype = get_ocr_engine()
    return engine is not None
