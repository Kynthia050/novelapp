from flask import Blueprint, render_template, url_for, request, jsonify, render_template_string, current_app
import os, uuid, json
import MySQLdb.cursors
from werkzeug.utils import secure_filename
from pathlib import Path
from db import get_db_connection, mysql

new_novel_bp = Blueprint('new_novel', __name__, template_folder='templates')

# ===== App config (no secret_key, no session) =====
ALLOWED_EXTS = {"jpg", "jpeg", "png", "webp"}

def _cover_dir() -> Path:
    """Return absolute path to static/cover directory and ensure it exists."""
    root = Path(current_app.root_path)
    path = root / "static" / "cover"
    path.mkdir(parents=True, exist_ok=True)
    return path

def dcur():
    return mysql.connection.cursor(MySQLdb.cursors.DictCursor)

# -------- user resolver (no session) --------
def get_active_user():
    """
    ไม่มี session: ใช้ X-User-Id ถ้ามี; ไม่งั้นเลือกผู้ใช้คนแรกจาก DB
    """
    uid = request.headers.get("X-User-Id")
    cur = dcur()
    if uid and str(uid).isdigit():
        cur.execute("SELECT users_id, username, display_name, role FROM users WHERE users_id=%s", (int(uid),))
        u = cur.fetchone()
        if u:
            cur.close()
            return u
    cur.execute("SELECT users_id, username, display_name, role FROM users ORDER BY users_id LIMIT 1")
    u = cur.fetchone()
    cur.close()
    return u or {"users_id": 1, "username": "guest", "display_name": "Guest", "role": "user"}

# -------- helpers --------
def _resolve_cate_id(value: str | None):
    """รับได้ทั้ง cate_id หรือชื่อหมวด คืน cate_id/None"""
    if not value:
        return None
    val = value.strip()
    cur = dcur()
    if val.isdigit():
        cur.execute("SELECT cate_id FROM categories WHERE cate_id=%s", (int(val),))
        row = cur.fetchone(); cur.close()
        return int(val) if row else None
    cur.execute("SELECT cate_id FROM categories WHERE name=%s LIMIT 1", (val,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT cate_id FROM categories WHERE name LIKE %s LIMIT 1", (f"%{val}%",))
        row = cur.fetchone()
    cur.close()
    return row["cate_id"] if row else None

def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTS

def _save_cover(novels_id: int, file_storage):
    if not file_storage or not file_storage.filename:
        return None
    if not _allowed(file_storage.filename):
        raise ValueError("ไฟล์รูปปกต้องเป็น .jpg .jpeg .png .webp")
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    new_name = f"{novels_id}_{uuid.uuid4().hex}.{ext}"
    safe_name = secure_filename(new_name)
    cover_dir = _cover_dir()
    file_storage.save((cover_dir / safe_name).as_posix())
    return f"cover/{safe_name}"  # เก็บเป็นพาธใต้ static/

def _slugify_for_tags(name: str) -> str:
    # ตามสคีมาที่ให้มา slug มักเท่ากับ name อยู่แล้ว → เก็บเหมือนกันได้เลย
    return (name or "").strip()

def _upsert_tag(name: str) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    slug = _slugify_for_tags(name)
    cur = dcur()
    cur.execute(
        """
        INSERT INTO tags (name, slug) VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE tag_id = LAST_INSERT_ID(tag_id)
        """,
        (name, slug),
    )
    tag_id = cur.lastrowid
    cur.close()
    return tag_id

def _parse_tags_field(raw):
    """
    รองรับ:
      - list อยู่แล้ว
      - JSON string ของ list (เช่น '["a","b"]' จาก FormData)
      - string คอมมาคั่น
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [t.strip() for t in raw if t and str(t).strip()]
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    s = str(raw).strip()
    if not s:
        return []
    # พยายาม parse JSON ก่อน
    try:
        j = json.loads(s)
        if isinstance(j, list):
            return [str(t).strip() for t in j if str(t).strip()]
    except Exception:
        pass
    # fallback: คอมมาคั่น
    return [t.strip() for t in s.split(",") if t.strip()]

# -------- pages --------
@new_novel_bp.get("/")
def index():
    return "<p>MYSHELF Server is running! <a href='/novels/new'>Create New Novel</a></p>"

@new_novel_bp.get("/novels/new")
def new_novel_form():
    user = get_active_user()
    cur = dcur()
    cur.execute("SELECT cate_id, name FROM categories ORDER BY name ASC")
    categories = cur.fetchall()
    cur.close()
    return render_template(
        "new_novel.html",
        categories=categories,
        username=user.get("username", "guest"),  # นามปากกาอ่านอย่างเดียว
    )

# ค้นหา categories (สำหรับ typeahead/datalist)
@new_novel_bp.get("/api/categories")
def api_categories():
    q = (request.args.get("q") or request.args.get("term") or "").strip()
    cur = dcur()
    if q:
        cur.execute("SELECT cate_id, name FROM categories WHERE name LIKE %s ORDER BY name ASC", (f"%{q}%",))
    else:
        cur.execute("SELECT cate_id, name FROM categories ORDER BY name ASC")
    rows = cur.fetchall()
    cur.close()
    return jsonify(rows)

# -------- create novel --------
@new_novel_bp.post("/api/novels")
def api_create_novel():
    """
    รองรับ 2 แบบ:
      - application/json      : ไม่แนบไฟล์
      - multipart/form-data   : แนบไฟล์ปกใน field 'cover'
    ฟิลด์: title, synopsis, mainCategory (id หรือชื่อ), tags (list/JSON string/คอมมาคั่น)
    """
    user = get_active_user()
    ct = (request.content_type or "").lower()

    if ct.startswith("multipart/form-data"):
        form = request.form
        title = (form.get("title") or "").strip()
        synopsis = (form.get("synopsis") or "").strip() or None
        mainCategory = (form.get("mainCategory") or "").strip()
        tags = _parse_tags_field(form.get("tags"))
        cover_file = request.files.get("cover")
    else:
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        synopsis = (data.get("synopsis") or "").strip() or None
        mainCategory = (data.get("mainCategory") or "").strip()
        tags = _parse_tags_field(data.get("tags"))
        cover_file = None

    if not title:
        return jsonify(ok=False, error="กรุณากรอกชื่อเรื่อง"), 400

    cate_id = _resolve_cate_id(mainCategory)

    cur = dcur()
    # status ตาม ENUM ในฐานข้อมูล: 'แบบร่าง' | 'เผยแพร่' | 'จบแล้ว'
    cur.execute(
        """
        INSERT INTO novels (title, description, status, users_id, cate_id, cover)
        VALUES (%s, %s, 'แบบร่าง', %s, %s, NULL)
        """,
        (title, synopsis, user["users_id"], cate_id),
    )
    novels_id = cur.lastrowid

    # เซฟปกถ้ามี
    try:
        if cover_file:
            rel_path = _save_cover(novels_id, cover_file)  # 'cover/xxx.jpg'
            if rel_path:
                cur.execute("UPDATE novels SET cover=%s WHERE novels_id=%s", (rel_path, novels_id))
    except ValueError as e:
        mysql.connection.rollback()
        cur.close()
        return jsonify(ok=False, error=str(e)), 400

    # บันทึกแท็ก
    for t in tags:
        tag_id = _upsert_tag(t)
        if tag_id:
            cur.execute(
                "INSERT IGNORE INTO novels_tags (novels_id, tag_id) VALUES (%s, %s)",
                (novels_id, tag_id),
            )

    mysql.connection.commit()
    cur.close()
    return jsonify(ok=True, novels_id=novels_id)

# -------- view result (simple) --------
@new_novel_bp.get("/novels/<int:novels_id>")
def view_novel(novels_id: int):
    cur = dcur()
    cur.execute(
        """
        SELECT n.*, u.username, c.name AS category
        FROM novels n
        LEFT JOIN users u ON u.users_id = n.users_id
        LEFT JOIN categories c ON c.cate_id = n.cate_id
        WHERE n.novels_id=%s
        """,
        (novels_id,),
    )
    n = cur.fetchone()
    if not n:
        cur.close()
        return render_template_string("<p style='padding:2rem'>Not found</p>"), 404

    cur.execute(
        """
        SELECT t.name
        FROM novels_tags nt
        JOIN tags t ON t.tag_id = nt.tag_id
        WHERE nt.novels_id=%s
        ORDER BY t.name
        """,
        (novels_id,),
    )
    tags = [r["name"] for r in cur.fetchall()]
    cur.close()

    cover_url = url_for("static", filename=n["cover"]) if n.get("cover") else None
    return render_template_string(
        """
        <!doctype html><meta charset="utf-8">
        <div style="max-width:900px;margin:2rem auto;font-family:system-ui;color:#222">
          <p><a href="{{ url_for('new_novel.new_novel_form') }}">← New Novel</a></p>
          <h1>{{ n.title }}</h1>
          <p>ผู้เขียน: <b>{{ n.username }}</b></p>
          <p>หมวดหมู่: {{ n.category or '-' }}</p>
          {% if cover_url %}<p><img src="{{ cover_url }}" style="max-width:260px;border-radius:12px"></p>{% endif %}
          <p><i>สถานะ:</i> {{ n.status }} | <i>สร้างเมื่อ:</i> {{ n.created_at }}</p>
          <h3>คำโปรย</h3><p style="white-space:pre-wrap">{{ n.description or '-' }}</p>
          <h3>แท็ก</h3><p>{{ ', '.join(tags) or '-' }}</p>
        </div>
        """,
        n=n, cover_url=cover_url
    )
