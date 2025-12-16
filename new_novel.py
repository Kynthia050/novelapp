# new_novel.py
from __future__ import annotations

from flask import (
    Blueprint, render_template, request, jsonify,
    current_app, session, redirect, g
)
from werkzeug.utils import secure_filename
from contextlib import closing
from pathlib import Path
import uuid
import json

from db import get_db_connection

# ---------- CONFIG ----------
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
COVER_SUBDIR = "cover"

# schema ของคุณไม่มี AUTO_INCREMENT ใน novels/tags/novels_tags
LOCK_NAME = "lock:new_novel_create"
LOCK_TIMEOUT = 5
# ---------------------------

new_novel_bp = Blueprint("new_novel", __name__, template_folder="templates")


# ---------- Utilities ----------
def _conn_alive():
    conn = get_db_connection()
    try:
        conn.ping(True)
    except Exception:
        pass
    return conn


def _k_to_str(k):
    if isinstance(k, (bytes, bytearray)):
        try:
            return k.decode("utf-8", "ignore")
        except Exception:
            return str(k)
    return str(k)


def _normalize_row(row: dict) -> dict:
    # ✅ ทำให้ key เป็น str เสมอ (แก้ปัญหา Jinja อ่าน c.cate_id ไม่ออก)
    out = {}
    for k, v in (row or {}).items():
        out[_k_to_str(k)] = v
    return out


def dictfetchone(cur):
    row = cur.fetchone()
    if row is None:
        return None

    if isinstance(row, dict):
        return _normalize_row(row)

    cols = [_k_to_str(d[0]) for d in (cur.description or [])]
    return dict(zip(cols, row))


def dictfetchall(cur):
    rows = cur.fetchall()
    if not rows:
        return []

    if isinstance(rows[0], dict):
        return [_normalize_row(r) for r in rows]

    cols = [_k_to_str(d[0]) for d in (cur.description or [])]
    return [dict(zip(cols, r)) for r in rows]


def _upload_dir() -> Path:
    static_folder = Path(current_app.static_folder)
    d = static_folder / COVER_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def allowed_image(filename: str, mimetype: str | None) -> bool:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        return False
    if mimetype and not mimetype.startswith("image/"):
        return False
    return True


def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = " ".join(s.split())
    s = s.replace("/", "-").replace("\\", "-")
    s = s.replace(",", " ")
    s = "-".join([p for p in s.split(" ") if p])
    return (s[:100] or "tag")


def _next_id(cur, table: str, pk: str) -> int:
    # table/pk เป็นค่าคงที่จากโค้ดเราเอง
    cur.execute(f"SELECT COALESCE(MAX({pk}),0)+1 AS next_id FROM {table}")
    row = dictfetchone(cur) or {}
    return int(row.get("next_id") or 1)


def _current_users_id() -> int | None:
    u = getattr(g, "user", None)
    if isinstance(u, dict) and u.get("users_id"):
        try:
            return int(u["users_id"])
        except Exception:
            pass
    sid = session.get("users_id")
    try:
        return int(sid) if sid is not None else None
    except Exception:
        return None


def _current_username(conn, users_id: int | None) -> str:
    u = getattr(g, "user", None)
    if isinstance(u, dict) and u.get("username"):
        return str(u.get("username") or "")

    if session.get("username"):
        return str(session.get("username") or "")

    if not users_id:
        return ""

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE users_id=%s LIMIT 1", (users_id,))
            row = dictfetchone(cur) or {}
            return str(row.get("username") or "")
    except Exception:
        return ""
# ----------------------------


@new_novel_bp.route("/novels/new", methods=["GET"])
def new_novel_form():
    users_id = _current_users_id()
    if not users_id:
        return redirect("/login")

    with closing(_conn_alive()) as conn:
        username = _current_username(conn, users_id)

        # ✅ ดึงหมวดหมู่จาก DB จริง + normalize key แล้ว
        with conn.cursor() as cur:
            cur.execute("SELECT cate_id, name FROM categories ORDER BY cate_id ASC")
            categories = dictfetchall(cur)

        # ช่วย debug เวลา DB ว่าง (ไม่ทำให้พัง)
        if not categories:
            current_app.logger.warning("categories ว่าง: ไม่พบข้อมูลในตาราง categories")

    return render_template(
        "new_novel.html",
        categories=categories,
        username=username,
    )


@new_novel_bp.route("/api/novels", methods=["POST"])
def api_create_novel():
    users_id = _current_users_id()
    if not users_id:
        return jsonify(ok=False, error="กรุณาเข้าสู่ระบบก่อนสร้างนิยาย"), 401

    content_type = (request.content_type or "").lower()
    is_multipart = content_type.startswith("multipart/form-data")

    title = ""
    synopsis = ""
    main_category = ""
    tags = []
    cover_file = None

    if is_multipart:
        form = request.form
        title = (form.get("title") or "").strip()
        synopsis = (form.get("synopsis") or "").strip()
        main_category = (form.get("mainCategory") or "").strip()

        raw_tags = form.get("tags") or "[]"
        try:
            tags = json.loads(raw_tags)
        except Exception:
            tags = []

        cover_file = request.files.get("cover")
    else:
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        synopsis = (data.get("synopsis") or "").strip()
        main_category = str(data.get("mainCategory") or "").strip()
        tags = data.get("tags") or []
        cover_file = None

    # ---- validation ตาม schema ----
    title = title[:150]                 # novels.title varchar(150)
    synopsis = (synopsis or "")[:200]   # novels.description varchar(200)

    if not title:
        return jsonify(ok=False, error="กรุณากรอกชื่อเรื่อง"), 400
    if not main_category:
        return jsonify(ok=False, error="กรุณาเลือกหมวดหมู่"), 400

    try:
        cate_id = int(main_category)
    except Exception:
        return jsonify(ok=False, error="หมวดหมู่ไม่ถูกต้อง"), 400

    will_upload_cover = bool(cover_file and cover_file.filename)
    if will_upload_cover and not allowed_image(cover_file.filename, cover_file.mimetype):
        return jsonify(ok=False, error="ชนิดไฟล์ภาพไม่ถูกต้อง (รองรับ .jpg .jpeg .png .webp)"), 400

    saved_cover_path: Path | None = None
    cover_filename: str | None = None

    with closing(_conn_alive()) as conn:
        try:
            with conn.cursor() as cur:
                # ✅ lock กัน id ชนกัน
                cur.execute("SELECT GET_LOCK(%s, %s) AS got", (LOCK_NAME, LOCK_TIMEOUT))
                got = (dictfetchone(cur) or {}).get("got")
                if got != 1:
                    return jsonify(ok=False, error="ระบบกำลังทำงาน กรุณาลองใหม่อีกครั้ง"), 503

                try:
                    # ตรวจ cate_id มีจริง
                    cur.execute("SELECT cate_id FROM categories WHERE cate_id=%s", (cate_id,))
                    if not dictfetchone(cur):
                        return jsonify(ok=False, error="หมวดหมู่ไม่ถูกต้อง"), 400

                    # gen id
                    novels_id = _next_id(cur, "novels", "novels_id")

                    # อัปโหลดปก (ถ้ามี)
                    if will_upload_cover:
                        upload_dir = _upload_dir()
                        ext = Path(secure_filename(cover_file.filename)).suffix.lower()
                        cover_filename = f"n{novels_id}_{uuid.uuid4().hex}{ext}"
                        saved_cover_path = upload_dir / cover_filename
                        cover_file.save(saved_cover_path)

                    # INSERT novels (ตรง schema)
                    cur.execute(
                        """
                        INSERT INTO novels (novels_id, title, description, users_id, cate_id, cover)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (novels_id, title, synopsis or None, users_id, cate_id, cover_filename),
                    )

                    # ---- TAGS + MAP ----
                    clean_tags: list[str] = []
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
                    clean_tags = clean_tags[:20]

                    next_tag_id = _next_id(cur, "tags", "tag_id")
                    next_nt_id = _next_id(cur, "novels_tags", "nt_id")

                    for name in clean_tags:
                        slug = _slugify(name)

                        # หา tag เดิม (schema ไม่มี unique slug จึงเลือกตัวแรก)
                        cur.execute(
                            "SELECT tag_id FROM tags WHERE slug=%s OR name=%s ORDER BY tag_id ASC LIMIT 1",
                            (slug, name[:50]),
                        )
                        row = dictfetchone(cur)
                        if row and row.get("tag_id") is not None:
                            tag_id = int(row["tag_id"])
                        else:
                            tag_id = next_tag_id
                            next_tag_id += 1
                            cur.execute(
                                "INSERT INTO tags (tag_id, name, slug) VALUES (%s, %s, %s)",
                                (tag_id, name[:50], slug),
                            )

                        # map ถ้ายังไม่มี
                        cur.execute(
                            "SELECT nt_id FROM novels_tags WHERE novels_id=%s AND tag_id=%s LIMIT 1",
                            (novels_id, tag_id),
                        )
                        if not dictfetchone(cur):
                            nt_id = next_nt_id
                            next_nt_id += 1
                            cur.execute(
                                "INSERT INTO novels_tags (nt_id, novels_id, tag_id) VALUES (%s, %s, %s)",
                                (nt_id, novels_id, tag_id),
                            )

                    conn.commit()
                finally:
                    cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))

        except Exception as e:
            conn.rollback()
            try:
                if saved_cover_path and saved_cover_path.exists():
                    saved_cover_path.unlink()
            except Exception:
                pass

            current_app.logger.exception("สร้างนิยายใหม่ไม่สำเร็จ: %s", e)
            return jsonify(ok=False, error="บันทึกไม่สำเร็จ กรุณาลองใหม่อีกครั้ง"), 500

    return jsonify(ok=True, novels_id=novels_id), 200


@new_novel_bp.route("/novels/<int:novels_id>", methods=["GET"])
def view_novel(novels_id: int):
    return redirect(f"/novel/{novels_id}")
