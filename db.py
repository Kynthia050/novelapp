# db.py
from __future__ import annotations

import os
from contextlib import closing
from urllib.parse import urlparse

from flask import current_app

import MySQLdb, MySQLdb.cursors


# ---------------- Defaults ----------------
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
    # SSL (optional)
    "MYSQL_SSL_CA": "",
    "MYSQL_SSL_MODE": "",  # e.g. "REQUIRED"
}


# ---------------- Env helpers ----------------
def _env(*keys, default=None):
    """คืนค่า env ตัวแรกที่มีค่า"""
    for k in keys:
        v = os.environ.get(k)
        if v not in (None, ""):
            return v
    return default


def _parse_mysql_url(url: str):
    """
    รองรับรูปแบบ:
      mysql://user:pass@host:port/dbname
      mysql+pymysql://...
    """
    if not url:
        return {}

    p = urlparse(url)
    host = p.hostname or ""
    port = p.port or 3306
    user = p.username or ""
    password = p.password or ""
    db = (p.path or "").lstrip("/")
    return {
        "MYSQL_HOST": host,
        "MYSQL_PORT": int(port),
        "MYSQL_USER": user,
        "MYSQL_PASSWORD": password,
        "MYSQL_DB": db,
    }


def apply_defaults(app):
    """
    ใส่ default ก่อน แล้วค่อย override จาก env
    รองรับ Railway:
      - MYSQL_URL / DATABASE_URL
      - MYSQLHOST/MYSQLPORT/MYSQLUSER/MYSQLPASSWORD/MYSQLDATABASE
    รองรับชื่ออีกแบบ:
      - MYSQL_DB / MYSQLDATABASE / MYSQLDB
    """
    # defaults
    for k, v in DEFAULTS.items():
        app.config.setdefault(k, v)

    # 1) URL (priority สูง)
    url = _env("MYSQL_URL", "MYSQL_PUBLIC_URL", "DATABASE_URL", default=None)
    if url:
        parsed = _parse_mysql_url(url)
        for k, v in parsed.items():
            if v not in (None, "", 0):
                app.config[k] = v

    # 2) key แยก (รองรับทั้งแบบทั่วไปและแบบ Railway)
    app.config["MYSQL_HOST"] = _env("MYSQL_HOST", "MYSQLHOST", default=app.config["MYSQL_HOST"])
    app.config["MYSQL_PORT"] = int(_env("MYSQL_PORT", "MYSQLPORT", default=str(app.config["MYSQL_PORT"])))
    app.config["MYSQL_USER"] = _env("MYSQL_USER", "MYSQLUSER", default=app.config["MYSQL_USER"])
    app.config["MYSQL_PASSWORD"] = _env("MYSQL_PASSWORD", "MYSQLPASSWORD", default=app.config["MYSQL_PASSWORD"])

    # DB name: รองรับหลายชื่อ (Railway มักเป็น MYSQLDATABASE)
    app.config["MYSQL_DB"] = _env(
        "MYSQL_DB", "MYSQLDATABASE", "MYSQLDB",
        default=app.config["MYSQL_DB"]
    )

    # knobs
    app.config["MYSQL_CHARSET"] = _env("MYSQL_CHARSET", default=app.config["MYSQL_CHARSET"])

    # env อาจเป็น true/false หรือ 1/0
    v_unicode = _env("MYSQL_USE_UNICODE", default=None)
    if v_unicode is not None:
        app.config["MYSQL_USE_UNICODE"] = str(v_unicode).strip().lower() in ("1", "true", "yes", "y", "on")
    else:
        app.config["MYSQL_USE_UNICODE"] = bool(app.config["MYSQL_USE_UNICODE"])

    app.config["MYSQL_CONNECT_TIMEOUT"] = int(_env("MYSQL_CONNECT_TIMEOUT", default=str(app.config["MYSQL_CONNECT_TIMEOUT"])))
    app.config["MYSQL_READ_TIMEOUT"] = int(_env("MYSQL_READ_TIMEOUT", default=str(app.config["MYSQL_READ_TIMEOUT"])))
    app.config["MYSQL_WRITE_TIMEOUT"] = int(_env("MYSQL_WRITE_TIMEOUT", default=str(app.config["MYSQL_WRITE_TIMEOUT"])))

    # SSL (optional)
    app.config["MYSQL_SSL_CA"] = _env("MYSQL_SSL_CA", default=app.config.get("MYSQL_SSL_CA", ""))
    app.config["MYSQL_SSL_MODE"] = _env("MYSQL_SSL_MODE", default=app.config.get("MYSQL_SSL_MODE", ""))

    # แจ้งเตือนถ้ายังเป็น localhost (พบบ่อยบน Railway)
    if str(app.config.get("MYSQL_HOST", "")).strip() in ("127.0.0.1", "localhost"):
        if _env("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", default=None):
            print(
                "[WARNING] DB host ยังเป็น localhost (127.0.0.1). "
                "บน Railway ต้องตั้ง MYSQL_URL หรือ MYSQLHOST/MYSQLUSER/... ใน Web Service Variables"
            )


def _cfg(key, default=None):
    try:
        return current_app.config.get(key, default)
    except Exception:
        return default


# ---------------- Core Connection ----------------
def get_db_connection():
    """
    สร้าง connection ใหม่ + ping(True) เพื่อ auto-reconnect
    หมายเหตุ: ผู้เรียกควรปิด conn เอง หรือใช้ contextlib.closing(...)
    """
    connect_kwargs = dict(
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

    # SSL (optional)
    ssl_ca = _cfg("MYSQL_SSL_CA", DEFAULTS["MYSQL_SSL_CA"])
    ssl_mode = _cfg("MYSQL_SSL_MODE", DEFAULTS["MYSQL_SSL_MODE"])
    if ssl_ca:
        # mysqlclient ใช้พารามิเตอร์ ssl={"ca": "..."}
        connect_kwargs["ssl"] = {"ca": ssl_ca}
    # ssl_mode บางเวอร์ชันอาจไม่รองรับ; เลยไม่ใส่บังคับ

    conn = MySQLdb.connect(**connect_kwargs)

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

    - ใส่ default + override จาก ENV (Railway-friendly)
    - ไม่ทำให้แอปล้มตอนบูตถ้า DB ต่อไม่ได้
    - ถ้าจะให้เช็ก/รัน schema ตอนบูต ให้เปิด ENV:
        DB_STARTUP_CHECK=1
        DB_RUN_SCHEMA=1
    """
    if app is None:
        return True

    apply_defaults(app)

    startup_check = _env("DB_STARTUP_CHECK", default="0") == "1"
    run_schema = bool(run_schema_if_exists) and (_env("DB_RUN_SCHEMA", default="0") == "1")

    if not startup_check and not run_schema:
        return True

    try:
        with closing(get_db_connection()) as conn:
            if run_schema:
                base = app.root_path
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
    except Exception as e:
        print("[WARNING] init_db: connect/run schema skipped due to error:", repr(e))
        return False


# ---------------- Back-compat shim: mysql ----------------
class _MySQLShim:
    def init_app(self, app):
        apply_defaults(app)

    @property
    def connection(self):
        return get_db_connection()

mysql = _MySQLShim()


# ---------------- Helpers ----------------
def query_one(sql: str, params=None):
    with closing(get_db_connection()) as conn:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

def query_all(sql: str, params=None):
    with closing(get_db_connection()) as conn:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

def execute(sql: str, params=None):
    with closing(get_db_connection()) as conn:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(sql, params or ())
            rowcount = cur.rowcount
            last_id = getattr(cur, "lastrowid", None)
        conn.commit()
    return rowcount, last_id
