# db.py
from __future__ import annotations
from contextlib import closing
from flask import current_app
import os
import MySQLdb, MySQLdb.cursors

# ---------------- Defaults & Config ----------------
DEFAULTS = {
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_USER": "root",
    "MYSQL_PASSWORD": "",
    "MYSQL_DB": "readweb",   
    "MYSQL_PORT": 3306,
    "MYSQL_CHARSET": "utf8mb4",
    "MYSQL_USE_UNICODE": True,
    "MYSQL_CONNECT_TIMEOUT": 10,
    "MYSQL_READ_TIMEOUT": 30,
    "MYSQL_WRITE_TIMEOUT": 30,
}

def apply_defaults(app):
    for k, v in DEFAULTS.items():
        app.config.setdefault(k, v)

def _cfg(key, default=None):
    try:
        return current_app.config.get(key, default)
    except Exception:
        return default

# ---------------- Core Connection ----------------
def get_db_connection():
    """
    สร้าง connection ใหม่ + ping(True) เพื่อ auto-reconnect
    หมายเหตุ: ผู้เรียกต้องปิด conn เอง หรือใช้ contextlib.closing(...)
    """
    conn = MySQLdb.connect(
        host=_cfg("MYSQL_HOST", DEFAULTS["MYSQL_HOST"]),
        user=_cfg("MYSQL_USER", DEFAULTS["MYSQL_USER"]),
        passwd=_cfg("MYSQL_PASSWORD", DEFAULTS["MYSQL_PASSWORD"]),
        db=_cfg("MYSQL_DB", DEFAULTS["MYSQL_DB"]),
        port=int(_cfg("MYSQL_PORT", DEFAULTS["MYSQL_PORT"])),
        charset=_cfg("MYSQL_CHARSET", DEFAULTS["MYSQL_CHARSET"]),
        use_unicode=_cfg("MYSQL_USE_UNICODE", DEFAULTS["MYSQL_USE_UNICODE"]),
        autocommit=True,
        connect_timeout=int(_cfg("MYSQL_CONNECT_TIMEOUT", DEFAULTS["MYSQL_CONNECT_TIMEOUT"])),
        read_timeout=int(_cfg("MYSQL_READ_TIMEOUT", DEFAULTS["MYSQL_READ_TIMEOUT"])),
        write_timeout=int(_cfg("MYSQL_WRITE_TIMEOUT", DEFAULTS["MYSQL_WRITE_TIMEOUT"])),
    )
    try:
        conn.ping(True)
    except Exception:
        pass
    return conn

def init_db(app=None, schema_path="schema.sql", run_schema_if_exists=True):
    """
    ใช้ใน app.py:
        from db import init_db
        init_db(app)
    - ใส่ค่า default ให้ app.config
    - ทดสอบเชื่อมต่อ
    - ถ้ามี schema.sql จะรันให้อัตโนมัติ
    """
    if app is not None:
        apply_defaults(app)

    with closing(MySQLdb.connect(
        host=(app.config["MYSQL_HOST"] if app else DEFAULTS["MYSQL_HOST"]),
        user=(app.config["MYSQL_USER"] if app else DEFAULTS["MYSQL_USER"]),
        passwd=(app.config["MYSQL_PASSWORD"] if app else DEFAULTS["MYSQL_PASSWORD"]),
        db=(app.config["MYSQL_DB"] if app else DEFAULTS["MYSQL_DB"]),
        port=int(app.config["MYSQL_PORT"] if app else DEFAULTS["MYSQL_PORT"]),
        charset=(app.config["MYSQL_CHARSET"] if app else DEFAULTS["MYSQL_CHARSET"]),
        use_unicode=(app.config["MYSQL_USE_UNICODE"] if app else DEFAULTS["MYSQL_USE_UNICODE"]),
        autocommit=True,
        connect_timeout=int(app.config.get("MYSQL_CONNECT_TIMEOUT", DEFAULTS["MYSQL_CONNECT_TIMEOUT"]) if app else DEFAULTS["MYSQL_CONNECT_TIMEOUT"]),
        read_timeout=int(app.config.get("MYSQL_READ_TIMEOUT", DEFAULTS["MYSQL_READ_TIMEOUT"]) if app else DEFAULTS["MYSQL_READ_TIMEOUT"]),
        write_timeout=int(app.config.get("MYSQL_WRITE_TIMEOUT", DEFAULTS["MYSQL_WRITE_TIMEOUT"]) if app else DEFAULTS["MYSQL_WRITE_TIMEOUT"]),
    )) as conn:
        try:
            conn.ping(True)
        except Exception:
            pass

        if run_schema_if_exists:
            base = app.root_path if app else os.getcwd()
            path = schema_path if os.path.isabs(schema_path) else os.path.join(base, schema_path)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    sql = f.read()
                statements = [s.strip() for s in sql.split(";") if s.strip()]
                with conn.cursor() as cur:
                    for stmt in statements:
                        cur.execute(stmt)
                conn.commit()
    return True

# ---------------- Back-compat shim: mysql ----------------
class _MySQLShim:
    """
    ให้ใช้งานแบบเดิมได้บางส่วน:
        from db import mysql
        conn = mysql.connection
        with conn.cursor(...) as cur: ...
    หมายเหตุ: จะคืน connection ใหม่ทุกครั้งที่อ่าน property
    """
    def init_app(self, app):  # เผื่อ code เก่าเรียก mysql.init_app(app)
        apply_defaults(app)

    @property
    def connection(self):
        # ผู้เรียกควรปิด conn เองหลังใช้งาน
        return get_db_connection()

# export ตัวแปร mysql เพื่อให้ไฟล์เก่า import ได้
mysql = _MySQLShim()

# ---------------- Helpers ----------------
def query_one(sql: str, params=None):
    """คืน 1 แถวแรกแบบ dict หรือ None"""
    with closing(get_db_connection()) as conn:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

def query_all(sql: str, params=None):
    """คืนหลายแถวแบบ list[dict] (อาจเป็นลิสต์ว่าง)"""
    with closing(get_db_connection()) as conn:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

def execute(sql: str, params=None):
    """รันคำสั่งเขียนข้อมูล; คืน (rowcount, lastrowid)"""
    with closing(get_db_connection()) as conn:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(sql, params or ())
            rowcount = cur.rowcount
            last_id = getattr(cur, "lastrowid", None)
        conn.commit()
    return rowcount, last_id
