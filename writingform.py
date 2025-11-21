from __future__ import annotations

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, abort, current_app, jsonify, flash
)
from werkzeug.utils import secure_filename
from datetime import datetime
from contextlib import closing
from pathlib import Path
import MySQLdb  # สำหรับ conn.ping(True) ถ้าใช้ MySQLdb backend

from db import get_db_connection
from auth import roles_required

# ---------- CONFIG ----------
CHAPTER_IMAGE_SUBDIR = "chapter_images"  # รูปที่แทรกในเนื้อหาตอนจะเก็บที่ /static/chapter_images
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Blueprint สำหรับหน้าเขียน/แก้ไขตอน
writing_bp = Blueprint("writing", __name__)


# ---------- DB Utilities ----------

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
        # ถ้าพัง เดี๋ยวตอน execute จะ error เอง
        pass
    return conn


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


def _novel_or_404(conn, novels_id: int):
    """ดึงนิยาย ถ้าไม่มีให้ 404"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM novels WHERE novels_id=%s", (novels_id,))
        novel = dictfetchone(cur)
    if not novel:
        abort(404)
    return novel


def _image_upload_dir() -> Path:
    """
    คืนโฟลเดอร์สำหรับเก็บรูปในเนื้อหา:
    <app.static_folder>/chapter_images
    """
    static_folder = Path(current_app.static_folder)
    d = static_folder / CHAPTER_IMAGE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _allowed_image(filename: str, mimetype: str | None) -> bool:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        return False
    if mimetype and not mimetype.startswith("image/"):
        return False
    return True


# =========================  PAGE: เขียน/แก้ไขตอน  =========================

@writing_bp.route("/<int:novels_id>", methods=["GET"])
@roles_required("user")
def writing_form(novels_id: int):
    """
    แสดงหน้าเขียน / แก้ไขตอน

    - ถ้ามี query string ?chapter_id=<id> = โหมดแก้ไขตอน
    - ถ้าไม่มี chapter_id = โหมดสร้างตอนใหม่ (suggested_part = MAX(chapter_no)+1)
    """
    chapter_id = request.args.get("chapter_id", type=int)

    with closing(_conn_alive()) as conn:
        novel = _novel_or_404(conn, novels_id)

        chapter = None
        suggested_part = 1

        with conn.cursor() as cur:
            if chapter_id:
                # ดึงข้อมูลตอนที่ต้องการแก้ไข
                cur.execute(
                    """
                    SELECT chapters_id, novels_id, title, content_html, chapter_no
                      FROM chapters
                     WHERE chapters_id=%s AND novels_id=%s
                    """,
                    (chapter_id, novels_id),
                )
                chapter = dictfetchone(cur)
                if not chapter:
                    abort(404)

                suggested_part = chapter.get("chapter_no") or 1
            else:
                # ตอนใหม่ → ให้ลำดับตอน = MAX(chapter_no)+1
                cur.execute(
                    """
                    SELECT COALESCE(MAX(chapter_no), 0) + 1 AS next_no
                      FROM chapters
                     WHERE novels_id=%s
                    """,
                    (novels_id,),
                )
                row = dictfetchone(cur) or {}
                suggested_part = row.get("next_no") or 1

    # ตัวแปรที่ writingform.html ใช้:
    # - novel        → ชื่อเรื่อง ฯลฯ
    # - novels_id    → ใช้ใน hidden + config JS
    # - chapter      → None (ตอนใหม่) หรือ dict ของตอน (แก้ไข)
    # - suggested_part → ตั้งค่าลำดับตอนเริ่มต้นใน input#part
    return render_template(
        "writingform.html",
        novel=novel,
        novels_id=novels_id,
        chapter=chapter,
        suggested_part=suggested_part,
    )


# =========================  SAVE / AUTOSAVE  =========================

@writing_bp.route("/save", methods=["POST"])
@roles_required("user")
def save_chapter():
    """
    ใช้ได้ทั้ง
    - autosave จาก JS (fetch + Accept: application/json)
    - กดปุ่ม "บันทึก/อัปเดต" จากฟอร์มปกติ

    ฟิลด์ที่คาดหวังจาก writingform.html:
      - novels_id (required)
      - chapter_id (optional – ใส่เมื่อแก้ไข)
      - part      (เลขลำดับตอน → chapter_no)
      - epName    (ชื่อหัวตอน → title)
      - content_html (เนื้อหา HTML จาก Quill)
    """
    accept = (request.headers.get("Accept") or "").lower()
    is_autosave = "application/json" in accept  # autosave จะใส่ Accept: application/json

    def _json_resp(payload, status=200):
        return jsonify(payload), status

    # --- ดึงและตรวจค่าพื้นฐาน ---
    try:
        novels_id = int(request.form.get("novels_id", "0"))
    except ValueError:
        novels_id = 0

    if not novels_id:
        if is_autosave:
            return _json_resp({"success": False, "error": "novels_id required"}, 400)
        abort(400, description="novels_id required")

    chapter_id_raw = request.form.get("chapter_id")
    chapter_id = None
    if chapter_id_raw:
        try:
            chapter_id = int(chapter_id_raw)
        except ValueError:
            chapter_id = None

    part_raw = request.form.get("part") or ""
    try:
        chapter_no = int(part_raw)
    except ValueError:
        chapter_no = 0

    title = (request.form.get("epName") or "").strip()
    content_html = (request.form.get("content_html") or "").strip()

    # --- validation ฝั่ง server สำหรับ submit ปกติ (ไม่ใช่ autosave) ---
    if not is_autosave:
        if chapter_no < 1:
            flash("กรุณากรอกลำดับตอนที่ถูกต้อง", "error")
            return redirect(
                url_for("writing.writing_form", novels_id=novels_id, chapter_id=chapter_id or None)
            )

        # ฝั่ง JS เช็กแล้วว่า ถ้าไม่มีตัวอักษรและไม่มีรูปให้เตือน
        if not content_html.strip():
            flash("กรุณากรอกเนื้อเรื่อง", "error")
            return redirect(
                url_for("writing.writing_form", novels_id=novels_id, chapter_id=chapter_id or None)
            )

    # --- เขียนลงฐานข้อมูล ---
    with closing(_conn_alive()) as conn:
        _novel_or_404(conn, novels_id)

        with conn.cursor() as cur:
            if chapter_id:
                # แก้ไขตอนเดิม
                cur.execute(
                    "SELECT chapters_id FROM chapters WHERE chapters_id=%s AND novels_id=%s",
                    (chapter_id, novels_id),
                )
                if not dictfetchone(cur):
                    if is_autosave:
                        return _json_resp({"success": False, "error": "chapter not found"}, 404)
                    abort(404)

                # ถ้า part ไม่ valid ให้ fallback เป็น chapter_no เดิม
                if chapter_no < 1:
                    cur.execute(
                        "SELECT chapter_no FROM chapters WHERE chapters_id=%s",
                        (chapter_id,),
                    )
                    row = dictfetchone(cur)
                    if row and row.get("chapter_no"):
                        chapter_no = row["chapter_no"]
                    else:
                        chapter_no = 1

                cur.execute(
                    """
                    UPDATE chapters
                       SET chapter_no=%s,
                           title=%s,
                           content_html=%s
                     WHERE chapters_id=%s
                    """,
                    (chapter_no, title or None, content_html or None, chapter_id),
                )

            else:
                # สร้างตอนใหม่
                if chapter_no < 1:
                    # ถ้า part ไม่ถูกต้อง ให้คำนวณ MAX+1 อีกครั้ง
                    cur.execute(
                        """
                        SELECT COALESCE(MAX(chapter_no), 0) + 1 AS next_no
                          FROM chapters
                         WHERE novels_id=%s
                        """,
                        (novels_id,),
                    )
                    row = dictfetchone(cur) or {}
                    chapter_no = row.get("next_no") or 1

                cur.execute(
                    """
                    INSERT INTO chapters (novels_id, title, content_html, chapter_no)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (novels_id, title or None, content_html or None, chapter_no),
                )
                chapter_id = getattr(cur, "lastrowid", None)

        conn.commit()

       # --- ตอบกลับ ---
    if is_autosave:
        # ใช้ใน JS autosave → ต้องส่ง chapters_id กลับไปเพื่อเก็บลง hidden input
        return _json_resp({"success": True, "chapters_id": chapter_id})

    # submit ปกติ → เด้งกลับไปหน้าแก้ไขนิยาย
    flash("บันทึกตอนสำเร็จ", "success")
    return redirect(
        url_for("editnovel.edit_novel", novels_id=novels_id)
    )


# =========================  UPLOAD IMAGE จาก Quill  =========================

@writing_bp.route("/upload", methods=["POST"])
@roles_required("user")
def upload_image():
    """
    รองรับการอัปโหลดรูปจากปุ่มรูปภาพใน Quill toolbar
    - รับไฟล์จากฟิลด์ชื่อ "file"
    - เซฟลง /static/chapter_images/<ชื่อไฟล์>
    - คืน JSON {url: "<absolute-url>"} ให้ JS แทรก <img> ในเนื้อหา
    """
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "no file"}), 400

    if not _allowed_image(file.filename, file.mimetype):
        return jsonify({"error": "invalid file type"}), 400

    upload_dir = _image_upload_dir()

    fname = secure_filename(file.filename)
    stem = Path(fname).stem
    ext = Path(fname).suffix.lower()
    filename = f"{stem}_{int(datetime.utcnow().timestamp())}{ext}"

    file.save(upload_dir / filename)

    url = url_for("static", filename=f"{CHAPTER_IMAGE_SUBDIR}/{filename}")
    return jsonify({"url": url})
