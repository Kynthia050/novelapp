# new_novel.py
from __future__ import annotations

from flask import (
    Blueprint, render_template, request, jsonify,
    current_app, session, redirect, url_for
)
from werkzeug.utils import secure_filename
from contextlib import closing
from pathlib import Path
from datetime import datetime
import MySQLdb  # สำหรับ conn.ping(True)

from db import get_db_connection

# ---------- CONFIG ----------
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
# จะอัปโหลดไว้ใต้ <project>/static/cover เหมือน edit_novel.py
COVER_SUBDIR = "cover"
# ---------------------------

# blueprint แสดงหน้า + API สร้างนิยายใหม่
new_novel_bp = Blueprint("new_novel", __name__, template_folder="templates")


# ---------- Utilities (ยึดแบบเดียวกับ edit_novel.py) ----------
def _conn_alive():
    """
    คืน connection ที่พร้อมใช้งานเสมอ:
    - สร้างจาก get_db_connection()
    - ping(True) เพื่อ auto-reconnect ถ้าหลุด
    """
    conn = get_db_connection()
    try:
        conn.ping(True)
    except Exception:
        # ถ้าผิดพลาด ให้ใช้ conn เดิมไป ระบบจะเด้งตอน execute เอง
        pass
    return conn


def allowed_image(filename: str, mimetype: str | None) -> bool:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        return False
    if mimetype and not mimetype.startswith("image/"):
        return False
    return True


def dictfetchone(cur):
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def dictfetchall(cur):
    rows = cur.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return rows
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _upload_dir() -> Path:
    """คืนโฟลเดอร์สำหรับเก็บปก: <app.static_folder>/cover"""
    static_folder = Path(current_app.static_folder)
    d = static_folder / COVER_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d
# -----------------------------------------------------


# =========================  PAGE: NEW NOVEL  =========================
@new_novel_bp.route("/novels/new", methods=["GET"])
def new_novel_form():
    """
    แสดงหน้า new_novel.html
    - เติม categories สำหรับ select
    - เติม username ในช่องนามปากกา (readonly)
    """
    username = session.get("username") or ""

    with closing(_conn_alive()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT cate_id, name FROM categories ORDER BY name")
            categories = dictfetchall(cur)

    return render_template(
        "new_novel.html",
        categories=categories,
        username=username,
    )


# =========================  API: CREATE NOVEL  =========================
@new_novel_bp.route("/api/novels", methods=["POST"])
def api_create_novel():
    """
    ใช้กับ fetch ใน new_novel.html:
      - รองรับทั้ง JSON และ multipart/form-data (มีไฟล์ปก)
      - สร้างแถวในตาราง novels
      - สร้าง tag ใน tags + map ใน novels_tags
      - คืน { ok: true, novels_id: <id> }
    """
    users_id = session.get("users_id")
    if not users_id:
        return jsonify(ok=False, error="กรุณเข้าสู่ระบบก่อนสร้างนิยาย"), 401

    content_type = (request.content_type or "").lower()
    is_multipart = content_type.startswith("multipart/form-data")

    title = ""
    synopsis = ""
    pen_name = session.get("username") or ""
    main_category = ""
    tags = []
    cover_file = None

    if is_multipart:
        form = request.form
        title = (form.get("title") or "").strip()
        synopsis = (form.get("synopsis") or "").strip()
        pen_name = (form.get("penName") or pen_name).strip()
        main_category = (form.get("mainCategory") or "").strip()

        raw_tags = form.get("tags") or "[]"
        try:
            import json
            tags = json.loads(raw_tags)
        except Exception:
            tags = []

        cover_file = request.files.get("cover")
    else:
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        synopsis = (data.get("synopsis") or "").strip()
        pen_name = (data.get("penName") or pen_name).strip()
        main_category = (data.get("mainCategory") or "").strip()
        tags = data.get("tags") or []
        cover_file = None  # ไม่มีไฟล์ใน JSON

    # ---- validation เบื้องต้น ----
    if not title:
        return jsonify(ok=False, error="กรุณากรอกชื่อเรื่อง"), 400
    if not main_category:
        return jsonify(ok=False, error="กรุณาเลือกหมวดหมู่"), 400

    # ตรวจไฟล์ภาพ ถ้ามี
    will_upload_cover = cover_file and cover_file.filename
    if will_upload_cover and not allowed_image(cover_file.filename, cover_file.mimetype):
        return jsonify(
            ok=False,
            error="ชนิดไฟล์ภาพไม่ถูกต้อง (รองรับ .jpg .jpeg .png .webp)",
        ), 400

    with closing(_conn_alive()) as conn:
        try:
            with conn.cursor() as cur:
                # ตรวจหมวดหมู่ว่ามีจริง
                cur.execute(
                    "SELECT cate_id FROM categories WHERE cate_id=%s",
                    (main_category,),
                )
                if not dictfetchone(cur):
                    return jsonify(ok=False, error="หมวดหมู่ไม่ถูกต้อง"), 400

                # จัดการไฟล์ปก (ถ้ามี)
                cover_filename = None
                if will_upload_cover:
                    upload_dir = _upload_dir()
                    fname = secure_filename(cover_file.filename)
                    stem = Path(fname).stem
                    ext = Path(fname).suffix.lower()
                    cover_filename = f"{stem}_{int(datetime.utcnow().timestamp())}{ext}"
                    cover_file.save(upload_dir / cover_filename)

                # INSERT novels (ให้ status ใช้ default 'แบบร่าง')
                cur.execute(
                    """
                    INSERT INTO novels
                        (title, description, users_id, cate_id, cover)
                    VALUES
                        (%s, %s, %s, %s, %s)
                    """,
                    (
                        title,
                        synopsis or None,
                        users_id,
                        main_category,
                        cover_filename,
                    ),
                )
                novels_id = getattr(cur, "lastrowid", None)

                # ---- TAGS ----
                # ทำความสะอาด + dedupe
                clean_tags = []
                seen = set()
                for t in tags or []:
                    s = (t or "").strip()
                    if not s:
                        continue
                    key = s.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    clean_tags.append(s)

                import json  # เผื่อด้านบนไม่สำเร็จ

                for name in clean_tags:
                    # ใช้ ON DUPLICATE KEY พร้อม slug = name
                    cur.execute(
                        """
                        INSERT INTO tags (name, slug)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE tag_id=LAST_INSERT_ID(tag_id)
                        """,
                        (name, name),
                    )
                    cur.execute("SELECT LAST_INSERT_ID() AS tag_id")
                    row = dictfetchone(cur)
                    tag_id = row["tag_id"]

                    # map novels_tags (unique (novels_id, tag_id))
                    cur.execute(
                        """
                        INSERT INTO novels_tags (novels_id, tag_id)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE created_at = created_at
                        """,
                        (novels_id, tag_id),
                    )

            conn.commit()

        except Exception as e:
            conn.rollback()
            current_app.logger.exception("สร้างนิยายใหม่ไม่สำเร็จ: %s", e)
            return jsonify(ok=False, error="บันทึกไม่สำเร็จ กรุณาลองใหม่อีกครั้ง"), 500

    return jsonify(ok=True, novels_id=novels_id), 200


# =========================  VIEW NOVEL REDIRECT  =========================
@new_novel_bp.route("/novels/<int:novels_id>", methods=["GET"])
def view_novel(novels_id: int):
    """
    endpoint นี้เอาไว้ใช้กับ
      viewUrlTemplate = "{{ url_for('new_novel.view_novel', novels_id=0) }}"

    ทำหน้าที่ redirect ไปหน้าแสดงนิยายตัวจริง
    ปัจจุบันใช้ path /novel/<id> (ตาม log ระบบเดิม)
    ถ้า endpoint จริงต่างออกไป สามารถแก้ตรงนี้ภายหลังได้
    """
    # ถ้าระบบเดิมมี blueprint ชื่อ 'novel' และ endpoint เช่น 'novelcover'
    # สามารถเปลี่ยนเป็น:
    #   return redirect(url_for("novel.novelcover", novels_id=novels_id))
    # ตอนนี้ใช้ path ตรง ๆ ไปก่อน
    return redirect(f"/novel/{novels_id}")
