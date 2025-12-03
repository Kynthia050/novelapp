from flask import (
    Blueprint, render_template, abort, url_for,
    request, jsonify, g, session
)
from werkzeug.exceptions import HTTPException
from MySQLdb.cursors import DictCursor
from db import get_db_connection

reading_bp = Blueprint('reading', __name__, template_folder='templates')


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


def _columns(cur, table: str):
    try:
        cur.execute(f"DESCRIBE {table}")
        return {r['Field'] for r in cur.fetchall()}
    except Exception:
        return set()


def _get_current_user_id():
    """
    ดึง users_id ของผู้ใช้ที่ล็อกอินอยู่
    ปรับให้ตรงกับระบบ auth ของโปรเจกต์คุณ:
    - ถ้าใช้ g.user → ต้องมี g.user["users_id"]
    - ถ้าใช้ session → ต้องมี session["users_id"]
    """
    if hasattr(g, "user") and g.user:
        uid = g.user.get("users_id") or g.user.get("id")
        if uid:
            return int(uid)

    uid = session.get("users_id") or session.get("user_id")
    if uid:
        return int(uid)

    return None


# ---------- Read chapter ----------
@reading_bp.route("/read/<int:novels_id>/<int:chapter_no>")
def read_chapter(novels_id: int, chapter_no: int):
    """หน้าอ่านตอน: เติมตัวแปรที่ template ต้องใช้ + สร้าง prev/next/back"""
    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            # ---- ดึงข้อมูลตอน + เรื่อง ----
            cur.execute(
                """
                SELECT c.chapters_id, c.novels_id, c.title AS chapter_title,
                       c.chapter_no, c.created_at,
                       n.title AS novel_title,
                       u.username AS author_name
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
                # ถ้าไม่เจอตอน → ให้ 404 ปกติ
                abort(404, description="Chapter not found in database")

            # ---- เนื้อหา: รองรับทั้ง content_html และ content ----
            ccols = _columns(cur, "chapters")
            content = None
            if "content_html" in ccols:
                cur.execute(
                    "SELECT content_html FROM chapters WHERE chapters_id=%s",
                    (row["chapters_id"],),
                )
                r = cur.fetchone() or {}
                content = r.get("content_html")
            elif "content" in ccols:
                cur.execute(
                    "SELECT content FROM chapters WHERE chapters_id=%s",
                    (row["chapters_id"],),
                )
                r = cur.fetchone() or {}
                content = r.get("content")

            if content and ("<" in str(content) and ">" in str(content)):
                html_content = content
                paragraphs = None
            else:
                html_content = None
                paragraphs = split_paragraphs(content or "")


            # ---- ปุ่มก่อนหน้า/ถัดไป ----
            cur.execute(
                "SELECT MAX(chapter_no) AS prev_no "
                "FROM chapters WHERE novels_id=%s AND chapter_no<%s",
                (novels_id, chapter_no),
            )
            prev_no = (cur.fetchone() or {}).get("prev_no")

            cur.execute(
                "SELECT MIN(chapter_no) AS next_no "
                "FROM chapters WHERE novels_id=%s AND chapter_no>%s",
                (novels_id, chapter_no),
            )
            next_no = (cur.fetchone() or {}).get("next_no")

            prev_url = (
                url_for("reading.read_chapter", novels_id=novels_id, chapter_no=prev_no)
                if prev_no is not None else None
            )
            next_url = (
                url_for("reading.read_chapter", novels_id=novels_id, chapter_no=next_no)
                if next_no is not None else None
            )

            try:
                back_url = url_for("novel.detail", novels_id=novels_id)
            except Exception:
                back_url = "/"

            # ---- เช็คว่าเป็นโหมด Preview หรือเปล่า ----
            is_preview = request.args.get("preview", default=0, type=int) == 1
            writing_url = None
            if is_preview:
                # กลับไปหน้า writingform ของตอนนี้
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
        # ให้ abort(404) / abort(403) อื่น ๆ ทำงานปกติ
        raise
    except Exception as e:
        print(f"reading.read_chapter error: {e}")
        abort(500)


# ---------- API: บันทึก Progress ----------

@reading_bp.route("/api/reading/progress", methods=["POST"])
def save_reading_progress():
    """
    รับ progress จากหน้าอ่านตอน แล้วบันทึกลง reading_history

    ตาม schema:
      - UNIQUE KEY uk_history_user_novel (users_id, novels_id)
      - 1 user + 1 novel = 1 แถว
    บันทึก:
      - chapters_id ล่าสุด
      - progress ล่าสุด (0-100)
      - last_read_at = CURRENT_TIMESTAMP
    """
    user_id = _get_current_user_id()
    if not user_id:
        # ไม่ล็อกอิน: ให้ 401 แบบเงียบๆ JS จัดการเอง
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # readingform.html ส่งเป็น x-www-form-urlencoded
    novels_id = request.form.get("novels_id") or request.form.get("novel_id")
    chapters_id = request.form.get("chapters_id") or request.form.get("chapter_id")
    progress = request.form.get("progress") or request.form.get("scroll_percent")

    try:
        novels_id = int(novels_id or 0)
        chapters_id = int(chapters_id or 0)
        progress = int(progress or 0)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid data"}), 400

    if not novels_id or not chapters_id:
        return jsonify({"ok": False, "error": "missing ids"}), 403

    # บังคับ 0-100
    if progress < 0:
        progress = 0
    if progress > 100:
        progress = 100

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            sql = """
            INSERT INTO reading_history (users_id, novels_id, chapters_id, progress)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              chapters_id = VALUES(chapters_id),
              progress = VALUES(progress),
              last_read_at = CURRENT_TIMESTAMP
            """
            cur.execute(sql, (user_id, novels_id, chapters_id, progress))
            conn.commit()
    except Exception as e:
        print(f"save_reading_progress error: {e}")
        return jsonify({"ok": False}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return jsonify({"ok": True})
