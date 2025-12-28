from __future__ import annotations

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, abort, current_app, jsonify, flash
)
from contextlib import closing
from pathlib import Path
import MySQLdb  # สำหรับ conn.ping(True) ถ้าใช้ MySQLdb backend

from db import get_db_connection
from auth import roles_required
from media_storage import upload_image_file
from moderation_utils import mark_chapter_pending_review

# ---------- CONFIG ----------
CHAPTER_IMAGE_SUBDIR = "chapter_images"  # Cloudinary folder for chapter images
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


def _to_int(val, default=0):
    try:
        return int(val)
    except Exception:
        return default


def _novel_or_404(conn, novels_id: int):
    """ดึงนิยาย ถ้าไม่มีให้ 404"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM novels WHERE novels_id=%s", (novels_id,))
        novel = dictfetchone(cur)
    if not novel:
        abort(404)
    return novel



def _allowed_image(filename: str, mimetype: str | None) -> bool:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        return False
    if mimetype and not mimetype.startswith("image/"):
        return False
    return True


def _safe_next_url(next_url: str | None) -> str | None:
    """
    กัน open redirect: อนุญาตเฉพาะ path ภายในเว็บ (ขึ้นต้นด้วย /)
    """
    if not next_url:
        return None
    next_url = next_url.strip()
    if next_url.startswith("/"):
        return next_url
    return None


# =========================  PAGE: เขียน/แก้ไขตอน  =========================

@writing_bp.route("/<int:novels_id>", methods=["GET"])
@roles_required("user")
def writing_form(novels_id: int):
    """
    แสดงหน้าเขียน / แก้ไขตอน

    - ถ้ามี query string ?chapter_id=<id> = โหมดแก้ไขตอน
    - ถ้าไม่มี chapter_id = โหมดสร้างตอนใหม่
    - ถ้ามี ?reset=1 = บังคับให้เป็นฟอร์มว่าง (เคลียร์ค่าเดิม)
    """
    reset = request.args.get("reset", default=0, type=int)
    chapter_id = request.args.get("chapter_id", type=int)

    # ✅ ถ้ากด reset ให้ถือว่าเป็น “ตอนใหม่” และไม่โหลดข้อมูลเดิม
    if reset == 1:
        chapter_id = None

    with closing(_conn_alive()) as conn:
        novel = _novel_or_404(conn, novels_id)

        chapter = None
        suggested_part = 1

        with conn.cursor() as cur:
            if chapter_id:
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

    return render_template(
        "writingform.html",
        novel=novel,
        novels_id=novels_id,
        chapter=chapter,
        suggested_part=suggested_part,
    )


# =========================  CANCEL (ไม่บันทึก + เคลียร์ค่า)  =========================

@writing_bp.route("/cancel", methods=["POST"])
@roles_required("user")
def cancel_writing():
    """
    กดยกเลิก:
    - ไม่บันทึกลง DB
    - รีโหลดกลับไปฟอร์มว่างด้วย ?reset=1
    รองรับทั้ง fetch (json) และ submit ปกติ
    """
    accept = (request.headers.get("Accept") or "").lower()
    wants_json = "application/json" in accept

    novels_id = _to_int(request.form.get("novels_id"), 0)
    next_url = _safe_next_url(request.form.get("next"))

    if wants_json:
        return jsonify({"success": True, "cancelled": True})

    flash("ยกเลิกแล้ว (ไม่บันทึกข้อมูล)", "info")

    if next_url:
        return redirect(next_url)

    if novels_id:
        return redirect(url_for("writing.writing_form", novels_id=novels_id, reset=1))

    # fallback
    return redirect(url_for("home.index"))


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
    is_autosave = "application/json" in accept

    def _json_resp(payload, status=200):
        return jsonify(payload), status

    # --- ดึง novels_id ก่อน (เพื่อใช้ redirect/cancel ได้) ---
    novels_id = _to_int(request.form.get("novels_id"), 0)
    if not novels_id:
        if is_autosave:
            return _json_resp({"success": False, "error": "novels_id required"}, 400)
        abort(400, description="novels_id required")

    # ✅ กันพลาด: ถ้าปุ่ม “ยกเลิก” เผลอ POST มาที่ /save ให้ไม่เขียน DB
    action = (request.form.get("action") or request.form.get("intent") or "").strip().lower()
    if action == "cancel" or request.form.get("cancel") in ("1", "true", "yes", "on"):
        if is_autosave:
            return _json_resp({"success": True, "cancelled": True})
        flash("ยกเลิกแล้ว (ไม่บันทึกข้อมูล)", "info")
        return redirect(url_for("writing.writing_form", novels_id=novels_id, reset=1))

    chapter_id_raw = request.form.get("chapter_id")
    chapter_id = _to_int(chapter_id_raw, 0) or None

    part_raw = request.form.get("part") or ""
    chapter_no = _to_int(part_raw, 0)

    title = (request.form.get("epName") or "").strip()
    content_html = (request.form.get("content_html") or "").strip()

    # --- validation ฝั่ง server สำหรับ submit ปกติ (ไม่ใช่ autosave) ---
    if not is_autosave:
        if chapter_no < 1:
            flash("กรุณากรอกลำดับตอนที่ถูกต้อง", "error")
            return redirect(url_for("writing.writing_form", novels_id=novels_id, chapter_id=chapter_id or None))

        if not content_html.strip():
            flash("กรุณากรอกเนื้อเรื่อง", "error")
            return redirect(url_for("writing.writing_form", novels_id=novels_id, chapter_id=chapter_id or None))

    # --- เขียนลงฐานข้อมูล ---
    with closing(_conn_alive()) as conn:
        _novel_or_404(conn, novels_id)

        with conn.cursor() as cur:
            if chapter_id:
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
                    cur.execute("SELECT chapter_no FROM chapters WHERE chapters_id=%s", (chapter_id,))
                    row = dictfetchone(cur)
                    chapter_no = (row or {}).get("chapter_no") or 1

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
                try:
                    mark_chapter_pending_review(conn, chapter_id, novels_id)
                except Exception:
                    pass

            else:
                if chapter_no < 1:
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
        return _json_resp({"success": True, "chapters_id": chapter_id})

    flash("บันทึกตอนสำเร็จ", "success")
    return redirect(url_for("editnovel.edit_novel", novels_id=novels_id))


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

    try:
        url = upload_image_file(file, folder=CHAPTER_IMAGE_SUBDIR)
    except RuntimeError as e:
        current_app.logger.exception("chapter image upload failed")
        return jsonify({"error": str(e)}), 500
    return jsonify({"url": url})
