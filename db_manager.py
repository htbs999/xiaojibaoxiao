"""
数据库管理模块 - MySQL 适配微信云托管（无备份）
"""

import os
import pymysql
from pymysql.cursors import DictCursor
from logger import get_logger

log = get_logger("db")


# ========== 从环境变量读取 MySQL 连接信息 ==========
def get_mysql_config():
    addr = os.environ.get("MYSQL_ADDRESS", "")
    if addr:
        parts = addr.split(":")
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 3306
    else:
        host = os.environ.get("MYSQL_HOST", "localhost")
        port = int(os.environ.get("MYSQL_PORT", 3306))

    return {
        "host": host,
        "port": port,
        "user": os.environ.get("MYSQL_USERNAME", os.environ.get("MYSQL_USER", "root")),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DB", "xiaojibaoxiao"),
        "charset": "utf8mb4",
    }


def get_connection():
    cfg = get_mysql_config()
    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg["charset"],
        cursorclass=DictCursor,
        autocommit=False,
    )
    return conn


# ========== 初始化表结构 ==========
def init_db():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(255) NOT NULL UNIQUE,
                    is_admin TINYINT DEFAULT 0,
                    created_at DATETIME DEFAULT NOW()
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cursor.execute("SHOW COLUMNS FROM users LIKE 'is_admin'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN is_admin TINYINT DEFAULT 0")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    date DATE NOT NULL,
                    person VARCHAR(255) NOT NULL,
                    category VARCHAR(255) NOT NULL,
                    amount DECIMAL(10,2) NOT NULL,
                    remark TEXT DEFAULT '',
                    image_path VARCHAR(512) DEFAULT '',
                    user_id INT,
                    created_at DATETIME DEFAULT NOW(),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cursor.execute("SHOW COLUMNS FROM expenses LIKE 'image_path'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE expenses ADD COLUMN image_path VARCHAR(512) DEFAULT ''")

        conn.commit()
        log.info("数据库表结构初始化完成")
    finally:
        conn.close()


# ========== 报销记录 CRUD ==========
def add_expense(date, person, category, amount, remark="", image_path="", user_id=None):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO expenses (date, person, category, amount, remark, image_path, user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (date, person, category, amount, remark, image_path, user_id)
            )
            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()


def update_expense(expense_id, date, person, category, amount, remark=""):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE expenses SET date=%s, person=%s, category=%s, amount=%s, remark=%s WHERE id=%s",
                (date, person, category, amount, remark, expense_id)
            )
            conn.commit()
    finally:
        conn.close()


def delete_expense(expense_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM expenses WHERE id=%s", (expense_id,))
            conn.commit()
    finally:
        conn.close()


def get_all_expenses():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM expenses ORDER BY date DESC, id DESC")
            return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


# ========== 用户管理 ==========
def get_or_create_user(username):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE username=%s", (username,))
            row = cursor.fetchone()
            if row is None:
                cursor.execute("INSERT INTO users (username) VALUES (%s)", (username,))
                conn.commit()
                cursor.execute("SELECT * FROM users WHERE username=%s", (username,))
                row = cursor.fetchone()
            return dict(row)
    finally:
        conn.close()


def get_all_users():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users ORDER BY username")
            return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_user_expenses_count(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS cnt FROM expenses WHERE user_id=%s", (user_id,))
            row = cursor.fetchone()
            return row["cnt"]
    finally:
        conn.close()


def delete_user(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM expenses WHERE user_id=%s", (user_id,))
            cursor.execute("DELETE FROM users WHERE id=%s", (user_id,))
            conn.commit()
    finally:
        conn.close()


def get_expenses_by_user(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM expenses WHERE user_id=%s ORDER BY date DESC, id DESC",
                (user_id,)
            )
            return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


# ========== 管理员 ==========
def is_admin_user(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT is_admin FROM users WHERE id=%s", (user_id,))
            row = cursor.fetchone()
            return bool(row and row["is_admin"])
    finally:
        conn.close()


def set_user_admin(user_id, is_admin=True):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET is_admin=%s WHERE id=%s", (int(is_admin), user_id))
            conn.commit()
    finally:
        conn.close()


def ensure_admin_exists():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE is_admin=1 LIMIT 1")
            admin = cursor.fetchone()
            if admin:
                return dict(admin)
            cursor.execute("SELECT * FROM users ORDER BY id ASC LIMIT 1")
            first = cursor.fetchone()
            if first:
                cursor.execute("UPDATE users SET is_admin=1 WHERE id=%s", (first["id"],))
                conn.commit()
                return dict(first)
            return None
    finally:
        conn.close()


def get_all_expenses_with_user():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT e.*, u.username 
                   FROM expenses e 
                   LEFT JOIN users u ON e.user_id = u.id 
                   ORDER BY e.date DESC, e.id DESC"""
            )
            return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()