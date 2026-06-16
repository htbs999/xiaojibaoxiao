"""
数据库管理模块 - SQLite 本地存储（无备份，适配云托管测试）
"""

import sqlite3
import os
from config import resource_path
from logger import get_logger

log = get_logger("db")

# 数据库文件放在项目根目录
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expense_data.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ocr_tasks (
            task_id TEXT PRIMARY KEY,
            progress INTEGER DEFAULT 0,
            status TEXT DEFAULT '排队中',
            done INTEGER DEFAULT 0,
            ok INTEGER DEFAULT 0,
            amount REAL,
            raw_text TEXT DEFAULT '',
            image_path TEXT DEFAULT '',
            engine TEXT DEFAULT '',
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # 兼容旧表
    task_cols = [c[1] for c in conn.execute("PRAGMA table_info(ocr_tasks)").fetchall()]
    for col in ("raw_text", "image_path", "engine", "error"):
        if col not in task_cols:
            conn.execute(f"ALTER TABLE ocr_tasks ADD COLUMN {col} TEXT DEFAULT ''")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # 兼容旧表：无 is_admin 列则添加
    user_cols = [c[1] for c in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "is_admin" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            person TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            remark TEXT DEFAULT '',
            image_path TEXT DEFAULT '',
            user_id INTEGER,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(expenses)").fetchall()]
    if "image_path" not in cols:
        conn.execute("ALTER TABLE expenses ADD COLUMN image_path TEXT DEFAULT ''")
    conn.commit()
    conn.close()
    log.info("数据库初始化完成（SQLite）")


# ------------------------------------------------------------------
# OCR 任务存储（SQLite 共享，兼容 gunicorn 多 worker）
# ------------------------------------------------------------------

def create_ocr_task(task_id: str):
    """创建 OCR 任务记录"""
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO ocr_tasks (task_id) VALUES (?)",
        (task_id,)
    )
    conn.commit()
    conn.close()


def update_ocr_task(task_id: str, **fields):
    """更新 OCR 任务字段"""
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [task_id]
    conn = get_connection()
    conn.execute(f"UPDATE ocr_tasks SET {sets} WHERE task_id=?", vals)
    conn.commit()
    conn.close()


def get_ocr_task(task_id: str) -> dict | None:
    """获取 OCR 任务状态"""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM ocr_tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["done"] = bool(d["done"])
    d["ok"] = bool(d["ok"])
    return d


def add_expense(date, person, category, amount, remark="", image_path="", user_id=None):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO expenses (date, person, category, amount, remark, image_path, user_id) VALUES (?,?,?,?,?,?,?)",
        (date, person, category, amount, remark, image_path, user_id)
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_expense(expense_id, date, person, category, amount, remark=""):
    conn = get_connection()
    conn.execute(
        "UPDATE expenses SET date=?, person=?, category=?, amount=?, remark=? WHERE id=?",
        (date, person, category, amount, remark, expense_id)
    )
    conn.commit()
    conn.close()


def delete_expense(expense_id):
    conn = get_connection()
    conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    conn.commit()
    conn.close()


def get_all_expenses():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM expenses ORDER BY date DESC, id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_or_create_user(username):
    """获取或创建用户，若用户名为 admin 则自动设为管理员"""
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if user:
        # 如果用户已存在但不是管理员，且用户名为 admin，则升级为管理员
        if username.lower() == "admin" and user["is_admin"] == 0:
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user["id"],))
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        conn.close()
        return dict(user)
    else:
        # 创建新用户，如果是 admin 则 is_admin=1，否则 is_admin=0
        is_admin = 1 if username.lower() == "admin" else 0
        cursor = conn.execute(
            "INSERT INTO users (username, is_admin) VALUES (?, ?)",
            (username, is_admin)
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        return {"id": user_id, "username": username, "is_admin": is_admin}


def get_all_users():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_expenses_count(user_id):
    conn = get_connection()
    row = conn.execute("SELECT COUNT(*) as cnt FROM expenses WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row["cnt"]


def delete_user(user_id):
    conn = get_connection()
    conn.execute("DELETE FROM expenses WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


def get_expenses_by_user(user_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM expenses WHERE user_id=? ORDER BY date DESC, id DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_expenses_with_user(user_id=None):
    """获取报销记录及用户名，可按 user_id 筛选

    Args:
        user_id: 若为 None 返回所有记录（管理员），否则只返回该用户的记录
    """
    conn = get_connection()
    if user_id is None:
        rows = conn.execute(
            """SELECT e.*, u.username
               FROM expenses e
               LEFT JOIN users u ON e.user_id = u.id
               ORDER BY e.date DESC, e.id DESC"""
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT e.*, u.username
               FROM expenses e
               LEFT JOIN users u ON e.user_id = u.id
               WHERE e.user_id = ?
               ORDER BY e.date DESC, e.id DESC""",
            (user_id,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_admin_user(user_id):
    conn = get_connection()
    row = conn.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return bool(row and row["is_admin"])


def set_user_admin(user_id, is_admin=True):
    conn = get_connection()
    conn.execute("UPDATE users SET is_admin=? WHERE id=?", (int(is_admin), user_id))
    conn.commit()
    conn.close()


def ensure_admin_exists():
    conn = get_connection()
    admin = conn.execute("SELECT * FROM users WHERE is_admin=1 LIMIT 1").fetchone()
    if admin:
        conn.close()
        return dict(admin)
    first = conn.execute("SELECT * FROM users ORDER BY id ASC LIMIT 1").fetchone()
    if first:
        conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (first["id"],))
        conn.commit()
    conn.close()
    if first:
        return dict(first)
    return None


def get_all_expenses_with_user():
    conn = get_connection()
    rows = conn.execute(
        """SELECT e.*, u.username 
           FROM expenses e 
           LEFT JOIN users u ON e.user_id = u.id 
           ORDER BY e.date DESC, e.id DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]