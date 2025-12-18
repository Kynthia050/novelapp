from __future__ import annotations

from flask import (
    Blueprint, render_template, abort, url_for,
    request, jsonify, g, session
)
from werkzeug.exceptions import HTTPException
from MySQLdb.cursors import DictCursor
from db import get_db_connection
from html import unescape

reading_bp = Blueprint("reading", __name__, template_folder="templates")


# ---------- Utilities ----------

def split_paragraphs(content: str):
    if not content:
        return []
    text = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in text.split("\n") if p.strip()]
    return parts


def _table_exists(cur, name: str) -> bool:
    try:
        cur.execute(f"DESCRIBE {name}")
        cur.fetchall()
        return True
    except Exception:
        return False


def _columns(cur, table: str) -> set[str]:
    try:
        cur.execute(f"DESCRIBE {table}")
        return {r["Field"] for r in cur.fetchall()}
    except Exception:
        return set()


def _as_text(v):
    """แปลงค่าจาก DB ให้เป็น str แบบปลอดภัย (รองรับ bytes)"""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except Exception:
            return v.decode("utf-8", errors="ignore")
    return str(v)


def _maybe_unescape_html(s: str) -> str:
    """
    แก้กรณีเก็บ HTML แบบถูก escape มาแล้ว เช่น &lt;p&gt;...&lt;/p&gt;
    จะ unescape เฉพาะเมื่อมีสัญญาณว่าเป็น HTML ที่ถูก encode จริง
    """
    if not s:
        return s
    has_lt = ("&lt;" in s) or ("&#60;" in s)
    has_gt = ("&gt;" in s) or ("&#62;" in s)
    if has_lt and has_gt:
        return unescape(s)
    return s


def _get_current_user_id() -> int | None:
    """ดึง users_id ของผู้ใช้ที่ล็อกอินอยู่"""
    if hasattr(g, "user") and g.user:
        uid = g.user.get("users_id") or g.user.get("id")
        if uid:
            return int(uid)

    uid = session.get("users_id") or session.get("user_id")
    if uid:
        return int(uid)

    return None


def _get_payload() -> dict:
    """รองรับทั้ง form-data และ JSON"""
    if request.is_json:
        return request.get_json(silent=True) or {}
    # form / x-www-form-urlencoded / multipart
    return dict(request.form or {})


def _pick(payload: dict, *keys: str):
    for k in keys:
        if k in payload and payload.get(k) not in (None, ""):
            return payload.get(k)
    return None


def _to_int_strict(v) -> int:
    """แปลงเป็น int แบบเคร่ง (ใช้กับ id)"""
    if v is None:
        raise ValueError("missing")
    if isinstance(v, bool):
        raise ValueError("invalid")
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return int(v)
    s = str(v).strip()
    if s == "":
        raise ValueError("empty")
    return int(s)


def _to_progress(v) -> int:
    """แปลง progress ให้ทน: รับ int/float/'12.34'/'12%'/' 12 '"""
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        p = v
    elif isinstance(v, float):
        p = int(round(v))
    else:
        s = str(v).strip()
        if not s:
            return 0
        if s.endswith("%"):
            s = s[:-1].strip()
        try:
            p = int(s)
        except Exception:
            try:
                p = int(round(float(s)))
            except Exception:
                return 0

    if p < 0:
        return 0
    if p > 100:
        return 100
    return p


# ---------- reading_history (per chapter) ----------

def _needs_rh_id(cur) -> bool:
    """
    เผื่อกรณี rh_id ไม่ AUTO_INCREMENT:
    - ถ้า insert แบบไม่ใส่ rh_id แล้วล้มด้วย error 'doesn't have a default value'
      เราจะ fallback สร้าง rh_id ให้เอง
    """
    cols = _columns(cur, "reading_history")
    return "rh_id" in cols


def _next_rh_id(cur) -> int:
    cur.execute("SELECT COALESCE(MAX(rh_id),0) + 1 AS next_id FROM reading_history")
    row = cur.fetchone() or {}
    return int(row.get("next_id") or 1)


def _upsert_reading_history_per_chapter(
    cur,
    user_id: int,
    novels_id: int,
    chapters_id: int,
    progress: int = 0,
) -> None:
    """
    reading_history ต่อ chapter:
    UNIQUE (users_id, novels_id, chapters_id)
    """
    if not _table_exists(cur, "reading_history"):
        return

    cols = _columns(cur, "reading_history")
    # คอลัมน์หลักตามสคีมาที่คุณส่งมา
    need = {"users_id", "novels_id", "chapters_id"}
    if not need.issubset(cols):
        return

    has_progress = "progress" in cols
    has_last = "last_read_at" in cols
    has_rh_id = "rh_id" in cols

    base_cols = ["users_id", "novels_id", "chapters_id"]
    base_vals = ["%s", "%s", "%s"]
    params = [int(user_id), int(novels_id), int(chapters_id)]

    if has_progress:
        base_cols.append("progress")
        base_vals.append("%s")
        params.append(int(progress))

    if has_last:
        base_cols.append("last_read_at")
        base_vals.append("CURRENT_TIMESTAMP")

    upd = []
    if has_progress:
        upd.append("progress = VALUES(progress)")
    if has_last:
        upd.append("last_read_at = CURRENT_TIMESTAMP")

    sql = f"""
        INSERT INTO reading_history ({", ".join(base_cols)})
        VALUES ({", ".join(base_vals)})
        ON DUPLICATE KEY UPDATE {", ".join(upd) if upd else "chapters_id=chapters_id"}
    """

    try:
        cur.execute(sql, tuple(params))
        return
    except Exception as e:
        # เผื่อ rh_id ไม่ได้ AUTO_INCREMENT -> ต้องใส่ค่าเอง
        msg = str(e).lower()
        if has_rh_id and ("rh_id" in msg) and ("default" in msg or "doesn't have a default value" in msg):
            new_id = _next_rh_id(cur)
            cols2 = ["rh_id"] + base_cols
            vals2 = ["%s"] + base_vals
            params2 = [new_id] + params

            sql2 = f"""
                INSERT INTO reading_history ({", ".join(cols2)})
                VALUES ({", ".join(vals2)})
                ON DUPLICATE KEY UPDATE {", ".join(upd) if upd else "chapters_id=chapters_id"}
            """
            cur.execute(sql2, tuple(params2))
            return

        raise


# ---------- Read chapter ----------

@reading_bp.route("/read/<int:novels_id>/<int:chapter_no>")
def read_chapter(novels_id: int, chapter_no: int):
    """
    หน้าอ่านตอน:
    - ผู้ใช้อ่านปกติ: เห็นเฉพาะ status='published'
    - Preview: อนุญาตเฉพาะเจ้าของนิยาย
    - prev/next: สำหรับผู้ใช้อ่านปกติจะ "ข้าม" ตอน draft อัตโนมัติ
    - ✅ บันทึก reading_history ทันทีเมื่อเข้าอ่าน (ถ้า login และไม่ใช่ preview)
    """
    conn = None
    try:
        preview_requested = request.args.get("preview", default=0, type=int) == 1
        current_user_id = _get_current_user_id()

        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            ccols = _columns(cur, "chapters")
            has_status = "status" in ccols

            # ---- ดึงข้อมูลตอน + เรื่อง ----
            cur.execute(
                f"""
                SELECT c.chapters_id, c.novels_id, c.title AS chapter_title,
                       c.chapter_no, c.created_at
                       {", c.status" if has_status else ""}
                     , n.title AS novel_title
                     , n.users_id AS author_id
                     , u.username AS author_name
                FROM chapters c
                JOIN novels n ON n.novels_id = c.novels_id
                LEFT JOIN users u ON u.users_id = n.users_id
                WHERE c.novels_id=%s AND c.chapter_no=%s
                LIMIT 1
                """,
                (novels_id, chapter_no),
            )
            row = cur.fetchone()
            if not row:
                abort(404, description="Chapter not found in database")

            # ---- preview permission ----
            is_preview = False
            if preview_requested and current_user_id and row.get("author_id"):
                try:
                    is_preview = int(current_user_id) == int(row["author_id"])
                except Exception:
                    is_preview = False

            # ---- กันคนอ่านปกติไม่ให้เข้าตอน draft ----
            if has_status and (not is_preview):
                if (row.get("status") or "").lower() != "published":
                    abort(404)

            # ✅ touch history เมื่อเข้าอ่าน (นับต่อ chapter)
            if current_user_id and (not is_preview) and _table_exists(cur, "reading_history"):
                try:
                    _upsert_reading_history_per_chapter(
                        cur,
                        user_id=int(current_user_id),
                        novels_id=int(row["novels_id"]),
                        chapters_id=int(row["chapters_id"]),
                        progress=0,
                    )
                    conn.commit()
                except Exception as e:
                    print(f"[reading.touch_history] error: {e}")

            # ---- เนื้อหา ----
            content_html = None
            content_text = None

            if "content_html" in ccols and "content" in ccols:
                cur.execute(
                    "SELECT content_html, content FROM chapters WHERE chapters_id=%s",
                    (row["chapters_id"],),
                )
                r = cur.fetchone() or {}
                content_html = _as_text(r.get("content_html"))
                content_text = _as_text(r.get("content"))
            elif "content_html" in ccols:
                cur.execute(
                    "SELECT content_html FROM chapters WHERE chapters_id=%s",
                    (row["chapters_id"],),
                )
                r = cur.fetchone() or {}
                content_html = _as_text(r.get("content_html"))
            elif "content" in ccols:
                cur.execute(
                    "SELECT content FROM chapters WHERE chapters_id=%s",
                    (row["chapters_id"],),
                )
                r = cur.fetchone() or {}
                content_text = _as_text(r.get("content"))

            if content_html and content_html.strip():
                html_content = _maybe_unescape_html(content_html.strip())
                paragraphs = None
            else:
                html_content = None
                paragraphs = split_paragraphs((content_text or "").strip())

            # ---- prev/next ----
            if has_status and (not is_preview):
                cur.execute(
                    "SELECT MAX(chapter_no) AS prev_no "
                    "FROM chapters "
                    "WHERE novels_id=%s AND chapter_no<%s AND status='published'",
                    (novels_id, chapter_no),
                )
            else:
                cur.execute(
                    "SELECT MAX(chapter_no) AS prev_no "
                    "FROM chapters WHERE novels_id=%s AND chapter_no<%s",
                    (novels_id, chapter_no),
                )
            prev_no = (cur.fetchone() or {}).get("prev_no")

            if has_status and (not is_preview):
                cur.execute(
                    "SELECT MIN(chapter_no) AS next_no "
                    "FROM chapters "
                    "WHERE novels_id=%s AND chapter_no>%s AND status='published'",
                    (novels_id, chapter_no),
                )
            else:
                cur.execute(
                    "SELECT MIN(chapter_no) AS next_no "
                    "FROM chapters WHERE novels_id=%s AND chapter_no>%s",
                    (novels_id, chapter_no),
                )
            next_no = (cur.fetchone() or {}).get("next_no")

            prev_url = url_for("reading.read_chapter", novels_id=novels_id, chapter_no=prev_no) if prev_no is not None else None
            next_url = url_for("reading.read_chapter", novels_id=novels_id, chapter_no=next_no) if next_no is not None else None

            try:
                back_url = url_for("novel.detail", novels_id=novels_id)
            except Exception:
                back_url = "/"

            writing_url = None
            if is_preview:
                try:
                    writing_url = url_for(
                        "writing.writing_form",
                        novels_id=row["novels_id"],
                        chapter_id=row["chapters_id"],
                    )
                except Exception:
                    writing_url = None

        return render_template(
            "readingform.html",
            novels_id=row["novels_id"],
            chapters_id=row["chapters_id"],
            novel_title=row.get("novel_title"),
            chapter_title=row.get("chapter_title"),
            chapter_no=row.get("chapter_no"),
            author_name=row.get("author_name") or "Unknown",
            created_at=row.get("created_at"),
            paragraphs=paragraphs,
            html_content=html_content,
            prev_url=prev_url,
            next_url=next_url,
            back_url=back_url,
            is_preview=is_preview,
            writing_url=writing_url,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"reading.read_chapter error: {e}")
        abort(500)
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


# ---------- API: บันทึก Progress ----------

@reading_bp.route("/api/reading/progress", methods=["POST"])
def save_reading_progress():
    """
    รับ progress จากหน้าอ่านตอน แล้วบันทึกลง reading_history
    - 1 user + 1 novel + 1 chapter = 1 แถว (UNIQUE users_id, novels_id, chapters_id)
    """
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = _get_payload()

    novels_id_raw = _pick(payload, "novels_id", "novel_id", "novelId")
    chapters_id_raw = _pick(payload, "chapters_id", "chapter_id", "chaptersId", "chapterId")
    progress_raw = _pick(payload, "progress", "scroll_percent", "scrollPercent")

    try:
        novels_id = _to_int_strict(novels_id_raw)
        chapters_id = _to_int_strict(chapters_id_raw)
    except Exception:
        return jsonify({
            "ok": False,
            "error": "invalid ids",
            "received": {
                "novels_id": novels_id_raw,
                "chapters_id": chapters_id_raw,
                "progress": progress_raw,
                "content_type": request.headers.get("Content-Type"),
            }
        }), 400

    progress = _to_progress(progress_raw)

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            _upsert_reading_history_per_chapter(
                cur,
                user_id=int(user_id),
                novels_id=int(novels_id),
                chapters_id=int(chapters_id),
                progress=int(progress),
            )
            conn.commit()

    except Exception as e:
        print(f"save_reading_progress error: {e}")
        return jsonify({"ok": False}), 500
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    return jsonify({"ok": True, "progress": progress})
