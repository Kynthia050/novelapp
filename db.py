# db.py
from __future__ import annotations

import os
import re
from contextlib import closing
from urllib.parse import urlparse

from flask import current_app

import MySQLdb
import MySQLdb.cursors


# ---------------- Defaults ----------------
DEFAULTS = {
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_USER": "root",
    "MYSQL_PASSWORD": "",
    "MYSQL_DB": "readweb",
    "MYSQL_PORT": 3306,
    "MYSQL_CHARSET": "utf8mb4",
    "MYSQL_CONNECT_TIMEOUT": 10,
    "MYSQL_READ_TIMEOUT": 30,
    "MYSQL_WRITE_TIMEOUT": 30,
    # SSL (optional)
    "MYSQL_SSL_CA": "",
    "MYSQL_SSL_MODE": "",  # e.g. "REQUIRED" (ไม่บังคับใส่เพราะบางเวอร์ชันไม่รองรับ)
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

    # บางที URL อาจไม่ใช่ MySQL จริง ๆ (เช่น postgres) -> ปล่อยว่าง
    if p.scheme and "mysql" not in p.scheme.lower():
        return {}

    return {
        "MYSQL_HOST": host,
        "MYSQL_PORT": int(port),
        "MYSQL_USER": user,
        "MYSQL_PASSWORD": password,
        "MYSQL_DB": db,
    }


def apply_defaults(app):
    """
    ใส่ defaults ก่อน แล้ว override จาก env
    รองรับ Railway:
      - MYSQL_URL / DATABASE_URL
      - MYSQLHOST/MYSQLPORT/MYSQLUSER/MYSQLPASSWORD/MYSQLDATABASE
    รองรับชื่อทั่วไป:
      - MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB
    """
    for k, v in DEFAULTS.items():
        app.config.setdefault(k, v)

    # 1) URL (priority สูง)
    url = _env("MYSQL_URL", "MYSQL_PUBLIC_URL", "DATABASE_URL", default=None)
    if url:
        parsed = _parse_mysql_url(url)
        for k, v in parsed.items():
            if v not in (None, "", 0):
                app.config[k] = v

    # 2) key แยก (รองรับทั้งทั่วไปและแบบ Railway)
    app.config["MYSQL_HOST"] = _env("MYSQL_HOST", "MYSQLHOST", default=app.config["MYSQL_HOST"])
    app.config["MYSQL_PORT"] = int(_env("MYSQL_PORT", "MYSQLPORT", default=str(app.config["MYSQL_PORT"])))
    app.config["MYSQL_USER"] = _env("MYSQL_USER", "MYSQLUSER", default=app.config["MYSQL_USER"])
    app.config["MYSQL_PASSWORD"] = _env("MYSQL_PASSWORD", "MYSQLPASSWORD", default=app.config["MYSQL_PASSWORD"])
    app.config["MYSQL_DB"] = _env("MYSQL_DB", "MYSQLDATABASE", "MYSQLDB", default=app.config["MYSQL_DB"])

    # knobs
    app.config["MYSQL_CHARSET"] = _env("MYSQL_CHARSET", default=app.config["MYSQL_CHARSET"])
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
                "บน Railway ต้องตั้ง MYSQL_URL หรือ MYSQLHOST/MYSQLUSER/... ใน Variables ของ Web Service"
            )


def _cfg(key: str, default=None):
    """อ่านจาก current_app.config ถ้ามี ไม่งั้น fallback ไป env แล้วค่อย default"""
    try:
        if current_app:
            v = current_app.config.get(key, None)
            if v not in (None, ""):
                return v
    except Exception:
        pass

    # fallback จาก env ตามชื่อที่พบบ่อย
    if key == "MYSQL_HOST":
        return _env("MYSQL_HOST", "MYSQLHOST", default=default)
    if key == "MYSQL_PORT":
        return _env("MYSQL_PORT", "MYSQLPORT", default=default)
    if key == "MYSQL_USER":
        return _env("MYSQL_USER", "MYSQLUSER", default=default)
    if key == "MYSQL_PASSWORD":
        return _env("MYSQL_PASSWORD", "MYSQLPASSWORD", default=default)
    if key == "MYSQL_DB":
        return _env("MYSQL_DB", "MYSQLDATABASE", "MYSQLDB", default=default)

    return _env(key, default=default)


def _effective_config():
    """
    สรุป config สำหรับเชื่อมต่อ DB:
    - เริ่มจาก DEFAULTS
    - ถ้ามี current_app.config ก็ทับ
    - ถ้ามี MYSQL_URL/DATABASE_URL ก็ทับ (priority สูง)
    - ทับด้วย env แบบแยก (MYSQL_HOST/MYSQLHOST ฯลฯ)
    """
    cfg = dict(DEFAULTS)

    # จาก Flask config
    try:
        if current_app:
            for k in DEFAULTS.keys():
                v = current_app.config.get(k, None)
                if v not in (None, ""):
                    cfg[k] = v
    except Exception:
        pass

    # จาก URL
    url = _env("MYSQL_URL", "MYSQL_PUBLIC_URL", "DATABASE_URL", default=None)
    if url:
        parsed = _parse_mysql_url(url)
        for k, v in parsed.items():
            if v not in (None, "", 0):
                cfg[k] = v

    # จาก env แบบแยก
    cfg["MYSQL_HOST"] = _env("MYSQL_HOST", "MYSQLHOST", default=cfg["MYSQL_HOST"])
    cfg["MYSQL_PORT"] = int(_env("MYSQL_PORT", "MYSQLPORT", default=str(cfg["MYSQL_PORT"])))
    cfg["MYSQL_USER"] = _env("MYSQL_USER", "MYSQLUSER", default=cfg["MYSQL_USER"])
    cfg["MYSQL_PASSWORD"] = _env("MYSQL_PASSWORD", "MYSQLPASSWORD", default=cfg["MYSQL_PASSWORD"])
    cfg["MYSQL_DB"] = _env("MYSQL_DB", "MYSQLDATABASE", "MYSQLDB", default=cfg["MYSQL_DB"])

    cfg["MYSQL_CHARSET"] = _env("MYSQL_CHARSET", default=cfg["MYSQL_CHARSET"])
    cfg["MYSQL_CONNECT_TIMEOUT"] = int(_env("MYSQL_CONNECT_TIMEOUT", default=str(cfg["MYSQL_CONNECT_TIMEOUT"])))
    cfg["MYSQL_READ_TIMEOUT"] = int(_env("MYSQL_READ_TIMEOUT", default=str(cfg["MYSQL_READ_TIMEOUT"])))
    cfg["MYSQL_WRITE_TIMEOUT"] = int(_env("MYSQL_WRITE_TIMEOUT", default=str(cfg["MYSQL_WRITE_TIMEOUT"])))

    cfg["MYSQL_SSL_CA"] = _env("MYSQL_SSL_CA", default=cfg.get("MYSQL_SSL_CA", ""))
    cfg["MYSQL_SSL_MODE"] = _env("MYSQL_SSL_MODE", default=cfg.get("MYSQL_SSL_MODE", ""))

    return cfg


# ---------------- Core Connection ----------------
def get_db_connection():
    """
    (สไตล์เดียวกับโค้ดสั้นที่คุณให้มา)
    - ดึงค่าจาก ENV ก่อน (รองรับ Railway)
    - รองรับ URL: MYSQL_URL / DATABASE_URL
    - ใช้ DictCursor เป็นค่าเริ่มต้น
    """
    cfg = _effective_config()

    connect_kwargs = {
        "host": cfg["MYSQL_HOST"],
        "user": cfg["MYSQL_USER"],
        "passwd": cfg["MYSQL_PASSWORD"],  # ใช้ passwd ให้ชัวร์กับ mysqlclient
        "db": cfg["MYSQL_DB"],
        "port": int(cfg["MYSQL_PORT"]),
        "charset": cfg["MYSQL_CHARSET"],
        "cursorclass": MySQLdb.cursors.DictCursor,
        "autocommit": True,
        "connect_timeout": int(cfg["MYSQL_CONNECT_TIMEOUT"]),
        "read_timeout": int(cfg["MYSQL_READ_TIMEOUT"]),
        "write_timeout": int(cfg["MYSQL_WRITE_TIMEOUT"]),
    }

    # SSL (optional) — ใส่เฉพาะเมื่อมี CA
    if cfg.get("MYSQL_SSL_CA"):
        connect_kwargs["ssl"] = {"ca": cfg["MYSQL_SSL_CA"]}

    conn = MySQLdb.connect(**connect_kwargs)

    # auto-reconnect แบบนุ่ม ๆ
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
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()


def query_all(sql: str, params=None):
    with closing(get_db_connection()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()


def execute(sql: str, params=None):
    with closing(get_db_connection()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rowcount = cur.rowcount
            last_id = getattr(cur, "lastrowid", None)
        conn.commit()
    return rowcount, last_id


def active_user_where(cur, alias: str = "u"):
    """
    Return (sql, params) to filter active users via users.is_active.
    Uses enum marker if available, with fallback markers for bool/tinyint.
    """
    try:
        cur.execute("SHOW COLUMNS FROM users LIKE 'is_active'")
        row = cur.fetchone() or {}
    except Exception:
        return "1=1", []

    if not row:
        return "1=1", []

    col_type = str(row.get("Type") or "")
    matches = re.findall(r"'([^']*)'", col_type)
    active_marker = matches[0] if matches else ""

    markers = [
        active_marker,
        "active",
        "1",
        "true",
        "yes",
        "y",
        "บัญชีปกติ",
    ]

    seen = set()
    normalized = []
    for m in markers:
        if m is None:
            continue
        s = str(m).strip()
        if not s:
            continue
        s = s.lower()
        if s in seen:
            continue
        seen.add(s)
        normalized.append(s)

    placeholders = ", ".join(["%s"] * len(normalized))
    clause = f"LOWER(CAST({alias}.is_active AS CHAR)) IN ({placeholders})"
    return clause, normalized
