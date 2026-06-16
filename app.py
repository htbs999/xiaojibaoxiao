"""
Flask OCR 接口 — 双模式改造（文件上传 + 微信小程序 Base64 JSON）
========================================================================
高级 Python 工程师实现：兼容传统 multipart/form-data 与微信小程序
application/json 请求，保持 OCR 处理逻辑统一，避免重复代码。

PEP8 规范，完善异常处理，详细注释。
"""

import base64
import logging
import re

from flask import Flask, jsonify, request

from ocr_handler import ocr_engine  # OCR 引擎实例，提供 process(image_bytes) -> dict

app = Flask(__name__)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------

def _extract_base64_image(raw_image: str) -> bytes:
    """从 Base64 字符串中提取并解码图像数据。

    兼容两种格式：
      - "data:image/png;base64,iVBORw0KGgo..."
      - "iVBORw0KGgo..."  （无前缀）

    Args:
        raw_image: 从 JSON 中提取的原始 image 字段值。

    Returns:
        解码后的二进制图像数据。

    Raises:
        ValueError: 解码失败时抛出。
    """
    # 去除 data:image/...;base64, 前缀（如存在）
    pattern = r'^data:image/[a-zA-Z]+;base64,'
    image_data = re.sub(pattern, '', raw_image, count=1).strip()

    if not image_data:
        raise ValueError('Base64 图像数据为空')

    try:
        return base64.b64decode(image_data)
    except Exception as exc:
        raise ValueError(f'Base64 解码失败: {exc}') from exc


# ------------------------------------------------------------------
# 核心路由 — 双模式 OCR 接口
# ------------------------------------------------------------------

@app.route('/ocr', methods=['POST'])
def ocr():
    """智能识别请求来源并统一执行 OCR 识别。

    请求模式 A — 传统浏览器 / 文件上传
        Content-Type: multipart/form-data
        参数: image=<图片文件>

    请求模式 B — 微信小程序
        Content-Type: application/json
        Body: {"image": "data:image/png;base64,..."}

    返回: {"text": "识别结果"} 或 {"error": "错误描述"}
    """
    # ----------------------------------------------------------
    # 第一步：判断请求类型并提取图像二进制数据
    # ----------------------------------------------------------
    is_from_miniprogram = request.content_type and 'application/json' in request.content_type

    if is_from_miniprogram:
        # ---------- 分支 A：微信小程序 Base64 JSON 请求 ----------
        try:
            payload = request.get_json(silent=True)
            if not payload or 'image' not in payload:
                return jsonify({'error': '缺少必要参数: image (Base64 字符串)'}), 400

            raw_image = payload['image']
            image_bytes = _extract_base64_image(raw_image)
        except ValueError as exc:
            logger.warning('Base64 解码失败: %s', exc)
            return jsonify({'error': f'Base64 图像数据无效: {exc}'}), 400
        except Exception as exc:
            logger.exception('JSON 请求解析异常')
            return jsonify({'error': f'请求解析失败: {exc}'}), 400

    else:
        # ---------- 分支 B：传统文件上传 multipart/form-data ----------
        if 'image' not in request.files:
            return jsonify({'error': '缺少必要参数: image (图片文件)'}), 400

        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': '上传文件为空'}), 400

        try:
            image_bytes = file.read()
        except Exception as exc:
            logger.exception('文件读取失败')
            return jsonify({'error': f'文件读取失败: {exc}'}), 400

    # ----------------------------------------------------------
    # 第二步：统一的 OCR 处理逻辑（两种模式共享，零重复）
    # ----------------------------------------------------------
    try:
        result = ocr_engine.process(image_bytes)
    except Exception as exc:
        logger.exception('OCR 引擎处理异常')
        return jsonify({'error': f'OCR 识别失败: {exc}'}), 500

    return jsonify(result)


# ------------------------------------------------------------------
# 可选：独立的测试用 OCR 引擎桩（仅供本地验证，生产环境请替换）
# ------------------------------------------------------------------
# class _MockOcrEngine:
#     """模拟 OCR 引擎，返回固定结果用于测试。"""
#     def process(self, image_bytes: bytes) -> dict:
#         return {'text': 'Hello, World!'}
#
# ocr_engine = _MockOcrEngine()


if __name__ == '__main__':
    app.run(debug=True, port=5000)
