"""
数据库管理模块 - MySQL 适配微信云托管
"""

import os
import sys
import time
from datetime import datetime

import pymysql
from pymysql.cursors import DictCursor

from config import BACKUP_DIR
from logger import get_logger

log = get_logger("db")


# ========== 从环境变量读取 MySQL 连接信息 ==========
def get_mysql_config():
    """从环境变量获取 MySQL 配置，兼容微信云托管标准变量名"""
    # 优先使用 MYSQL_ADDRESS（格式：host:port）
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
        autocommit=False,  # 手动提交
    )
    return conn


# ========== 初始化表结构 ==========
def init_db():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 创建 users 表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(255) NOT NULL UNIQUE,
                    is_admin TINYINT DEFAULT 0,
                    created_at DATETIME DEFAULT NOW()
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # 兼容旧表：添加 is_admin 列（如果不存在）
            cursor.execute("SHOW COLUMNS FROM users LIKE 'is_admin'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN is_admin TINYINT DEFAULT 0")

            # 创建 expenses 表
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
            # 兼容旧表：添加 image_path 列（如果不存在）
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
            new_id = cursor.lastrowid
            return new_id
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
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
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
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
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
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
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
    """确保至少有一个管理员用户，返回管理员用户信息。如果没有任何用户则返回 None。"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE is_admin=1 LIMIT 1")
            admin = cursor.fetchone()
            if admin:
                return dict(admin)
            # 没有任何管理员：将第一个用户设为管理员
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
    """返回所有记录，附带用户名和用户ID（管理员专用）"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT e.*, u.username 
                   FROM expenses e 
                   LEFT JOIN users u ON e.user_id = u.id 
                   ORDER BY e.date DESC, e.id DESC"""
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()


# ========== 备份（MySQL 版本） ==========
def backup_db(auto=False):
    """
    备份数据库：使用 mysqldump 导出 SQL 文件。
    注意：需要系统安装 mysqldump 命令，云托管环境可能不支持。
    如果不可用，可降级为记录日志或跳过。
    """
    cfg = get_mysql_config()
    ts = time.strftime("%Y%m%d_%H%M%S")
    tag = "auto" if auto else "manual"
    filename = f"expense_{ts}_{tag}.sql"
    filepath = os.path.join(BACKUP_DIR, filename)

    # 尝试使用 mysqldump
    dump_cmd = (
        f"mysqldump -h {cfg['host']} -P {cfg['port']} "
        f"-u {cfg['user']} -p'{cfg['password']}' "
        f"{cfg['database']} > {filepath}"
    )
    ret = os.system(dump_cmd)
    if ret == 0:
        log.info(f"{'自动' if auto else '手动'}备份完成：{filepath}")
        return filepath
    else:
        log.warning("mysqldump 不可用或备份失败，请手动备份数据库")
        return None


def auto_backup():
    """启动时自动备份（如果距离上次自动备份超过 6 小时）"""
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR, exist_ok=True)

    auto_files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("expense_") and "_auto.sql" in f],
        reverse=True
    )
    if auto_files:
        try:
            # 文件名格式：expense_20260101_120000_auto.sql
            last_ts = auto_files[0].split("_", 1)[1].rsplit("_", 1)[0]
            last_dt = datetime.strptime(last_ts, "%Y%m%d_%H%M%S")
            if (datetime.now() - last_dt).total_seconds() < 6 * 3600:
                log.debug("6 小时内已有自动备份，跳过")
                return None
        except (ValueError, IndexError):
            pass
    return backup_db(auto=True)


def list_backups():
    """返回备份文件列表（.sql 文件），按时间倒序"""
    if not os.path.exists(BACKUP_DIR):
        return []
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".sql")],
        reverse=True
    )
    result = []
    for f in files:
        fp = os.path.join(BACKUP_DIR, f)
        size_kb = os.path.getsize(fp) // 1024
        result.append({"filename": f, "path": fp, "size_kb": size_kb})
    return result


def restore_backup(backup_path):
    """
    从备份文件恢复数据库（执行 SQL 文件）。
    需要系统安装 mysql 客户端命令。
    """
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"备份文件不存在：{backup_path}")

    cfg = get_mysql_config()
    restore_cmd = (
        f"mysql -h {cfg['host']} -P {cfg['port']} "
        f"-u {cfg['user']} -p'{cfg['password']}' "
        f"{cfg['database']} < {backup_path}"
    )
    ret = os.system(restore_cmd)
    if ret != 0:
        raise RuntimeError("数据库恢复失败，请检查 mysql 客户端是否可用")
    log.info(f"数据库已从备份恢复：{backup_path}")