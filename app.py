"""
微信群报销管理工具 - Flask Web 版
支持多用户登录、文本解析、Excel/PDF导出、搜索/分页/统计
"""

import os
import sys
import uuid
from datetime import datetime
from functools import wraps
from threading import Thread

from flask import (
    Flask, request, redirect, url_for, render_template_string,
    session, jsonify, send_file
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_manager import (
    init_db, add_expense, update_expense, delete_expense,
    get_all_expenses, get_expenses_by_user,
    get_or_create_user, get_all_users,
    is_admin_user, get_all_expenses_with_user,
    get_connection
)
from text_parser import parse_expense_text
from excel_exporter import export_to_excel
from pdf_exporter import export_to_pdf
from config import resource_path
from logger import get_logger

log = get_logger("server")
app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

# 适配 HTTPS 环境的 session cookie 配置
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_HTTPONLY=True
)

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

init_db()
log.info("Flask 服务初始化完成")


# ========== 鉴权装饰器 ==========
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# ========== 页面路由 ==========
@app.route("/")
@login_required
def index():
    return serve_index()


@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return serve_login()


def serve_index():
    html_path = resource_path('index.html')
    if not os.path.exists(html_path):
        return f"index.html not found at {html_path}", 404
    with open(html_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return render_template_string(content, username=session.get('username', ''))


def serve_login():
    html_path = resource_path('login.html')
    if not os.path.exists(html_path):
        return f"login.html not found at {html_path}", 404
    with open(html_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return render_template_string(content, username=session.get('username', ''))


# ========== API：认证 ==========
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    if not username or len(username) > 20:
        return jsonify({"error": "请输入有效用户名（1-20字符）"}), 400
    user = get_or_create_user(username)
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return jsonify({"ok": True, "username": user["username"]})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/whoami")
@login_required
def api_whoami():
    return jsonify({
        "user_id": session["user_id"],
        "username": session["username"],
        "is_admin": is_admin_user(session["user_id"])
    })


# ========== API：报销数据 ==========
@app.route("/api/expenses")
@login_required
def api_expenses():
    if is_admin_user(session["user_id"]):
        rows = get_all_expenses_with_user()
    else:
        rows = get_expenses_by_user(session["user_id"])
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
        date=data["date"],
        person=data["person"],
        category=data["category"],
        amount=float(data["amount"]),
        remark=data.get("remark", ""),
        image_path=data.get("image_path", ""),
        user_id=session["user_id"]
    )
    return jsonify({"ok": True, "id": exp_id})


@app.route("/api/expenses/<int:exp_id>", methods=["PUT"])
@login_required
def api_update_expense(exp_id):
    data = request.get_json()
    update_expense(
        exp_id,
        data.get("date", ""),
        data.get("person", ""),
        data.get("category", ""),
        float(data.get("amount", 0)),
        data.get("remark", "")
    )
    return jsonify({"ok": True})


@app.route("/api/expenses/<int:exp_id>", methods=["DELETE"])
@login_required
def api_delete_expense(exp_id):
    delete_expense(exp_id)
    return jsonify({"ok": True})


# ========== API：文本解析 ==========
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
            person=r.get("person", session["username"]),
            category=r.get("category", "其他"),
            amount=r["amount"],
            remark=r.get("remark", ""),
            user_id=session["user_id"]
        )
        added.append(r)
    return jsonify({"ok": True, "count": len(added), "records": added})


# ========== API：OCR 识图 ==========
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
        "progress": 0,
        "status": "准备中...",
        "result": None,
        "done": False,
        "image_path": filename
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
        if r["success"] and r["amount"] is not None:
            resp["ok"] = True
            resp["amount"] = r["amount"]
            resp["engine"] = r.get("engine")
            resp["image_path"] = task.get("image_path", "")
            resp["raw_text"] = r.get("raw_text", "")
            resp["message"] = "识别成功，请确认后录入"
        elif r.get("success") and r.get("amount") is None:
            resp["ok"] = True
            resp["amount"] = None
            resp["engine"] = r.get("engine")
            resp["raw_text"] = r.get("raw_text", "")
            resp["message"] = "识别成功但未提取到金额"
        else:
            resp["ok"] = False
            resp["error"] = r.get("error", "识别失败")
            resp["raw_text"] = r.get("raw_text", "")

    return jsonify(resp)


# ========== API：导出 ==========
def _build_export_query():
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "").strip()
    person = (request.args.get("person") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()

    conn = get_connection()
    conditions = []
    params = []

    if not is_admin_user(session["user_id"]):
        conditions.append("e.user_id = ?")
        params.append(session["user_id"])

    if q:
        conditions.append("(e.person LIKE ? OR e.remark LIKE ? OR e.category LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])

    if category:
        conditions.append("e.category = ?")
        params.append(category)

    if person:
        conditions.append("e.person LIKE ?")
        params.append(f"%{person}%")

    if start_date:
        conditions.append("e.date >= ?")
        params.append(start_date)

    if end_date:
        conditions.append("e.date <= ?")
        params.append(end_date)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = conn.execute(
        f"SELECT e.*, u.username FROM expenses e "
        f"LEFT JOIN users u ON e.user_id = u.id "
        f"{where} ORDER BY e.date DESC, e.id DESC",
        params
    ).fetchall()
    conn.close()

    data = []
    for r in rows:
        d = dict(r)
        d["amount"] = float(d["amount"])
        data.append(d)
    return data


@app.route("/api/export")
@login_required
def api_export():
    fmt = (request.args.get("format") or "excel").strip().lower()
    if fmt not in ("excel", "pdf"):
        return jsonify({"error": "格式仅支持 excel 或 pdf"}), 400

    rows = _build_export_query()
    if not rows:
        return jsonify({"error": "没有可导出的数据"}), 400

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    username = session.get("username", "user")

    if fmt == "excel":
        output_path = export_to_excel(rows)
        return send_file(
            output_path,
            as_attachment=True,
            download_name=f"报销明细_{username}_{timestamp}.xlsx"
        )
    else:
        output_path = export_to_pdf(rows)
        return send_file(
            output_path,
            as_attachment=True,
            download_name=f"报销明细_{username}_{timestamp}.pdf"
        )


# ========== API：图片查看 ==========
@app.route("/api/image/<filename>")
@login_required
def api_image(filename):
    filepath = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "图片不存在"}), 404
    return send_file(filepath)


# ========== API：搜索 & 分页 ==========
@app.route("/api/expenses/search")
@login_required
def api_expenses_search():
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(5, int(request.args.get("per_page", 20))))

    all_data = _build_export_query()
    total = len(all_data)

    offset = (page - 1) * per_page
    data = all_data[offset:offset + per_page]

    total_amount = round(sum(d["amount"] for d in data), 2)

    return jsonify({
        "data": data,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, -(-total // per_page)),
        "total_amount": total_amount,
    })


# ========== API：统计 ==========
@app.route("/api/stats")
@login_required
def api_stats():
    conn = get_connection()
    is_admin = is_admin_user(session["user_id"])
    user_filter = "" if is_admin else "WHERE e.user_id = ?"
    params = [] if is_admin else [session["user_id"]]

    cat_rows = conn.execute(
        f"SELECT e.category, COUNT(*) as cnt, SUM(e.amount) as total "
        f"FROM expenses e {user_filter} GROUP BY e.category ORDER BY total DESC",
        params).fetchall()

    person_rows = conn.execute(
        f"SELECT e.person, COUNT(*) as cnt, SUM(e.amount) as total "
        f"FROM expenses e {user_filter} GROUP BY e.person ORDER BY total DESC",
        params).fetchall()

    total_row = conn.execute(
        f"SELECT COUNT(*) as cnt, COALESCE(SUM(e.amount), 0) as total "
        f"FROM expenses e {user_filter}", params).fetchone()

    conn.close()

    return jsonify({
        "total_count": total_row["cnt"],
        "total_amount": round(float(total_row["total"]), 2),
        "by_category": [{"category": r["category"], "count": r["cnt"],
                         "amount": round(float(r["total"]), 2)} for r in cat_rows],
        "by_person": [{"person": r["person"], "count": r["cnt"],
                       "amount": round(float(r["total"]), 2)} for r in person_rows],
    })


# ========== 启动 ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  报销管理 Web 版已启动")
    print(f"  监听端口: {port}\n")
    app.run(host="0.0.0.0", port=port, debug=True)