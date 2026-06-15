"""
微信群报销管理工具 - Flask Web 版
"""
import os
import sys
import uuid
from datetime import datetime
from functools import wraps
from threading import Thread

from flask import (
    Flask, request, redirect, url_for, render_template_string,
    session, jsonify, send_file, abort,
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
#  ★ 关键：secret_key 必须是固定字符串，绝不能 os.urandom
#  把它放进云托管「环境变量」里更安全，没有的话先用固定值
# ============================================================
FLASK_SECRET = os.environ.get("FLASK_SECRET_KEY", "WangChengExpenseApp-2024-Fixed-Key-DoNotChange")
if len(FLASK_SECRET) < 16:
    raise RuntimeError("FLASK_SECRET_KEY too short, need >=16 chars")

app = Flask(__name__)
app.secret_key = FLASK_SECRET

# 云托管前置代理（HTTPS卸载）→ 让 Flask 正确识别真实协议
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ============================================================
#  Cookie 配置 —— 这几项是 Flask 原生 cookie-session 能工作的命门
# ============================================================
# 云托管外层是 HTTPS（用户→代理），但 Flask 看到的可能是 HTTP，
# 所以 Secure 必须 False，否则浏览器拿到 Set-Cookie 也不存
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
# Lax 允许同站点导航（a标签/301/脚本location跳转都算"同站点"）
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_PATH"] = "/"
# 重要：不要让 cookie 绑到奇怪的域名上 → 留 None 让浏览器自己匹配当前域
app.config["SESSION_COOKIE_DOMAIN"] = None
# 防止 gunicorn fork 后 pid 影响 session（防御性设置）
os.environ.setdefault("WERKZEUG_RUN_MAIN", "true")

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

init_db()
log.info(f"Flask 服务初始化完成 [secret_key loaded: {'env' if os.environ.get('FLASK_SECRET_KEY') else 'fallback'}]")


# ========== 鉴权 ==========
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            # ★ 调试：打出来你会一眼看到底有没有 cookie / 验签是否失败
            log.warning(f"[AUTH] session empty, path={request.path}, cookies={list(request.cookies.keys())}")
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# ========== 页面 ==========
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
    html_path = resource_path("index.html")
    if not os.path.exists(html_path):
        return f"index.html not found at {html_path}", 404
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return render_template_string(content, username=session.get("username", ""))


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


# ========== /api/login —— 登录核心 ==========
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username or len(username) > 20:
        return jsonify({"error": "请输入有效用户名（1-20字符）"}), 400

    user = get_or_create_user(username)

    # 先清一遍，再写（防御：如果浏览器带着旧无效 cookie，这里覆盖它）
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session.modified = True

    log.info(f"[LOGIN] user={username} id={user['id']}  session_keys={list(session.keys())}")
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
        "is_admin": is_admin_user(session["user_id"]),
    })


# ===================== 以下是你原有的业务路由，完全不动 =====================

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
        date=data["date"], person=data["person"], category=data["category"],
        amount=float(data["amount"]), remark=data.get("remark", ""),
        image_path=data.get("image_path", ""), user_id=session["user_id"]
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
            person=r.get("person", session["username"]),
            category=r.get("category", "其他"),
            amount=r["amount"], remark=r.get("remark", ""),
            user_id=session["user_id"]
        )
        added.append(r)
    return jsonify({"ok": True, "count": len(added), "records": added})


# ---- OCR ----
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
    resp = {"progress": task["progress"], "status": task["status"], "done": task["done"]}
    if task["done"] and task["result"]:
        r = task["result"]
        if r["success"] and r["amount"] is not None:
            resp.update({"ok": True, "amount": r["amount"], "engine": r.get("engine"),
                         "image_path": task.get("image_path", ""),
                         "raw_text": r.get("raw_text", ""),
                         "message": "识别成功，请确认后录入"})
        elif r.get("success") and r.get("amount") is None:
            resp.update({"ok": True, "amount": None, "engine": r.get("engine"),
                         "raw_text": r.get("raw_text", ""),
                         "message": "识别成功但未提取到金额"})
        else:
            resp.update({"ok": False, "error": r.get("error", "识别失败"),
                         "raw_text": r.get("raw_text", "")})
    return jsonify(resp)


# ---- 导出 / 查询 / 统计（不动） ----
def _build_export_query():
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "").strip()
    person = (request.args.get("person") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    conn = get_connection()
    conditions, params = [], []
    if not is_admin_user(session["user_id"]):
        conditions.append("e.user_id = ?"); params.append(session["user_id"])
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
    uname = session.get("username", "user")
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
    is_admin = is_admin_user(session["user_id"])
    filt = "" if is_admin else "WHERE e.user_id = ?"
    par = [] if is_admin else [session["user_id"]]
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