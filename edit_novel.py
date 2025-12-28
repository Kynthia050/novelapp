from __future__ import annotations
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, jsonify, abort, current_app, flash
)
from contextlib import closing
from pathlib import Path
import json
import MySQLdb  # สำหรับ conn.ping(True)

from db import get_db_connection
from media_storage import upload_image_file

# ---------- CONFIG ----------
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
# Legacy static folder name / Cloudinary folder
COVER_SUBDIR = "cover"
# ---------------------------

# หน้า UI + ฟอร์มบันทึก
editnovel_bp = Blueprint("editnovel", __name__, template_folder="templates")
# API สำหรับ ajax จากหน้าเดียวกัน
api_bp = Blueprint("api", __name__, url_prefix="/api")


# ---------- Utilities ----------
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
    # ถ้ามี mimetype ให้เช็คคร่าว ๆ ด้วย
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


def _novel_or_404(conn, novels_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM novels WHERE novels_id=%s", (novels_id,))
        novel = dictfetchone(cur)
    if not novel:
        abort(404)
    return novel


def _cover_url(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    if s.startswith(("http://", "https://", "/")):
        return s
    if s.startswith("static/"):
        return "/" + s
    if s.startswith("cover/"):
        return url_for("static", filename=s)
    return url_for("static", filename=f"{COVER_SUBDIR}/{s}")


def _local_cover_filename(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    if s.startswith(("http://", "https://")):
        return None
    if s.startswith("/"):
        s = s[1:]
    if s.startswith("static/"):
        s = s[len("static/"):]
    if s.startswith("cover/"):
        s = s[len("cover/"):]
    if "/" in s or "\\" in s:
        return None
    return s or None


def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = " ".join(s.split())
    s = s.replace("/", "-").replace("\\", "-")
    s = s.replace(",", " ")
    s = "-".join([p for p in s.split(" ") if p])
    return (s[:100] or "tag")


def _next_id(cur, table: str, pk: str) -> int:
    cur.execute(f"SELECT COALESCE(MAX({pk}),0)+1 AS next_id FROM {table}")
    row = dictfetchone(cur) or {}
    return int(row.get("next_id") or 1)


def _tag_find_or_create(cur, name: str) -> int:
    name50 = (name or "").strip()[:50]
    if not name50:
        raise ValueError("empty tag")

    cur.execute("SELECT tag_id FROM tags WHERE name=%s ORDER BY tag_id ASC LIMIT 1", (name50,))
    row = dictfetchone(cur)
    if row and row.get("tag_id") is not None:
        return int(row["tag_id"])

    slug = _slugify(name50)
    try:
        cur.execute("SELECT tag_id FROM tags WHERE slug=%s ORDER BY tag_id ASC LIMIT 1", (slug,))
        row = dictfetchone(cur)
        if row and row.get("tag_id") is not None:
            return int(row["tag_id"])
    except Exception:
        pass

    tag_id = _next_id(cur, "tags", "tag_id")
    try:
        cur.execute(
            "INSERT INTO tags (tag_id, name, slug) VALUES (%s, %s, %s)",
            (tag_id, name50, slug),
        )
    except Exception:
        cur.execute(
            "INSERT INTO tags (tag_id, name) VALUES (%s, %s)",
            (tag_id, name50),
        )

    return int(tag_id)


def _read_tags_from_request():
    tags = None
    content_type = (request.content_type or "").lower()
    if content_type.startswith("application/json"):
        data = request.get_json(silent=True) or {}
        if "tags" in data:
            tags = data.get("tags")
    else:
        if "tags" in request.form:
            tags = request.form.get("tags")

    if tags is None:
        return None
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    if not isinstance(tags, list):
        tags = []
    return tags


def _clean_tags(tags):
    clean_tags = []
    seen = set()
    for t in (tags or []):
        s = (t or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        clean_tags.append(s)
    return clean_tags[:20]


def _json_error(msg: str, code: int = 400):
    return jsonify({"error": msg}), code
# --------------------------------


# =========================  PAGES  =========================
@editnovel_bp.route("/<int:novels_id>/edit", methods=["GET"])
def edit_novel(novels_id):
    """
    แสดงหน้าแก้ไขนิยาย + เติมข้อมูลที่ต้องใช้ในฟอร์ม
    ใช้ตาราง: novels, categories, chapters, novels_tags(+tags)
    """
    with closing(_conn_alive()) as conn:
        novel = _novel_or_404(conn, novels_id)  # novels มี title, description, cate_id, cover, ฯลฯ

        # URL ปก (ถ้ามีไฟล์)
        cover_url = _cover_url(novel.get("cover"))

        with conn.cursor() as cur:
            # หมวดหมู่
            cur.execute("SELECT cate_id, name FROM categories ORDER BY name")
            categories = dictfetchall(cur)

            # แท็กของเรื่องนี้
            cur.execute(
                """
                SELECT t.tag_id, t.name
                  FROM tags t
                  JOIN novels_tags nt ON nt.tag_id = t.tag_id
                 WHERE nt.novels_id = %s
                 ORDER BY t.name
                """,
                (novels_id,),
            )
            tags = dictfetchall(cur)

            # แท็กทั้งหมด (สำหรับ datalist)
            cur.execute("SELECT tag_id, name FROM tags ORDER BY name")
            all_tags = dictfetchall(cur)

            # ตอนทั้งหมด — ไม่ดึง content_html เพื่อลด payload
            cur.execute(
                """
                SELECT chapters_id, title, chapter_no, status, created_at, updated_at
                  FROM chapters
                 WHERE novels_id = %s
                 ORDER BY chapter_no ASC
                """,
                (novels_id,),
            )
            chapters = dictfetchall(cur)

    return render_template(
        "edit_novel.html",
        novel={**novel, "cover_url": cover_url},
        categories=categories,
        tags=tags,
        all_tags=all_tags,
        chapters=chapters,
    )


@editnovel_bp.route("/<int:novels_id>", methods=["POST"])
def update_novel(novels_id):
    """
    รับฟอร์มจากหน้า edit:
    - title, description, cate_id
    - cover (ไฟล์รูป) -> บันทึกชื่อไฟล์ลง novels.cover
    """
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    cate_id = request.form.get("cate_id")
    tags = _read_tags_from_request()

    if not title or not cate_id:
        flash("กรุณากรอกชื่อเรื่องและหมวดหมู่", "error")
        return redirect(url_for("editnovel.edit_novel", novels_id=novels_id))

    # จัดการไฟล์ปก (ถ้ามี)
    file = request.files.get("cover")
    will_upload_cover = file and file.filename

    if will_upload_cover and not allowed_image(file.filename, file.mimetype):
        flash("ชนิดไฟล์ภาพไม่ถูกต้อง (รองรับ .jpg .jpeg .png .webp)", "error")
        return redirect(url_for("editnovel.edit_novel", novels_id=novels_id))

    with closing(_conn_alive()) as conn:
        # ตรวจ novel (404 ถ้าไม่มี)
        novel = _novel_or_404(conn, novels_id)

        # ตรวจ cate_id ว่ามีจริง (กัน foreign key fail แบบ user friendly)
        with conn.cursor() as cur:
            cur.execute("SELECT cate_id FROM categories WHERE cate_id=%s", (cate_id,))
            if not dictfetchone(cur):
                flash("หมวดหมู่ไม่ถูกต้อง", "error")
                return redirect(url_for("editnovel.edit_novel", novels_id=novels_id))

        # เตรียมชื่อไฟล์ใหม่ (ถ้ามี)
        cover_value = None
        old_cover_ref = novel.get("cover")

        if will_upload_cover:
            try:
                cover_value = upload_image_file(file, folder=COVER_SUBDIR)
            except RuntimeError as e:
                flash(str(e), "error")
                return redirect(url_for("editnovel.edit_novel", novels_id=novels_id))

        # อัปเดต DB
        with conn.cursor() as cur:
            if cover_value:
                cur.execute(
                    """
                    UPDATE novels
                       SET title=%s, description=%s, cate_id=%s, cover=%s
                     WHERE novels_id=%s
                    """,
                    (title, description or None, cate_id, cover_value, novels_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE novels
                       SET title=%s, description=%s, cate_id=%s
                     WHERE novels_id=%s
                    """,
                    (title, description or None, cate_id, novels_id),
                )
        if tags is not None:
            clean_tags = _clean_tags(tags)
            with conn.cursor() as cur:
                tag_ids = []
                for name in clean_tags:
                    tag_id = _tag_find_or_create(cur, name)
                    tag_ids.append(tag_id)

                if tag_ids:
                    placeholders = ", ".join(["%s"] * len(tag_ids))
                    cur.execute(
                        f"DELETE FROM novels_tags WHERE novels_id=%s AND tag_id NOT IN ({placeholders})",
                        (novels_id, *tag_ids),
                    )
                else:
                    cur.execute("DELETE FROM novels_tags WHERE novels_id=%s", (novels_id,))

                use_nt_id = True
                next_nt_id = None
                try:
                    next_nt_id = _next_id(cur, "novels_tags", "nt_id")
                except Exception:
                    use_nt_id = False

                for tag_id in tag_ids:
                    cur.execute(
                        "SELECT 1 FROM novels_tags WHERE novels_id=%s AND tag_id=%s LIMIT 1",
                        (novels_id, tag_id),
                    )
                    if dictfetchone(cur):
                        continue

                    if use_nt_id:
                        nt_id = int(next_nt_id)
                        next_nt_id += 1
                        try:
                            cur.execute(
                                "INSERT INTO novels_tags (nt_id, novels_id, tag_id) VALUES (%s, %s, %s)",
                                (nt_id, novels_id, tag_id),
                            )
                        except Exception:
                            use_nt_id = False
                            cur.execute(
                                "INSERT INTO novels_tags (novels_id, tag_id) VALUES (%s, %s)",
                                (novels_id, tag_id),
                            )
                    else:
                        cur.execute(
                            "INSERT INTO novels_tags (novels_id, tag_id) VALUES (%s, %s)",
                            (novels_id, tag_id),
                        )

        conn.commit()

        # ลบไฟล์ปกเก่าหลัง commit สำเร็จ (ถ้ามีและอัปโหลดใหม่จริง)
        if cover_value and old_cover_ref:
            old_local = _local_cover_filename(old_cover_ref)
            if old_local:
                try:
                    (Path(current_app.static_folder) / COVER_SUBDIR / old_local).unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass

    flash("บันทึกสำเร็จ", "success")
    return redirect(url_for("mywrite"))


@editnovel_bp.post("/<int:novels_id>/chapters/<int:chapter_id>/status")
def update_chapter_status(novels_id, chapter_id):
    """อัปเดต status ของตอน (draft / published) จากหน้า edit_novel"""
    new_status = (request.form.get("status") or "").strip()
    if new_status not in ("draft", "published"):
        # ถ้าเป็น AJAX ให้คืน JSON
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "error": "invalid status"}), 400

        flash("สถานะไม่ถูกต้อง", "error")
        return redirect(url_for("editnovel.edit_novel", novels_id=novels_id))

    with closing(_conn_alive()) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("DESCRIBE chapters")
                ccols = {row["Field"] for row in cur.fetchall()}
            except Exception:
                ccols = set()

            if "updated_at" in ccols:
                cur.execute(
                    """
                    UPDATE chapters
                       SET status = %s,
                           updated_at = NOW()
                     WHERE chapters_id = %s
                       AND novels_id = %s
                    """,
                    (new_status, chapter_id, novels_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE chapters
                       SET status = %s
                     WHERE chapters_id = %s
                       AND novels_id = %s
                    """,
                    (new_status, chapter_id, novels_id),
                )
        conn.commit()

    # ✅ ถ้าเป็น AJAX: คืน JSON เพื่อไม่ต้อง refresh
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "chapter_id": chapter_id, "status": new_status}), 200

    # ✅ fallback แบบเดิม (กรณี JS ปิด): refresh + flash
    flash("อัปเดตสถานะตอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("editnovel.edit_novel", novels_id=novels_id))



# ----- ลบนิยายจากหน้าเว็บ (ใช้กับปุ่ม "ลบงานเขียนนี้") -----
@editnovel_bp.post("/<int:novels_id>/delete")
def delete_novel_page(novels_id):
    with closing(_conn_alive()) as conn:
        # เอาไว้ลบไฟล์ปกด้วย
        with conn.cursor() as cur:
            cur.execute("SELECT cover FROM novels WHERE novels_id=%s", (novels_id,))
            row = dictfetchone(cur)
            if not row:
                abort(404)
            cover_filename = row.get("cover")

            # ถ้า schema ตั้ง FK ON DELETE CASCADE ตารางลูกจะถูกลบให้อัตโนมัติ
            cur.execute("DELETE FROM novels WHERE novels_id=%s", (novels_id,))
        conn.commit()

    # ลบไฟล์ปกถ้ามี
    if cover_filename:
        cover_local = _local_cover_filename(cover_filename)
        if cover_local:
            try:
                (Path(current_app.static_folder) / COVER_SUBDIR / cover_local).unlink(
                    missing_ok=True
                )
            except Exception:
                pass

    flash("ลบงานเขียนเรียบร้อยแล้ว", "success")
    # กลับหน้าแรก (ปรับตาม endpoint จริงของโปรเจกต์ได้)
    return redirect("/")


# ----- ลบตอนจากหน้าเว็บ (ใช้กับปุ่ม "ลบ" ในลิสต์ตอน) -----
@editnovel_bp.post("/<int:novels_id>/chapters/<int:chapter_id>/delete")
def delete_chapter_page(novels_id, chapter_id):
    with closing(_conn_alive()) as conn:
        with conn.cursor() as cur:
            # ล็อกแถวตอนที่จะลบไว้ก่อนกัน race condition
            cur.execute(
                """
                SELECT chapter_no
                  FROM chapters
                 WHERE chapters_id=%s AND novels_id=%s
                 FOR UPDATE
                """,
                (chapter_id, novels_id),
            )
            row = dictfetchone(cur)
            if not row:
                flash("ไม่พบตอนที่ต้องการลบ", "error")
                return redirect(url_for("editnovel.edit_novel", novels_id=novels_id))

            deleted_no = row.get("chapter_no")

            # ลบตอน
            cur.execute(
                "DELETE FROM chapters WHERE chapters_id=%s AND novels_id=%s",
                (chapter_id, novels_id),
            )

            # เลื่อนเลขตอนถัดไปขึ้นมาแทน
            if deleted_no is not None:
                cur.execute(
                    """
                    UPDATE chapters
                       SET chapter_no = chapter_no - 1
                     WHERE novels_id = %s
                       AND chapter_no > %s
                    """,
                    (novels_id, deleted_no),
                )

        conn.commit()

    flash("ลบตอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("editnovel.edit_novel", novels_id=novels_id))



# =========================  API: TAGS  =========================
@api_bp.post("/novels/<int:novels_id>/tags")
def add_tag(novels_id):
    """
    เพิ่มแท็กให้เรื่อง:
      - ถ้าไม่มีแท็กนี้ -> สร้างในตาราง tags แล้วผูกใน novels_tags
      - ถ้ามี -> ผูกใน novels_tags (unique กันซ้ำอยู่แล้ว)
    """
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return _json_error("name required", 400)

    with closing(_conn_alive()) as conn:
        _novel_or_404(conn, novels_id)

        with conn.cursor() as cur:
            # สร้าง/ดึง tag_id โดยพึ่ง UNIQUE(name)
            cur.execute(
                """
                INSERT INTO tags (name) VALUES (%s)
                ON DUPLICATE KEY UPDATE tag_id=LAST_INSERT_ID(tag_id)
                """,
                (name,),
            )
            cur.execute("SELECT LAST_INSERT_ID() AS tag_id")
            tag_row = dictfetchone(cur)
            tag_id = tag_row["tag_id"]

            # ผูก map กับนิยาย (unique คู่ novels_id, tag_id)
            cur.execute(
                "INSERT IGNORE INTO novels_tags (novels_id, tag_id) VALUES (%s,%s)",
                (novels_id, tag_id),
            )

            cur.execute("SELECT tag_id, name FROM tags WHERE tag_id=%s", (tag_id,))
            tag = dictfetchone(cur)

        conn.commit()
    # 200 (มีอยู่แล้ว) / 201 (เพิ่งผูกครั้งแรก) ก็ใช้งานได้เหมือนกัน; ส่ง 200 ไว้เรียบง่าย
    return jsonify(tag), 200


@api_bp.delete("/novels/<int:novels_id>/tags/<int:tag_id>")
def remove_tag(novels_id, tag_id):
    with closing(_conn_alive()) as conn:
        _novel_or_404(conn, novels_id)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM novels_tags WHERE novels_id=%s AND tag_id=%s",
                (novels_id, tag_id),
            )
        conn.commit()
    return jsonify({"ok": True}), 200


# =========================  API: CHAPTERS  =========================
@api_bp.post("/novels/<int:novels_id>/chapters")
def create_chapter(novels_id):
    """
    สร้างตอนใหม่:
      - คำนวณ chapter_no = MAX(chapter_no)+1 ของนิยายเรื่องนั้น (ล็อกช่วงอ่าน/เขียนแบบง่าย)
      - INSERT แล้วคืน chapters_id, chapter_no, title, created_at
    """
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    content_html = (data.get("content_html") or "").strip()
    if not title or not content_html:
        return _json_error("title and content_html required", 400)

    with closing(_conn_alive()) as conn:
        _novel_or_404(conn, novels_id)

        with conn.cursor() as cur:
            # ล็อกแถวที่เกี่ยวข้องระดับเรื่องนี้เพื่อลดโอกาส chapter_no ชน (ถ้า DB รองรับ)
            cur.execute(
                "SELECT COALESCE(MAX(chapter_no), 0)+1 AS next_no "
                "FROM chapters WHERE novels_id=%s FOR UPDATE",
                (novels_id,),
            )
            next_no = dictfetchone(cur)["next_no"]

            cur.execute(
                """
                INSERT INTO chapters (novels_id, title, content_html, chapter_no)
                VALUES (%s, %s, %s, %s)
                """,
                (novels_id, title, content_html, next_no),
            )
            new_id = getattr(cur, "lastrowid", None)

            cur.execute(
                """
                SELECT chapters_id, chapter_no, title, created_at
                  FROM chapters
                 WHERE chapters_id=%s
                """,
                (new_id,),
            )
            row = dictfetchone(cur)

        conn.commit()
    return jsonify(row), 200


@api_bp.get("/chapters/<int:chapter_id>")
def get_chapter(chapter_id):
    with closing(_conn_alive()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, content_html FROM chapters WHERE chapters_id=%s",
                (chapter_id,),
            )
            row = dictfetchone(cur)
    if not row:
        return _json_error("not found", 404)
    return jsonify(row), 200


@api_bp.put("/chapters/<int:chapter_id>")
def update_chapter(chapter_id):
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    content_html = (data.get("content_html") or "").strip()

    if not title and not content_html:
        return _json_error("no change", 400)

    with closing(_conn_alive()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chapters_id FROM chapters WHERE chapters_id=%s",
                (chapter_id,),
            )
            if not dictfetchone(cur):
                return _json_error("not found", 404)

            # แก้ไข title / content พร้อมบังคับกลับเป็น draft
            cur.execute(
                """
                UPDATE chapters
                   SET title = %s,
                       content_html = %s,
                       status = 'draft'
                 WHERE chapters_id = %s
                """,
                (title, content_html, chapter_id),
            )
        conn.commit()

    return jsonify({"ok": True}), 200



@api_bp.delete("/chapters/<int:chapter_id>")
def delete_chapter(chapter_id):
    shifted = 0
    with closing(_conn_alive()) as conn:
        with conn.cursor() as cur:
            # ดึง novels_id + chapter_no ของตอนที่จะลบ แล้วล็อกแถว
            cur.execute(
                """
                SELECT novels_id, chapter_no
                  FROM chapters
                 WHERE chapters_id=%s
                 FOR UPDATE
                """,
                (chapter_id,),
            )
            row = dictfetchone(cur)
            if not row:
                return _json_error("not found", 404)

            novels_id = row.get("novels_id")
            deleted_no = row.get("chapter_no")

            # ลบตอน
            cur.execute("DELETE FROM chapters WHERE chapters_id=%s", (chapter_id,))

            # เลื่อนเลขตอนถัดไปขึ้นมาแทน (เฉพาะในนิยายเรื่องเดียวกัน)
            if novels_id is not None and deleted_no is not None:
                cur.execute(
                    """
                    UPDATE chapters
                       SET chapter_no = chapter_no - 1
                     WHERE novels_id = %s
                       AND chapter_no > %s
                    """,
                    (novels_id, deleted_no),
                )
                shifted = cur.rowcount

        conn.commit()

    return jsonify({"ok": True, "shifted": shifted}), 200




@api_bp.delete("/novels/<int:novels_id>")
def delete_novel(novels_id):
    with closing(_conn_alive()) as conn:
        # มี/ไม่มีนิยายนี้?
        with conn.cursor() as cur:
            cur.execute("SELECT novels_id FROM novels WHERE novels_id=%s", (novels_id,))
            row = dictfetchone(cur)
            if not row:
                return _json_error("not found", 404)

            # ถ้า schema ตั้ง FK ON DELETE CASCADE ตารางลูกจะถูกลบให้อัตโนมัติ
            cur.execute("DELETE FROM novels WHERE novels_id=%s", (novels_id,))
        conn.commit()
    return jsonify({"ok": True}), 200
