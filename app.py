"""
微信群报销管理工具 - Flask Web 版 (Token 鉴权)
"""
import os
import sys
import uuid
from datetime import datetime, timedelta
from functools import wraps
from threading import Thread

import jwt
from flask import (
    Flask, request, redirect, url_for, render_template_string,
    session, jsonify, send_file, abort, g
)
from werkzeug.middleware.proxy_fix import ProxyFix

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_manager import (
    init_db, add_expense, update_expense, delete_expense,
    get_all_expenses, get_expenses_by_user,
    get_or_create_user, get_all_users,
    is_admin_user, get_all_expenses_with_user,
    get_connection,
)
from text_parser import parse_expense_text
from excel_exporter import export_to_excel
from pdf_exporter import export_to_pdf
from config import resource_path
from logger import get_logger

log = get_logger("server")

# ============================================================
#  JWT 密钥配置（务必更换为你的随机密钥）
# ============================================================
JWT_SECRET = os.environ.get("JWT_SECRET_KEY", "WangChengExpenseApp-JWT-Secret-2024!@#")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 168  # 7天

# ============================================================
#  Flask 基础配置
# ============================================================
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "fallback-secret-do-not-use-in-production")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Cookie 相关配置不再需要，因为我们不再使用 Session
# 但保留以防万一（不影响 Token 鉴权）
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_PATH"] = "/"
app.config["SESSION_COOKIE_DOMAIN"] = None

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

init_db()
log.info("Flask 服务初始化完成 [Token 鉴权模式]")


# ========== JWT 工具函数 ==========
def create_token(user_id, username):
    """生成 JWT Token"""
    payload = {
        'user_id': user_id,
        'username': username,
        'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS),
        'iat': datetime.utcnow()
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token

def verify_token(token):
    """验证 Token，成功返回 payload，失败返回 None"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        log.warning("Token 已过期")
        return None
    except jwt.InvalidTokenError as e:
        log.warning(f"Token 无效: {e}")
        return None


# ========== 鉴权装饰器（同时支持 Token 和 Session，便于过渡） ==========
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None

        # 1. 网页端：从 Cookie 中读取 Token
        token = request.cookies.get('token')
        if token:
            payload = verify_token(token)
            if payload:
                g.user_id = payload['user_id']
                g.username = payload['username']
                return f(*args, **kwargs)

        # 2. 小程序端/API：从 Authorization 头读取 Token
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            payload = verify_token(token)
            if payload:
                g.user_id = payload['user_id']
                g.username = payload['username']
                return f(*args, **kwargs)

        # 3. Session 回退（如果有的话，可以保留兼容旧代码）
        if "user_id" in session:
            g.user_id = session["user_id"]
            g.username = session.get("username", "")
            return f(*args, **kwargs)

        # 全部未通过
        log.warning(f"[AUTH] 未登录请求 path={request.path}")
        if request.path.startswith("/api/"):
            return jsonify({"error": "未登录"}), 401
        return redirect(url_for("login_page"))

    return decorated


# ========== 页面路由（保持不变） ==========
@app.route("/")
@login_required
def index():
    return serve_index()

@app.route("/login")
def login_page():
    # 不再检查 Session，因为用户可能通过 Token 登录后直接访问首页
    return serve_login()

def serve_index():
    html_path = resource_path("index.html")
    if not os.path.exists(html_path):
        return f"index.html not found at {html_path}", 404
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return render_template_string(content, username=g.get("username", ""))

def serve_login():
    html_path = resource_path("login.html")
    if not os.path.exists(html_path):
        return f"login.html not found at {html_path}", 404
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return render_template_string(content)

@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口不存在"}), 404
    return "<h1>页面未找到</h1><p>请检查网址是否正确</p>", 404


# ========== 登录/登出（Token 核心） ==========
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username or len(username) > 20:
        return jsonify({"error": "请输入有效用户名（1-20字符）"}), 400

    user = get_or_create_user(username)
    token = create_token(user["id"], user["username"])

    resp = jsonify({
        "ok": True,
        "token": token,
        "username": user["username"]
    })

    # ★ 关键：同时把 Token 写入 HttpOnly Cookie，网页端会自动携带
    resp.set_cookie(
        'token',             # Cookie 名称
        token,
        max_age=timedelta(hours=JWT_EXPIRATION_HOURS),
        path='/',
        httponly=True,       # JS 无法读取，防 XSS
        samesite='Lax'       # 允许在顶级导航（跳转）时发送
        # secure=True        # 如果网站是 HTTPS 访问，请取消这行注释
    )

    log.info(f"[LOGIN] user={username} id={user['id']} token+cookie issued")
    return resp

@app.route("/api/whoami")
@login_required
def api_whoami():
    return jsonify({
        "user_id": g.user_id,
        "username": g.username,
        "is_admin": is_admin_user(g.user_id),
    })

# ===================== 以下业务路由（仅将 session 替换为 g） =====================

@app.route("/api/expenses")
@login_required
def api_expenses():
    if is_admin_user(g.user_id):
        rows = get_all_expenses_with_user()
    else:
        rows = get_expenses_by_user(g.user_id)
    for r in rows:
        r["amount"] = float(r["amount"])
    return jsonify(rows)


@app.route("/api/expenses", methods=["POST"])
@login_required
def api_add_expense():
    data = request.get_json()
    required = ["date", "person", "category", "amount"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"缺少字段: {', '.join(missing)}"}), 400
    exp_id = add_expense(
        date=data["date"], person=data["person"], category=data["category"],
        amount=float(data["amount"]), remark=data.get("remark", ""),
        image_path=data.get("image_path", ""), user_id=g.user_id
    )
    return jsonify({"ok": True, "id": exp_id})


@app.route("/api/expenses/<int:exp_id>", methods=["PUT"])
@login_required
def api_update_expense(exp_id):
    data = request.get_json()
    update_expense(
        exp_id, data.get("date", ""), data.get("person", ""),
        data.get("category", ""), float(data.get("amount", 0)),
        data.get("remark", "")
    )
    return jsonify({"ok": True})


@app.route("/api/expenses/<int:exp_id>", methods=["DELETE"])
@login_required
def api_delete_expense(exp_id):
    delete_expense(exp_id)
    return jsonify({"ok": True})


@app.route("/api/parse_text", methods=["POST"])
@login_required
def api_parse_text():
    data = request.get_json()
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "请输入报账文本"}), 400
    records = parse_expense_text(text)
    added = []
    for r in records:
        add_expense(
            date=r.get("date", datetime.now().strftime("%Y-%m-%d")),
            person=r.get("person", g.username),
            category=r.get("category", "其他"),
            amount=r["amount"], remark=r.get("remark", ""),
            user_id=g.user_id
        )
        added.append(r)
    return jsonify({"ok": True, "count": len(added), "records": added})


# ---- OCR（保持不变，但注意 uploadFile 也需要手动带 Token） ----
_ocr_tasks = {}

@app.route("/api/ocr", methods=["POST"])
@login_required
def api_ocr():
    from ocr_handler import recognize_amount_from_image

    if "image" not in request.files:
        return jsonify({"error": "请上传图片"}), 400
    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "请选择图片"}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"):
        return jsonify({"error": "仅支持 JPG/PNG/BMP/GIF/WEBP"}), 400

    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    file.save(filepath)

    task_id = uuid.uuid4().hex[:12]
    _ocr_tasks[task_id] = {
        "progress": 0, "status": "准备中...",
        "result": None, "done": False, "image_path": filename,
    }

    def run_ocr():
        try:
            def on_progress(pct, msg):
                _ocr_tasks[task_id]["progress"] = pct
                _ocr_tasks[task_id]["status"] = msg
            result = recognize_amount_from_image(filepath, progress_callback=on_progress)
            _ocr_tasks[task_id]["result"] = result
            _ocr_tasks[task_id]["done"] = True
            _ocr_tasks[task_id]["progress"] = 100
            _ocr_tasks[task_id]["status"] = "完成"
        except Exception as e:
            _ocr_tasks[task_id]["done"] = True
            _ocr_tasks[task_id]["result"] = {"success": False, "error": str(e)}
            _ocr_tasks[task_id]["status"] = "失败"
    Thread(target=run_ocr, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/ocr/progress/<task_id>")
@login_required
def api_ocr_progress(task_id):
    task = _ocr_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    resp = {
        "progress": task["progress"],
        "status": task["status"],
        "done": task["done"],
    }

    if task["done"] and task["result"]:
        r = task["result"]
        if r["success"]:
            resp["ok"] = True
            resp["amount"] = r.get("amount")
            resp["engine"] = r.get("engine")
            resp["image_path"] = task.get("image_path", "")
            resp["raw_text"] = r.get("raw_text", "")
            if r.get("amount") is not None:
                resp["message"] = "识别成功，请确认后录入"
            else:
                resp["message"] = "未识别到金额，请手动输入"
        else:
            resp["ok"] = False
            resp["error"] = r.get("error", "识别失败")
            resp["raw_text"] = r.get("raw_text", "")

    return jsonify(resp)


# ---- 导出 / 查询 / 统计（仅将 session 替换为 g） ----
def _build_export_query():
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "").strip()
    person = (request.args.get("person") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    conn = get_connection()
    conditions, params = [], []
    if not is_admin_user(g.user_id):
        conditions.append("e.user_id = ?"); params.append(g.user_id)
    if q:
        conditions.append("(e.person LIKE ? OR e.remark LIKE ? OR e.category LIKE ?)")
        like = f"%{q}%"; params.extend([like, like, like])
    if category: conditions.append("e.category = ?"); params.append(category)
    if person: conditions.append("e.person LIKE ?"); params.append(f"%{person}%")
    if start_date: conditions.append("e.date >= ?"); params.append(start_date)
    if end_date: conditions.append("e.date <= ?"); params.append(end_date)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = conn.execute(
        f"SELECT e.*, u.username FROM expenses e "
        f"LEFT JOIN users u ON e.user_id = u.id "
        f"{where} ORDER BY e.date DESC, e.id DESC", params
    ).fetchall()
    conn.close()
    return [{**dict(r), "amount": float(r["amount"])} for r in rows]


@app.route("/api/export")
@login_required
def api_export():
    fmt = (request.args.get("format") or "excel").strip().lower()
    if fmt not in ("excel", "pdf"):
        return jsonify({"error": "格式仅支持 excel 或 pdf"}), 400
    rows = _build_export_query()
    if not rows: return jsonify({"error": "没有可导出的数据"}), 400
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uname = g.get("username", "user")
    if fmt == "excel":
        p = export_to_excel(rows)
        return send_file(p, as_attachment=True, download_name=f"报销明细_{uname}_{ts}.xlsx")
    p = export_to_pdf(rows)
    return send_file(p, as_attachment=True, download_name=f"报销明细_{uname}_{ts}.pdf")


@app.route("/api/image/<filename>")
@login_required
def api_image(filename):
    fp = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(fp): return jsonify({"error": "图片不存在"}), 404
    return send_file(fp)


@app.route("/api/expenses/search")
@login_required
def api_expenses_search():
    page = max(1, int(request.args.get("page", 1)))
    pp = min(100, max(5, int(request.args.get("per_page", 20))))
    all_data = _build_export_query()
    total = len(all_data)
    slice_ = all_data[(page-1)*pp : page*pp]
    return jsonify({
        "data": slice_, "total": total, "page": page, "per_page": pp,
        "total_pages": max(1, -(-total//pp)),
        "total_amount": round(sum(d["amount"] for d in slice_), 2),
    })


@app.route("/api/stats")
@login_required
def api_stats():
    conn = get_connection()
    is_admin = is_admin_user(g.user_id)
    filt = "" if is_admin else "WHERE e.user_id = ?"
    par = [] if is_admin else [g.user_id]
    cat = conn.execute(
        f"SELECT e.category,COUNT(*) cnt,SUM(e.amount) total FROM expenses e {filt} GROUP BY e.category ORDER BY total DESC", par
    ).fetchall()
    per = conn.execute(
        f"SELECT e.person,COUNT(*) cnt,SUM(e.amount) total FROM expenses e {filt} GROUP BY e.person ORDER BY total DESC", par
    ).fetchall()
    tot = conn.execute(f"SELECT COUNT(*) cnt,COALESCE(SUM(e.amount),0) total FROM expenses e {filt}", par).fetchone()
    conn.close()
    return jsonify({
        "total_count": tot["cnt"], "total_amount": round(float(tot["total"]), 2),
        "by_category": [{"category":r["category"],"count":r["cnt"],"amount":round(float(r["total"]),2)} for r in cat],
        "by_person": [{"person":r["person"],"count":r["cnt"],"amount":round(float(r["total"]),2)} for r in per],
    })


# ========== 启动 ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)