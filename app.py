"""
Flask OCR + 报销管理 API — 双模式改造（文件上传 + 微信小程序 Base64 JSON）
=============================================================================
高级 Python 工程师实现：兼容传统 multipart/form-data 与微信小程序
application/json 请求，保持 OCR 处理逻辑统一，避免重复代码。

同时包含完整的报销管理后端：
  - JWT 认证（登录/注册/鉴权）
  - 报销记录的 CRUD（增删改查+搜索+分页）
  - 文本智能解析
  - Excel / PDF 导出
  - OCR 异步识别（支持进度轮询）
  - 图片上传与静态文件服务

PEP8 规范，完善异常处理，详细注释。
"""

import base64
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps

import jwt
from flask import Flask, jsonify, request, send_file
from werkzeug.utils import secure_filename

from db_manager import (
    add_expense,
    create_ocr_task,
    delete_all_expenses,
    delete_expense,
    delete_expenses_batch,
    ensure_admin_exists,
    get_all_expenses_with_user,
    get_expenses_with_user,
    get_or_create_user,
    get_connection,
    get_ocr_task,
    init_db,
    update_expense,
    update_ocr_task,
)
from excel_exporter import export_to_excel
from ocr_handler import init_ocr_engine, recognize_amount_from_image
from pdf_exporter import export_to_pdf
from text_parser import parse_expense_text

app = Flask(__name__)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# JWT 认证配置
# ------------------------------------------------------------------
JWT_SECRET = "expense-manager-secret-key-2026"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24

# ------------------------------------------------------------------
# 文件上传目录（存放 OCR 识别的图片）
# ------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ------------------------------------------------------------------
# 24 小时自动清理过期数据
# ------------------------------------------------------------------

CLEANUP_INTERVAL = 3600  # 每小时检查一次
DATA_RETENTION_HOURS = 24


def _cleanup_old_data():
    """删除超过 24 小时的图片文件及对应的 OCR 任务记录"""
    while True:
        try:
            now = time.time()
            cutoff = now - DATA_RETENTION_HOURS * 3600
            deleted = 0

            # 扫描 uploads 目录，删除过期文件
            if os.path.isdir(UPLOAD_FOLDER):
                for fname in os.listdir(UPLOAD_FOLDER):
                    fpath = os.path.join(UPLOAD_FOLDER, fname)
                    if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        deleted += 1

            # 清理数据库里对应的过期 OCR 任务记录
            from db_manager import get_connection
            conn = get_connection()
            expiry = (datetime.utcnow() - timedelta(hours=DATA_RETENTION_HOURS)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                "DELETE FROM ocr_tasks WHERE created_at < ?", (expiry,)
            )
            conn.commit()
            conn.close()

            if deleted:
                logger.info("清理过期文件 %d 个", deleted)
        except Exception:
            logger.exception("清理过期数据异常")

        time.sleep(CLEANUP_INTERVAL)


# 启动后台清理线程（daemon=True，随主进程退出）
_cleanup_thread = threading.Thread(target=_cleanup_old_data, daemon=True)
_cleanup_thread.start()


# ------------------------------------------------------------------
# JWT 辅助函数
# ------------------------------------------------------------------

def generate_token(user_id: int, username: str, is_admin: bool) -> str:
    """生成 JWT 令牌"""
    payload = {
        "user_id": user_id,
        "username": username,
        "is_admin": is_admin,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    """解码 JWT 令牌，失败返回 None"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def require_auth(f):
    """JWT 认证装饰器：从 Authorization Header 提取并验证令牌"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "未提供认证令牌"}), 401
        token = auth_header[7:]
        payload = decode_token(token)
        if payload is None:
            return jsonify({"error": "令牌无效或已过期"}), 401
        request.current_user = payload
        return f(*args, **kwargs)

    return decorated


# ------------------------------------------------------------------
# Base64 图像解码
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
    pattern = r"^data:image/[a-zA-Z]+;base64,"
    image_data = re.sub(pattern, "", raw_image, count=1).strip()

    if not image_data:
        raise ValueError("Base64 图像数据为空")

    try:
        return base64.b64decode(image_data)
    except Exception as exc:
        raise ValueError(f"Base64 解码失败: {exc}") from exc


# ------------------------------------------------------------------
# 异步 OCR 后台任务
# ------------------------------------------------------------------

def _run_ocr_task(task_id: str, image_bytes: bytes):
    """后台线程执行 OCR 识别并更新数据库中的任务状态

    Args:
        task_id: 任务唯一标识
        image_bytes: 图像二进制数据
    """
    suffix = ".png"

    try:
        update_ocr_task(task_id, progress=10, status="准备图像...")

        # 保存图片副本到上传目录（供前端查看）
        filename = f"ocr_{task_id}.png"
        save_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(save_path, "wb") as f:
            f.write(image_bytes)
        update_ocr_task(task_id, image_path=filename)

        # 写入临时文件供 OpenCV 读取
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name

        try:
            update_ocr_task(task_id, progress=50, status="OCR 分析中...")

            result = recognize_amount_from_image(tmp_path)

            if result.get("success"):
                amount = result.get("amount")
                update_ocr_task(
                    task_id,
                    progress=100, done=1, ok=1,
                    amount=amount,
                    raw_text=result.get("raw_text", ""),
                    engine=result.get("engine", "tesseract"),
                    status="完成" if amount is not None else "未提取到金额",
                )
            else:
                update_ocr_task(
                    task_id,
                    progress=100, done=1, ok=0,
                    error=result.get("error", "识别失败"),
                    status="识别失败",
                )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as e:
        update_ocr_task(
            task_id,
            progress=100, done=1, ok=0,
            error=str(e), status="系统错误",
        )


# ------------------------------------------------------------------
# 静态页面路由
# ------------------------------------------------------------------

@app.route("/")
def serve_index():
    """首页：报销管理主界面"""
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/login")
def serve_login():
    """登录页"""
    return send_file(os.path.join(BASE_DIR, "login.html"))


# ------------------------------------------------------------------
# 认证接口
# ------------------------------------------------------------------

@app.route("/api/login", methods=["POST"])
def api_login():
    """用户登录/注册

    请求: {"username": "张三"}
    返回: {"token": "...", "username": "张三"}
    """
    data = request.get_json(silent=True)
    if not data or "username" not in data:
        return jsonify({"error": "缺少用户名"}), 400

    username = data["username"].strip()
    if not username:
        return jsonify({"error": "用户名不能为空"}), 400

    user = get_or_create_user(username)
    token = generate_token(user["id"], user["username"], user["is_admin"])
    return jsonify({"token": token, "username": user["username"]})


@app.route("/api/whoami", methods=["GET"])
@require_auth
def api_whoami():
    """获取当前用户信息"""
    return jsonify({
        "username": request.current_user["username"],
        "is_admin": request.current_user["is_admin"],
    })


@app.route("/api/logout", methods=["POST"])
@require_auth
def api_logout():
    """登出（JWT 无状态，客户端自行清除令牌）"""
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# 报销记录 CRUD + 搜索 + 分页
# ------------------------------------------------------------------

@app.route("/api/expenses/search", methods=["GET"])
@require_auth
def api_search_expenses():
    """搜索报销记录（支持分页、多条件筛选）

    查询参数:
        page (int): 页码，默认 1
        per_page (int): 每页条数，默认 20
        q (str): 全文搜索关键词
        category (str): 品类筛选
        person (str): 报销人筛选
        start_date (str): 起始日期 YYYY-MM-DD
        end_date (str): 截止日期 YYYY-MM-DD
    """
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    person = request.args.get("person", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    user_id = request.current_user["user_id"]
    is_admin = request.current_user["is_admin"]

    # admin 看全部，普通用户只看自己的
    if is_admin:
        all_expenses = get_all_expenses_with_user()
    else:
        all_expenses = get_expenses_with_user(user_id)

    # 筛选
    filtered = []
    for exp in all_expenses:
        if q:
            haystack = (
                exp.get("person", "")
                + exp.get("category", "")
                + exp.get("remark", "")
            )
            if q.lower() not in haystack.lower():
                continue
        if category and exp.get("category", "") != category:
            continue
        if person and exp.get("person", "") != person:
            continue
        if start_date and exp.get("date", "") < start_date:
            continue
        if end_date and exp.get("date", "") > end_date:
            continue
        filtered.append(exp)

    # 分页
    total = len(filtered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    total_amount = round(sum(e.get("amount", 0) for e in filtered), 2)
    start = (page - 1) * per_page
    end = start + per_page
    page_data = filtered[start:end]

    return jsonify({
        "data": page_data,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_amount": total_amount,
    })


@app.route("/api/stats", methods=["GET"])
@require_auth
def api_stats():
    """获取统计数据

    返回: {
        total_count, total_amount,
        by_category: [{category, count, amount}],
        by_person: [{person, count, amount}],
    }
    """
    user_id = request.current_user["user_id"]
    is_admin = request.current_user["is_admin"]

    if is_admin:
        expenses = get_all_expenses_with_user()
    else:
        expenses = get_expenses_with_user(user_id)

    total_count = len(expenses)
    total_amount = round(sum(e.get("amount", 0) for e in expenses), 2)

    # 按品类统计
    by_category_map: dict = {}
    for e in expenses:
        cat = e.get("category", "其他")
        if cat not in by_category_map:
            by_category_map[cat] = {"category": cat, "count": 0, "amount": 0.0}
        by_category_map[cat]["count"] += 1
        by_category_map[cat]["amount"] += e.get("amount", 0)

    # 按人员统计
    by_person_map: dict = {}
    for e in expenses:
        p = e.get("person", "未知")
        if p not in by_person_map:
            by_person_map[p] = {"person": p, "count": 0, "amount": 0.0}
        by_person_map[p]["count"] += 1
        by_person_map[p]["amount"] += e.get("amount", 0)

    return jsonify({
        "total_count": total_count,
        "total_amount": total_amount,
        "by_category": sorted(by_category_map.values(), key=lambda x: x["amount"], reverse=True),
        "by_person": sorted(by_person_map.values(), key=lambda x: x["amount"], reverse=True),
    })


@app.route("/api/expenses", methods=["POST"])
@require_auth
def api_add_expense():
    """新增报销记录

    请求体: {date, person, category, amount, remark?, image_path?}
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "缺少请求数据"}), 400

    required = ["date", "person", "category", "amount"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"缺少必要字段: {field}"}), 400

    try:
        expense_id = add_expense(
            date=data["date"],
            person=data["person"],
            category=data["category"],
            amount=float(data["amount"]),
            remark=data.get("remark", ""),
            image_path=data.get("image_path", ""),
            user_id=request.current_user["user_id"],
        )
        return jsonify({"id": expense_id, "ok": True}), 201
    except Exception as e:
        logger.exception("新增报销记录失败")
        return jsonify({"error": f"保存失败: {e}"}), 500


@app.route("/api/expenses/<int:expense_id>", methods=["PUT"])
@require_auth
def api_update_expense(expense_id: int):
    """更新报销记录"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "缺少请求数据"}), 400

    try:
        update_expense(
            expense_id=expense_id,
            date=data.get("date", ""),
            person=data.get("person", ""),
            category=data.get("category", ""),
            amount=float(data.get("amount", 0)),
            remark=data.get("remark", ""),
        )
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("更新报销记录失败")
        return jsonify({"error": f"更新失败: {e}"}), 500


@app.route("/api/expenses/<int:expense_id>", methods=["DELETE"])
@require_auth
def api_delete_expense(expense_id: int):
    """删除报销记录"""
    try:
        delete_expense(expense_id)
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("删除报销记录失败")
        return jsonify({"error": f"删除失败: {e}"}), 500


@app.route("/api/expenses/batch-delete", methods=["POST"])
@require_auth
def api_batch_delete():
    """批量删除报销记录"""
    data = request.get_json(silent=True)
    if not data or "ids" not in data or not isinstance(data["ids"], list):
        return jsonify({"error": "缺少 ids 参数，格式: {\"ids\": [101,102]}"}), 400
    ids = data["ids"]
    if not ids:
        return jsonify({"error": "ids 不能为空"}), 400
    try:
        delete_expenses_batch(ids)
        return jsonify({"ok": True, "deleted": len(ids)})
    except Exception as e:
        logger.exception("批量删除失败")
        return jsonify({"error": f"批量删除失败: {e}"}), 500


@app.route("/api/expenses/all", methods=["DELETE"])
@require_auth
def api_clear_all():
    """清空所有报销记录（高危操作，仅管理员可执行）"""
    if not request.current_user.get("is_admin"):
        return jsonify({"error": "仅管理员可执行清空操作"}), 403
    try:
        delete_all_expenses()
        return jsonify({"ok": True, "message": "已清空全部报销记录"})
    except Exception as e:
        logger.exception("清空失败")
        return jsonify({"error": f"清空失败: {e}"}), 500


# ------------------------------------------------------------------
# 文本解析接口
# ------------------------------------------------------------------

@app.route("/api/parse_text", methods=["POST"])
@require_auth
def api_parse_text():
    """智能解析报账文本

    请求: {"text": "张三6月1号请客吃饭花了128.5元"}
    返回: {"records": [{date, person, category, amount, remark}]}
    """
    data = request.get_json(silent=True)
    if not data or "text" not in data:
        return jsonify({"error": "缺少文本"}), 400

    try:
        records = parse_expense_text(data["text"])
        return jsonify({"records": records})
    except Exception as e:
        logger.exception("文本解析异常")
        return jsonify({"error": f"解析失败: {e}"}), 500


# ------------------------------------------------------------------
# 数据导出接口
# ------------------------------------------------------------------

@app.route("/api/export", methods=["GET"])
@require_auth
def api_export():
    """导出报销记录（Excel / PDF）

    查询参数:
        format: "excel" 或 "pdf"
        其余筛选参数同 /api/expenses/search
    """
    fmt = request.args.get("format", "excel")
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    person = request.args.get("person", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    user_id = request.current_user["user_id"]
    is_admin = request.current_user["is_admin"]

    if is_admin:
        all_expenses = get_all_expenses_with_user()
    else:
        all_expenses = get_expenses_with_user(user_id)

    # 应用筛选条件
    filtered = []
    for exp in all_expenses:
        if q:
            haystack = (
                exp.get("person", "")
                + exp.get("category", "")
                + exp.get("remark", "")
            )
            if q.lower() not in haystack.lower():
                continue
        if category and exp.get("category", "") != category:
            continue
        if person and exp.get("person", "") != person:
            continue
        if start_date and exp.get("date", "") < start_date:
            continue
        if end_date and exp.get("date", "") > end_date:
            continue
        filtered.append(exp)

    if fmt == "pdf":
        output_path = export_to_pdf(filtered)
    else:
        output_path = export_to_excel(filtered)

    download_name = os.path.basename(output_path)
    return send_file(output_path, as_attachment=True, download_name=download_name)


# ------------------------------------------------------------------
# 图片文件服务
# ------------------------------------------------------------------

@app.route("/api/image/<filename>", methods=["GET"])
@require_auth
def api_get_image(filename: str):
    """获取 OCR 识别的截图文件（超过 24 小时的自动返回 404）"""
    safe_name = secure_filename(filename)
    filepath = os.path.join(UPLOAD_FOLDER, safe_name)
    if not os.path.isfile(filepath):
        return jsonify({"error": "图片不存在"}), 404

    # 检查文件是否超过 24 小时
    if os.path.getmtime(filepath) < time.time() - DATA_RETENTION_HOURS * 3600:
        try:
            os.remove(filepath)
        except OSError:
            pass
        return jsonify({"error": "图片已过期（超过 24 小时）"}), 404

    return send_file(filepath)


# ------------------------------------------------------------------
# 核心路由 — 双模式 OCR 接口（同步 + 异步任务）
# ------------------------------------------------------------------

@app.route("/api/ocr", methods=["POST"])
@require_auth
def api_ocr():
    """智能识别请求来源，统一创建 OCR 异步识别任务。

    请求模式 A — 传统浏览器 / 文件上传
        Content-Type: multipart/form-data
        参数: image=<图片文件>

    请求模式 B — 微信小程序
        Content-Type: application/json
        Body: {"image": "data:image/png;base64,..."}

    返回: {"task_id": "abc123"}
    """
    # 第一步：判断请求类型并提取图像二进制数据
    is_from_miniprogram = (
        request.content_type and "application/json" in request.content_type
    )

    if is_from_miniprogram:
        # ---------- 分支 A：微信小程序 Base64 JSON 请求 ----------
        try:
            payload = request.get_json(silent=True)
            if not payload or "image" not in payload:
                return jsonify({"error": "缺少必要参数: image (Base64 字符串)"}), 400
            raw_image = payload["image"]
            image_bytes = _extract_base64_image(raw_image)
        except ValueError as exc:
            logger.warning("Base64 解码失败: %s", exc)
            return jsonify({"error": f"Base64 图像数据无效: {exc}"}), 400
        except Exception as exc:
            logger.exception("JSON 请求解析异常")
            return jsonify({"error": f"请求解析失败: {exc}"}), 400
    else:
        # ---------- 分支 B：传统文件上传 multipart/form-data ----------
        if "image" not in request.files:
            return jsonify({"error": "缺少必要参数: image (图片文件)"}), 400

        file = request.files["image"]
        if file.filename == "":
            return jsonify({"error": "上传文件为空"}), 400

        try:
            image_bytes = file.read()
        except Exception as exc:
            logger.exception("文件读取失败")
            return jsonify({"error": f"文件读取失败: {exc}"}), 400

    # 第二步：创建异步 OCR 任务（写入 SQLite，多 worker 共享）
    task_id = uuid.uuid4().hex[:12]
    create_ocr_task(task_id)

    thread = threading.Thread(target=_run_ocr_task, args=(task_id, image_bytes))
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/ocr/progress/<task_id>", methods=["GET"])
@require_auth
def api_ocr_progress(task_id: str):
    """轮询 OCR 异步任务进度

    返回:
        {progress, status, done, ok, amount, raw_text, image_path, engine, error}
    """
    task = get_ocr_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


# ------------------------------------------------------------------
# 应用启动
# ------------------------------------------------------------------

# 模块导入时自动初始化数据库（兼容 gunicorn 部署方式）
init_db()
ensure_admin_exists()
logger.info("数据库初始化完成")

# 检查 OCR 引擎状态
ocr_ok = init_ocr_engine()
logger.info("OCR 引擎%s", "就绪" if ocr_ok else "不可用（请安装 Tesseract）")

if __name__ == "__main__":
    app.run(debug=True, port=5000)
