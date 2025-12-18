# new_novel.py
from __future__ import annotations

from flask import (
    Blueprint, render_template, request, jsonify,
    current_app, session, redirect, g
)
from werkzeug.utils import secure_filename
from contextlib import closing
from pathlib import Path
from datetime import datetime
import uuid
import json

from db import get_db_connection

# ---------- CONFIG ----------
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
COVER_SUBDIR = "cover"

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


def _read_payload():
    """
    รองรับ:
      - title
      - description (สไตล์ edit_novel)
      - cate_id
      - cover
      - tags (list หรือ json-string)
    และ fallback ของเดิม:
      - synopsis -> description
      - mainCategory -> cate_id
    """
    content_type = (request.content_type or "").lower()
    is_multipart = content_type.startswith("multipart/form-data")

    title = ""
    description = ""
    cate_raw = ""
    tags = []
    cover_file = None

    if is_multipart:
        form = request.form
        title = (form.get("title") or "").strip()
        description = (form.get("description") or form.get("synopsis") or "").strip()
        cate_raw = (form.get("cate_id") or form.get("mainCategory") or "").strip()

        raw_tags = form.get("tags") or "[]"
        try:
            tags = json.loads(raw_tags) if isinstance(raw_tags, str) else (raw_tags or [])
        except Exception:
            tags = []

        cover_file = request.files.get("cover")
    else:
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        description = (data.get("description") or data.get("synopsis") or "").strip()
        cate_raw = str(data.get("cate_id") or data.get("mainCategory") or "").strip()
        tags = data.get("tags") or []
        cover_file = None

    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []

    if not isinstance(tags, list):
        tags = []

    return title, description, cate_raw, tags, cover_file


def _save_cover_file(cover_file, novels_id: int) -> tuple[str | None, Path | None]:
    if not cover_file or not getattr(cover_file, "filename", ""):
        return None, None

    upload_dir = _upload_dir()
    fname = secure_filename(cover_file.filename)
    ext = Path(fname).suffix.lower() or ".jpg"
    stamp = int(datetime.utcnow().timestamp())
    cover_filename = f"n{novels_id}_{stamp}_{uuid.uuid4().hex}{ext}"
    saved_path = upload_dir / cover_filename
    cover_file.save(saved_path)
    return cover_filename, saved_path


def _tag_find_or_create(cur, name: str) -> int:
    """
    ✅ ปลอดภัย: ถ้า tags ไม่มีคอลัมน์ slug ก็ยังทำงานได้
    """
    name50 = (name or "").strip()[:50]
    if not name50:
        raise ValueError("empty tag")

    # 1) หาโดย name ก่อน (ปลอดภัยสุด)
    cur.execute("SELECT tag_id FROM tags WHERE name=%s ORDER BY tag_id ASC LIMIT 1", (name50,))
    row = dictfetchone(cur)
    if row and row.get("tag_id") is not None:
        return int(row["tag_id"])

    slug = _slugify(name50)

    # 2) ถ้ามี slug ก็ลองหาเพิ่ม (ถ้าไม่มีจะ except แล้วข้าม)
    try:
        cur.execute("SELECT tag_id FROM tags WHERE slug=%s ORDER BY tag_id ASC LIMIT 1", (slug,))
        row = dictfetchone(cur)
        if row and row.get("tag_id") is not None:
            return int(row["tag_id"])
    except Exception:
        pass

    # 3) สร้างใหม่
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
# ----------------------------


@new_novel_bp.route("/novels/new", methods=["GET"])
def new_novel_form():
    users_id = _current_users_id()
    if not users_id:
        return redirect("/login")

    with conn.cursor() as cur:
        cur.execute("SELECT DATABASE() AS db")
        dbname = (dictfetchone(cur) or {}).get("db")

        cur.execute("SELECT COUNT(*) AS n FROM categories")
        n = (dictfetchone(cur) or {}).get("n")

        cur.execute("SELECT cate_id, name FROM categories ORDER BY name")
        categories = dictfetchall(cur)

        current_app.logger.warning("NEW_NOVEL DB=%s | categories_count=%s | sample=%r", dbname, n, categories[:3])

        return render_template(
        "new_novel.html",
        categories=categories,
        username=username,
        all_tags=all_tags,
    )


@new_novel_bp.route("/api/novels", methods=["POST"])
def api_create_novel():
    users_id = _current_users_id()
    if not users_id:
        return jsonify(ok=False, error="กรุณาเข้าสู่ระบบก่อนสร้างนิยาย"), 401

    title, description, cate_raw, tags, cover_file = _read_payload()

    title = (title or "")[:150]
    description = (description or "")[:200]

    if not title:
        return jsonify(ok=False, error="กรุณากรอกชื่อเรื่อง"), 400
    if not cate_raw:
        return jsonify(ok=False, error="กรุณาเลือกหมวดหมู่"), 400

    try:
        cate_id = int(str(cate_raw).strip())
    except Exception:
        return jsonify(ok=False, error="หมวดหมู่ไม่ถูกต้อง"), 400

    will_upload_cover = bool(cover_file and getattr(cover_file, "filename", ""))
    if will_upload_cover and not allowed_image(cover_file.filename, cover_file.mimetype):
        return jsonify(ok=False, error="ชนิดไฟล์ภาพไม่ถูกต้อง (รองรับ .jpg .jpeg .png .webp)"), 400

    saved_cover_path: Path | None = None
    cover_filename: str | None = None
    novels_id: int | None = None

    with closing(_conn_alive()) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT GET_LOCK(%s, %s) AS got", (LOCK_NAME, LOCK_TIMEOUT))
                got = (dictfetchone(cur) or {}).get("got")
                if got != 1:
                    return jsonify(ok=False, error="ระบบกำลังทำงาน กรุณาลองใหม่อีกครั้ง"), 503

                try:
                    cur.execute("SELECT cate_id FROM categories WHERE cate_id=%s", (cate_id,))
                    if not dictfetchone(cur):
                        return jsonify(ok=False, error="หมวดหมู่ไม่ถูกต้อง"), 400

                    novels_id = _next_id(cur, "novels", "novels_id")

                    if will_upload_cover:
                        cover_filename, saved_cover_path = _save_cover_file(cover_file, novels_id)

                    cur.execute(
                        """
                        INSERT INTO novels (novels_id, title, description, users_id, cate_id, cover)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (novels_id, title, description or None, users_id, cate_id, cover_filename),
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

                    # nt_id อาจไม่มี -> ทำให้เป็น optional
                    use_nt_id = True
                    next_nt_id = None
                    try:
                        next_nt_id = _next_id(cur, "novels_tags", "nt_id")
                    except Exception:
                        use_nt_id = False

                    for name in clean_tags:
                        tag_id = _tag_find_or_create(cur, name)

                        # ✅ ปลอดภัย: ไม่อ้าง nt_id ตอนเช็ค
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
                                # ถ้า insert แบบมี nt_id ไม่ได้ ก็ fallback
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
