# novelcover.py
from flask import Blueprint, render_template, abort, url_for, request, redirect, session, flash, g, jsonify
from MySQLdb.cursors import DictCursor
from db import get_db_connection
from flask import current_app
from openai import OpenAI
import os

novel_bp = Blueprint("novel", __name__, template_folder="./templates")


def _has_table(cur, name: str) -> bool:
    try:
        cur.execute(f"DESCRIBE {name}")
        cur.fetchall()
        return True
    except Exception:
        return False


def _has_column(cur, table: str, col: str) -> bool:
    try:
        cur.execute(f"DESCRIBE {table}")
        cols = {r["Field"] for r in cur.fetchall()}
        return col in cols
    except Exception:
        return False


def _writer_sql_parts(cur):
    """เลือกวิธีดึงข้อมูลผู้เขียนจากตาราง novels / users ให้ได้ทั้ง writer_id และ writer_name"""
    try:
        cur.execute("DESCRIBE novels")
        cols = {r["Field"] for r in cur.fetchall()}
    except Exception:
        cols = set()

    # กรณีตาราง novels มีฟิลด์ users_id
    if "users_id" in cols:
        return (
            "u.users_id AS writer_id, u.username AS writer_name",
            "LEFT JOIN users u ON u.users_id = n.users_id",
        )

    # กรณีตาราง novels มีฟิลด์ writer_id
    if "writer_id" in cols:
        return (
            "u.users_id AS writer_id, u.username AS writer_name",
            "LEFT JOIN users u ON u.users_id = n.writer_id",
        )

    # กรณีตาราง novels มีฟิลด์ created_by
    if "created_by" in cols:
        return (
            "u.users_id AS writer_id, u.username AS writer_name",
            "LEFT JOIN users u ON u.users_id = n.created_by",
        )

    # กรณีไม่มีฟิลด์ไหนเลย
    return (
        "NULL AS writer_id, 'ผู้เขียนไม่ระบุ' AS writer_name",
        "",
    )



def _process_cover_url(cover_path: str | None) -> str:
    """แปลง cover ที่เก็บใน DB ให้เป็น URL ที่ใช้ใน <img>"""
    if not cover_path:
        return url_for("static", filename="cover/placeholder.jpg")
    cover_path = str(cover_path)
    if cover_path.startswith(("http://", "https://", "/static/")):
        return cover_path
    filename = os.path.basename(cover_path)
    return url_for("static", filename=f"cover/{filename}")


def _normalize_status(raw: str | None) -> str:
    """คืนสถานะเป็น completed / ongoing สำหรับใช้ใน template"""
    if not raw:
        return "ongoing"
    raw = raw.strip().lower()
    return "completed" if raw in {"completed", "จบแล้ว", "done", "finished", "finish"} else "ongoing"


def _user_profile_parts(cur):
    """
    คืน select username + avatar (pfpic) + join users สำหรับ comments

    โครง users:
      users_id, username, pfpic, ...
    """
    if not _has_table(cur, "users"):
        return ("NULL AS username", "NULL AS profile_image", "")

    sel_username = "u.username AS username"
    sel_avatar = "u.pfpic AS profile_image"
    join_clause = "LEFT JOIN users u ON u.users_id = c.users_id"
    return (sel_username, sel_avatar, join_clause)


def _process_avatar_url(raw: str | None) -> str | None:
    """แปลงค่า pfpic ใน DB ให้เป็น URL ใช้แสดงรูปโปรไฟล์"""
    if not raw:
        return None
    raw = str(raw)

    if raw.startswith(("http://", "https://", "/static/")):
        return raw

    filename = os.path.basename(raw)
    return url_for("static", filename=f"profile/{filename}")


def _current_user_id() -> int | None:
    """ดึง users_id ปัจจุบันจาก session / g.user"""
    # รองรับหลาย key ที่ login ตั้งไว้
    for key in ("users_id", "user_id", "uid"):
        val = session.get(key)
        if val not in (None, ""):
            try:
                return int(val)
            except Exception:
                return None

    u = getattr(g, "user", None)
    if isinstance(u, dict):
        for key in ("users_id", "user_id", "id"):
            val = u.get(key)
            if val not in (None, ""):
                try:
                    return int(val)
                except Exception:
                    return None
    return None

def _is_novel_owner(cur, users_id: int | None, novels_id: int) -> bool:
    """
    คืน True ถ้า users_id เป็นเจ้าของนิยายเรื่องนี้
    (พยายามรองรับทั้งคอลัมน์ users_id / writer_id / created_by)
    """
    if not users_id:
        return False

    try:
        cur.execute("DESCRIBE novels")
        cols = {r["Field"] for r in cur.fetchall()}
        owner_col = None
        for name in ("users_id", "writer_id", "created_by"):
            if name in cols:
                owner_col = name
                break
        if not owner_col:
            return False

        cur.execute(
            f"SELECT {owner_col} AS owner_id FROM novels WHERE novels_id = %s LIMIT 1",
            (novels_id,),
        )
        row = cur.fetchone()
        if not row:
            return False

        return int(row.get("owner_id") or 0) == int(users_id)
    except Exception:
        return False

# ---------- AI summary helpers ----------
AI_FALLBACK_PREFIX = "ไม่สามารถติดต่อบริการสรุปด้วย AI ได้ในขณะนี้"
AI_NOT_CONFIGURED_SUFFIX = "(ยังไม่ได้ตั้งค่า OPENAI_CLIENT ใน app.py)"

# ถ้าข้อความสรุปมีคำพวกนี้ ให้ถือว่าเป็นแคชเสีย/ไม่ควรใช้
BAD_CACHE_MARKERS = (
    "OPENAI_CLIENT",
    "ไม่สามารถเรียกใช้โมเดล AI ได้",
    AI_FALLBACK_PREFIX,
)

def _get_openai_client():
    """
    ดึง OpenAI client จาก app.py แบบทนทาน:
    - รองรับทั้ง app.config และ app.extensions
    """
    return current_app.config.get("OPENAI_CLIENT") or current_app.extensions.get("OPENAI_CLIENT")
# ---------------------------------------


def generate_comment_summary(base_summary, comments, novel_title: str = "") -> str:
    """
    เรียก OpenAI API เพื่อสรุปความคิดเห็นผู้อ่านของนิยายเรื่องหนึ่งจริง ๆ
    - base_summary = สรุปเดิม (ถ้าเคยสรุปแล้ว)
    - comments = list ของคอมเมนต์ใหม่ที่ยังไม่เคยถูกสรุป
    - novel_title = ชื่อเรื่อง (เอาไว้ช่วยให้ model รู้ context)
    """

    # ถ้าไม่มีคอมเมนต์ใหม่เลย แต่มีสรุปเดิมอยู่แล้ว → ส่งสรุปเดิมกลับ
    if (not comments) and base_summary:
        return base_summary

    # ดึง client จาก app.config (เราตั้งไว้ใน app.py แล้ว)
    client: OpenAI = current_app.config.get("OPENAI_CLIENT")
    if client is None:
        fallback = "ไม่สามารถเรียกใช้โมเดล AI ได้ (ยังไม่ได้ตั้งค่า OPENAI_CLIENT ใน app.py)"
        return base_summary + "\n\n" + fallback if base_summary else fallback

    # รวมข้อความคอมเมนต์ใหม่เป็น list
    comment_items = []
    for c in comments:
        text = str(c.get("content") or "").strip()
        if not text:
            continue
        # กันคอมเมนต์ยาวเกินไป
        if len(text) > 400:
            text = text[:400] + "..."
        comment_items.append(f"- {text}")

    if not comment_items and base_summary:
        return base_summary
    elif not comment_items:
        return base_summary or "ยังไม่มีความคิดเห็นจากผู้อ่านเพียงพอสำหรับการสรุป"

    comments_block = "\n".join(comment_items)

    # ใช้ instructions (ภาษาอังกฤษ) + input (มีไทยได้เต็ม ๆ)
    # เพื่อลดโอกาสเจอ bug encoding แปลก ๆ
    instructions = (
        "You are an assistant that summarizes reader comments for an online novel. "
        "You can read Thai and English comments and you must answer in Thai. "
        "Summarize the key sentiments (what readers like, dislike, and suggestions) "
        "into short message a few lines in Thai and do not spoil the story."
        "Example output format:\n"
        "- ความคิดเห็นที่สำคัญ: [สรุปสั้น ๆ]\n"
        "- ความคิดเห็นอื่น ๆ: [สรุปสั้น ๆ]"
    )

    if novel_title:
        title_part = f"นิยายเรื่อง: {novel_title}\n"
    else:
        title_part = ""

    if base_summary:
        user_prompt = (
            f"{title_part}"
            "นี่คือสรุปเดิมจากความคิดเห็นก่อนหน้า:\n"
            f"{base_summary}\n\n"
            "และนี่คือความคิดเห็นใหม่ที่เพิ่งเพิ่มเข้ามา:\n"
            f"{comments_block}\n\n"
            "โปรดสร้างสรุปฉบับอัปเดตที่รวมทั้งสรุปเดิมและความคิดเห็นใหม่ "
            "ให้ตอบเป็นภาษาไทยเท่านั้น แบ่งบรรทัดให้อ่านง่าย"
        )
    else:
        user_prompt = (
            f"{title_part}"
            "นี่คือความคิดเห็นจากผู้อ่านนิยายเรื่องนี้:\n"
            f"{comments_block}\n\n"
            "โปรดสรุปความคิดเห็นของผู้อ่านจากข้อความทั้งหมดด้านบน "
            "ให้เป็นภาษาไทยสั้น ๆ แบ่งเป็นหลายบรรทัดอ่านง่าย"
        )

    try:
        # ใช้รูปแบบเรียกตาม docs: instructions + input (string เดียว)
        # ตัวอย่างจากเอกสาร:
        #   client.responses.create(model="gpt-4o-mini", instructions="...", input="...")
        # อ้างอิง: GitHub openai-python :contentReference[oaicite:0]{index=0}
        response = client.responses.create(
            model="gpt-4o-mini",  # หรือรุ่นอื่นที่คุณมีสิทธิ์ใช้ เช่น gpt-4.1-mini
            instructions=instructions,
            input=user_prompt,
        )

        # ไลบรารีใหม่จะมี helper ชื่อ output_text สำหรับ text ล้วน
        summary_text = (getattr(response, "output_text", None) or "").strip()

        # กันเคสที่ output_text ไม่มี (เผื่อใช้เวอร์ชันอื่น)
        if not summary_text and hasattr(response, "output"):
            try:
                summary_text = response.output[0].content[0].text.strip()
            except Exception:
                pass

        if not summary_text:
            return base_summary or "ไม่สามารถสร้างสรุปความคิดเห็นได้ในขณะนี้"

        return summary_text

    except Exception as e:
        # log แบบไม่ไปชน encoding error (ใช้ repr)
        print("[generate_comment_summary] OpenAI error type:", type(e), "detail:", repr(e))
        fallback = "ไม่สามารถติดต่อบริการสรุปด้วย AI ได้ในขณะนี้ โปรดลองใหม่อีกครั้งภายหลัง"
        return base_summary + "\n\n" + fallback if base_summary else fallback



# ---------- route main: /novel/<novels_id> ----------

@novel_bp.route("/novel/<int:novels_id>", methods=["GET", "POST"])
def detail(novels_id: int):
    # เช็คว่าเป็น AJAX comment หรือไม่ (ใช้กับ JS fetch)
    is_ajax_comment = (
        request.method == "POST"
        and request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest"
    )
    # cm_id สำหรับโหมดแก้ไขความคิดเห็น (ถ้ามี)
    try:
        comment_id_for_update = int(request.form.get("comment_id") or 0)
    except Exception:
        comment_id_for_update = 0

    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:

            # ==================== POST: ส่งความคิดเห็น ====================
            if request.method == "POST":
                content = (request.form.get("content") or "").strip()

                # กันยิงตรง ๆ ยาวเกิน
                if len(content) > 500:
                    content = content[:500]

                if not content:
                    msg = "กรุณาพิมพ์ความคิดเห็นก่อนส่ง"
                    if is_ajax_comment:
                        return jsonify({"ok": False, "error": msg}), 400
                    flash(msg, "error")
                    return redirect(url_for("novel.detail", novels_id=novels_id))

                users_id = _current_user_id()
                if not users_id:
                    msg = "กรุณาเข้าสู่ระบบก่อนแสดงความคิดเห็น"
                    if is_ajax_comment:
                        return jsonify(
                            {"ok": False, "error": msg, "need_login": True}
                        ), 401
                    flash(msg, "error")
                    return redirect(url_for("novel.detail", novels_id=novels_id))

                if not _has_table(cur, "comments"):
                    msg = "ไม่พบตาราง comments ในฐานข้อมูล"
                    if is_ajax_comment:
                        return jsonify({"ok": False, "error": msg}), 500
                    flash(msg, "error")
                    return redirect(url_for("novel.detail", novels_id=novels_id))

                # ---------- Insert หรือ Update comment ----------
                is_editing = comment_id_for_update > 0
                target_cm_id = None

                if is_editing:
                    cur.execute(
                        """
                        SELECT cm_id, users_id, novels_id
                        FROM comments
                        WHERE cm_id = %s
                        LIMIT 1
                        """,
                        (comment_id_for_update,),
                    )
                    row = cur.fetchone()
                    if (not row) or int(row.get("novels_id") or 0) != novels_id:
                        msg = "ไม่พบบันทึกความคิดเห็นนี้"
                        if is_ajax_comment:
                            return jsonify({"ok": False, "error": msg}), 404
                        flash(msg, "error")
                        return redirect(url_for("novel.detail", novels_id=novels_id))

                    comment_owner_id = int(row.get("users_id") or 0)
                    if users_id != comment_owner_id:
                        msg = "คุณสามารถแก้ไขความคิดเห็นของตนเองเท่านั้น"
                        if is_ajax_comment:
                            return jsonify({"ok": False, "error": msg}), 403
                        flash(msg, "error")
                        return redirect(url_for("novel.detail", novels_id=novels_id))

                    cur.execute(
                        """
                        UPDATE comments
                        SET content = %s
                        WHERE cm_id = %s
                        """,
                        (content, comment_id_for_update),
                    )
                    target_cm_id = comment_id_for_update
                else:
                    cur.execute(
                        """
                        INSERT INTO comments (users_id, novels_id, content)
                        VALUES (%s, %s, %s)
                        """,
                        (users_id, novels_id, content),
                    )
                    target_cm_id = cur.lastrowid

                # ทำให้ summary เป็น dirty (ให้ไปสรุปใหม่)
                if _has_table(cur, "comment_summaries"):
                    cur.execute(
                        """
                        INSERT INTO comment_summaries (novels_id, summary_text, last_cm_id, dirty)
                        VALUES (%s, NULL, NULL, 1)
                        ON DUPLICATE KEY UPDATE dirty = 1
                        """,
                        (novels_id,),
                    )

                conn.commit()

                # ----- ถ้าเป็น AJAX → ส่ง JSON กลับ -----
                if is_ajax_comment:
                    sel_username, sel_avatar, join_users = _user_profile_parts(cur)
                    cur.execute(
                        f"""
                        SELECT c.cm_id,
                               c.users_id,
                               c.novels_id,
                               c.content,
                               c.created_at,
                               {sel_username},
                               {sel_avatar}
                        FROM comments c
                        {join_users}
                        WHERE c.cm_id = %s
                        LIMIT 1
                        """,
                        (target_cm_id,),
                    )
                    cm = cur.fetchone()
                    if not cm:
                        # insert ได้แต่ select ไม่เจอ -> ส่งข้อความธรรมดากลับไป
                        return jsonify(
                            {
                                "ok": True,
                                "message": "ส่งความคิดเห็นเรียบร้อยแล้ว",
                            }
                        )

                    cm["avatar_url"] = _process_avatar_url(cm.get("profile_image"))
                    current_uid = users_id
                    is_owner = _is_novel_owner(cur, current_uid, novels_id)
                    can_delete = bool(
                        current_uid
                        and (current_uid == cm.get("users_id") or is_owner)
                    )
                    can_edit = bool(current_uid and current_uid == cm.get("users_id"))

                    display_name = cm.get("username") or f"ผู้ใช้ #{cm['users_id']}"
                    created_display = (
                        cm["created_at"].strftime("%d/%m/%Y")
                        if cm.get("created_at")
                        else "ไม่ระบุวันที่"
                    )

                    return jsonify(
                        {
                            "ok": True,
                            "message": "แก้ไขความคิดเห็นสำเร็จ" if is_editing else "ส่งความคิดเห็นเรียบร้อยแล้ว",
                            "comment": {
                                "cm_id": cm["cm_id"],
                                "content": cm["content"],
                                "username": display_name,
                                "avatar_url": cm["avatar_url"],
                                "created_at": created_display,
                                "can_delete": can_delete,
                                "can_edit": can_edit,
                            },
                        }
                    ), 200 if is_editing else 201

                # ----- ไม่ใช่ AJAX → redirect ตามปกติ -----
                flash("แก้ไขความคิดเห็นสำเร็จ" if is_editing else "ส่งความคิดเห็นเรียบร้อยแล้ว", "success")
                return redirect(url_for("novel.detail", novels_id=novels_id))

            # ==================== GET: โหลดข้อมูลหน้า novel cover ====================

            # sort ตอน เก่า→ใหม่ / ใหม่→เก่า
            sort = request.args.get("sort", "asc")
            if sort not in ("asc", "desc"):
                sort = "asc"
            order_dir = "ASC" if sort == "asc" else "DESC"

            novel_tags = []

            # ---------- โหลดข้อมูลนิยาย ----------
            has_views_col = _has_column(cur, "novels", "views")
            sel_writer, join_writer = _writer_sql_parts(cur)
            novel_cols = [
                "n.novels_id",
                "n.title",
                "n.description",
                "n.status",
                "n.cover",
                "n.updated_at",
                "n.cate_id",
                "c.name AS category_name",
                sel_writer,
            ]
            if has_views_col:
                novel_cols.append("COALESCE(n.views, 0) AS total_views")

            cur.execute(
                f"""
                SELECT
                    {", ".join(novel_cols)}
                FROM novels n
                LEFT JOIN categories c ON c.cate_id = n.cate_id
                {join_writer}
                WHERE n.novels_id = %s
                """,
                (novels_id,),
            )

            novel = cur.fetchone()
            if not novel:
                abort(404, description="ไม่พบนิยายที่ระบุ")

            novel["status"] = _normalize_status(novel.get("status"))
            novel["cover_url"] = _process_cover_url(novel.get("cover"))
            if has_views_col:
                try:
                    novel["total_views"] = int(novel.get("total_views") or 0)
                except Exception:
                    novel["total_views"] = 0
            else:
                novel["total_views"] = 0

            uid = _current_user_id()

            # --- bookshelf state และยอดรวม ---
            has_bookshelf = _has_table(cur, "bookshelf")
            novel["in_bookshelf"] = False
            novel["bookshelf_count"] = 0

            if has_bookshelf:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM bookshelf WHERE novels_id = %s",
                    (novels_id,),
                )
                novel["bookshelf_count"] = int((cur.fetchone() or {}).get("c") or 0)

            if uid and has_bookshelf:
                cur.execute(
                    """
                    SELECT 1
                    FROM bookshelf
                    WHERE users_id = %s AND novels_id = %s
                    LIMIT 1
                    """,
                    (uid, novels_id),
                )
                novel["in_bookshelf"] = cur.fetchone() is not None

            # --- จำนวน favorite / bookmark / bookshelf ทั้งหมดของเรื่อง ---
            novel["total_favorites"] = 0
            if has_bookshelf:
                novel["total_favorites"] = novel.get("bookshelf_count", 0)
            elif _has_table(cur, "favorites"):
                cur.execute(
                    "SELECT COUNT(*) AS c FROM favorites WHERE novels_id = %s",
                    (novels_id,),
                )
                novel["total_favorites"] = int((cur.fetchone() or {}).get("c") or 0)
            elif _has_table(cur, "bookmarks"):
                cur.execute(
                    "SELECT COUNT(*) AS c FROM bookmarks WHERE novels_id = %s",
                    (novels_id,),
                )
                novel["total_favorites"] = int((cur.fetchone() or {}).get("c") or 0)

            # --- แท็กของนิยายเรื่องนี้ ---
            if _has_table(cur, "novels_tags"):
                if _has_table(cur, "tags"):
                    # ถ้าตาราง tags ของคุณใช้ชื่อคอลัมน์อื่น (เช่น tag_name)
                    # ให้เปลี่ยน t.name ใน SELECT ด้านล่างให้ตรงกับคอลัมน์จริง
                    cur.execute(
                        """
                        SELECT nt.tag_id,
                               COALESCE(t.name, CONCAT('แท็ก ', nt.tag_id)) AS tag_name
                        FROM novels_tags nt
                        JOIN tags t ON t.tag_id = nt.tag_id
                        WHERE nt.novels_id = %s
                        ORDER BY t.name
                        """,
                        (novels_id,),
                    )
                    novel_tags = cur.fetchall()
                else:
                    # fallback ถ้าไม่มีตาราง tags แยก ใช้ tag_id เป็นชื่อชั่วคราว
                    cur.execute(
                        """
                        SELECT tag_id,
                               CONCAT('แท็ก ', tag_id) AS tag_name
                        FROM novels_tags
                        WHERE novels_id = %s
                        ORDER BY tag_id
                        """,
                        (novels_id,),
                    )
                    novel_tags = cur.fetchall()

            # --- ratings (ถ้ามีตาราง ratings) ---
            novel["avg_rating"] = 0.0
            novel["rating_count"] = 0
            novel["user_rating"] = 0

            if _has_table(cur, "ratings"):
                cur.execute(
                    """
                    SELECT AVG(rating) AS avg_rating,
                           COUNT(*)    AS rating_count
                    FROM ratings
                    WHERE novels_id = %s
                    """,
                    (novels_id,),
                )
                row = cur.fetchone() or {}
                try:
                    novel["avg_rating"] = float(row.get("avg_rating") or 0.0)
                except (TypeError, ValueError):
                    novel["avg_rating"] = 0.0
                try:
                    novel["rating_count"] = int(row.get("rating_count") or 0)
                except (TypeError, ValueError):
                    novel["rating_count"] = 0

                if uid:
                    cur.execute(
                        """
                        SELECT rating
                        FROM ratings
                        WHERE novels_id = %s AND users_id = %s
                        LIMIT 1
                        """,
                        (novels_id, uid),
                    )
                    r = cur.fetchone()
                    try:
                        novel["user_rating"] = (
                            int(r["rating"])
                            if r and r.get("rating") is not None
                            else 0
                        )
                    except (TypeError, ValueError):
                        novel["user_rating"] = 0

            # ---------- readers ----------
            novel["total_readers"] = 0
            if _has_table(cur, "reading_history") and _has_column(
                cur, "reading_history", "users_id"
            ):
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT users_id) AS c
                    FROM reading_history
                    WHERE novels_id = %s
                    """,
                    (novels_id,),
                )
                novel["total_readers"] = int((cur.fetchone() or {}).get("c") or 0)
            elif _has_table(cur, "novel_reads"):
                if _has_column(cur, "novel_reads", "users_id"):
                    cur.execute(
                        """
                        SELECT COUNT(DISTINCT users_id) AS c
                        FROM novel_reads
                        WHERE novels_id = %s
                        """,
                        (novels_id,),
                    )
                else:
                    cur.execute(
                        "SELECT COUNT(*) AS c FROM novel_reads WHERE novels_id = %s",
                        (novels_id,),
                    )
                novel["total_readers"] = int((cur.fetchone() or {}).get("c") or 0)

            if not has_views_col:
                novel["total_views"] = novel.get("total_readers", 0)
            try:
                novel["total_views"] = int(novel.get("total_views") or 0)
            except Exception:
                novel["total_views"] = 0

            # ---------- chapters + like count ----------
            chap_pk = "chapters_id"
            try:
                cur.execute("DESCRIBE chapters")
                ccols = {r["Field"] for r in cur.fetchall()}
                if "chapters_id" in ccols:
                    chap_pk = "chapters_id"
                elif "chapter_id" in ccols:
                    chap_pk = "chapter_id"
            except Exception:
                pass

            like_sel = "0 AS like_count"
            like_join = ""
            group_by = ""

            if _has_column(cur, "chapters", "like_count"):
                like_sel = "COALESCE(c.like_count, 0) AS like_count"
            elif _has_table(cur, "chapter_likes"):
                fk = None
                try:
                    cur.execute("DESCRIBE chapter_likes")
                    lcols = {r["Field"] for r in cur.fetchall()}
                    if "chapters_id" in lcols:
                        fk = "chapters_id"
                    elif "chapter_id" in lcols:
                        fk = "chapter_id"
                except Exception:
                    fk = None

                if fk:
                    like_sel = f"COUNT(cl.{fk}) AS like_count"
                    like_join = f"LEFT JOIN chapter_likes cl ON cl.{fk} = c.{chap_pk}"
                    group_by = f"GROUP BY c.{chap_pk}"

            cur.execute(
                f"""
                SELECT
                    c.{chap_pk} AS chapters_id,
                    c.chapter_no,
                    c.title,
                    c.created_at,
                    {like_sel}
                FROM chapters c
                {like_join}
                WHERE c.novels_id = %s
                  AND c.status = 'published'
                {group_by}
                ORDER BY c.chapter_no {order_dir}, c.{chap_pk} {order_dir}
                """,
                (novels_id,),
            )
            chapters = cur.fetchall()
            novel["total_chapters"] = len(chapters)

            liked_set = set()
            if uid and _has_table(cur, "chapter_likes") and chapters:
                cur.execute(
                    """
                    SELECT chapters_id
                    FROM chapter_likes
                    WHERE users_id = %s
                    """,
                    (uid,),
                )
                for r in cur.fetchall():
                    cid = r.get("chapters_id")
                    if cid is not None:
                        liked_set.add(cid)

            read_set = set()
            if uid and _has_table(cur, "reading_history") and chapters:
                if (
                    _has_column(cur, "reading_history", "chapters_id")
                    and _has_column(cur, "reading_history", "novels_id")
                    and _has_column(cur, "reading_history", "users_id")
                ):
                    cur.execute(
                        """
                        SELECT chapters_id
                        FROM reading_history
                        WHERE users_id = %s AND novels_id = %s
                        """,
                        (uid, novels_id),
                    )
                    for r in cur.fetchall():
                        cid = r.get("chapters_id")
                        if cid is None:
                            continue
                        try:
                            read_set.add(int(cid))
                        except Exception:
                            read_set.add(cid)

            for ch in chapters:
                ch["like_count"] = int(ch.get("like_count") or 0)
                ch["is_liked"] = ch.get("chapters_id") in liked_set
                ch["is_read"] = ch.get("chapters_id") in read_set

            # ---------- comments + can_delete ----------
            comments = []
            if _has_table(cur, "comments"):
                sel_username, sel_avatar, join_users = _user_profile_parts(cur)
                cur.execute(
                    f"""
                    SELECT
                        c.cm_id,
                        c.users_id,
                        c.novels_id,
                        c.content,
                        c.created_at,
                        {sel_username},
                        {sel_avatar}
                    FROM comments c
                    {join_users}
                    WHERE c.novels_id = %s
                    ORDER BY c.created_at DESC
                    """,
                    (novels_id,),
                )
                comments = cur.fetchall()

                current_uid = _current_user_id()
                is_owner = _is_novel_owner(cur, current_uid, novels_id)

                for cm in comments:
                    cm["avatar_url"] = _process_avatar_url(cm.get("profile_image"))
                    cm["can_delete"] = bool(
                        current_uid
                        and (current_uid == cm.get("users_id") or is_owner)
                    )
                    cm["can_edit"] = bool(current_uid and current_uid == cm.get("users_id"))

        return render_template(
            "novelcover.html",
            novel=novel,
            chapters=chapters,
            novel_tags=novel_tags,
            comments=comments,
        )

    except Exception as e:
        print(f"[novel.detail] error: {e}")
        abort(500)


@novel_bp.route("/writerwork")
def writerwork():
    return render_template("writerwork.html")


@novel_bp.route("/novel/<int:novels_id>/bookshelf", methods=["POST"])
def toggle_bookshelf(novels_id: int):
    user_id = _current_user_id()
    if not user_id:
        return jsonify(ok=False, error="login_required"), 401

    conn = get_db_connection()
    try:
        with conn.cursor(DictCursor) as cur:
            # ????????????????????
            cur.execute(
                "SELECT 1 FROM bookshelf WHERE users_id=%s AND novels_id=%s LIMIT 1",
                (user_id, novels_id),
            )
            exists = cur.fetchone() is not None

            if exists:
                cur.execute(
                    "DELETE FROM bookshelf WHERE users_id=%s AND novels_id=%s",
                    (user_id, novels_id),
                )
                conn.commit()
                cur.execute(
                    "SELECT COUNT(*) AS c FROM bookshelf WHERE novels_id=%s",
                    (novels_id,),
                )
                cnt = int((cur.fetchone() or {}).get("c") or 0)
                return jsonify(ok=True, in_bookshelf=False, count=cnt)

            # ?????????????
            cur.execute(
                """
                INSERT INTO bookshelf (users_id, novels_id, created_at)
                VALUES (%s, %s, NOW())
                ON DUPLICATE KEY UPDATE created_at = VALUES(created_at)
                """,
                (user_id, novels_id),
            )
            conn.commit()
            cur.execute(
                "SELECT COUNT(*) AS c FROM bookshelf WHERE novels_id=%s",
                (novels_id,),
            )
            cnt = int((cur.fetchone() or {}).get("c") or 0)
            return jsonify(ok=True, in_bookshelf=True, count=cnt)

    finally:
        conn.close()

@novel_bp.route("/novel/<int:novels_id>/comment-summary", methods=["POST"])
def comment_summary(novels_id: int):
    """
    คืนสรุปความคิดเห็นของนิยายเรื่องหนึ่งในรูปแบบ JSON

    - ถ้ามีสรุปเก่าและ dirty = 0 → ส่งสรุปเก่าจาก DB เลย (from_cache = True)
    - ถ้ายังไม่เคยสรุป หรือ dirty = 1 → ดึงคอมเมนต์ใหม่แล้วสร้างสรุปใหม่
      และอัปเดตตาราง comment_summaries ให้ตรงกับสรุปล่าสุด
    """
    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            # ต้องมีตาราง comments ก่อนถึงจะสรุปได้
            if not _has_table(cur, "comments"):
                return jsonify({
                    "ok": False,
                    "error": "ยังไม่พบตาราง comments ในฐานข้อมูล"
                }), 400

            # อ่านชื่อเรื่อง (ใช้ช่วย context ให้โมเดล)
            novel_title = ""
            try:
                cur.execute(
                    "SELECT title FROM novels WHERE novels_id = %s LIMIT 1",
                    (novels_id,),
                )
                row = cur.fetchone()
                if row and row.get("title"):
                    novel_title = str(row["title"])
            except Exception:
                novel_title = ""

            has_summary_table = _has_table(cur, "comment_summaries")
            summary_row = None
            if has_summary_table:
                cur.execute(
                    """
                    SELECT summary_text, last_cm_id, dirty
                    FROM comment_summaries
                    WHERE novels_id = %s
                    LIMIT 1
                    """,
                    (novels_id,),
                )
                summary_row = cur.fetchone()

            # คำขึ้นต้นของข้อความ fallback เวลาเรียก AI ไม่ได้
            fallback_prefix = "ไม่สามารถติดต่อบริการสรุปด้วย AI ได้ในขณะนี้"

            base_summary = None
            last_cm_id = 0
            dirty = 1  # ถ้าไม่มี row เลยให้ถือว่าสกปรก (ต้องสรุปใหม่)

            if summary_row:
                base_summary = summary_row.get("summary_text") or None
                try:
                    last_cm_id = int(summary_row.get("last_cm_id") or 0)
                except (TypeError, ValueError):
                    last_cm_id = 0
                try:
                    dirty = int(summary_row.get("dirty") or 1)
                except (TypeError, ValueError):
                    dirty = 1

                # ถ้า summary เดิมเป็นข้อความ fallback ให้ถือว่าไม่มี base_summary
                if base_summary and str(base_summary).strip().startswith(fallback_prefix):
                    base_summary = None

            # ถ้ามีสรุปเดิมและไม่ dirty → ส่ง cache ได้เลย
            if base_summary and dirty == 0:
                return jsonify({
                    "ok": True,
                    "summary": base_summary,
                    "from_cache": True,
                })

            # ต้องสรุปใหม่ (ครั้งแรก หรือมีคอมเมนต์เปลี่ยน)
            # ถ้ามี base_summary + last_cm_id → ดึงเฉพาะคอมเมนต์ใหม่
            if base_summary and last_cm_id > 0:
                cur.execute(
                    """
                    SELECT cm_id, content
                    FROM comments
                    WHERE novels_id = %s
                      AND cm_id > %s
                    ORDER BY cm_id ASC
                    """,
                    (novels_id, last_cm_id),
                )
            else:
                # ยังไม่เคยสรุป → ดึงคอมเมนต์ทั้งหมดของนิยายเรื่องนี้
                cur.execute(
                    """
                    SELECT cm_id, content
                    FROM comments
                    WHERE novels_id = %s
                    ORDER BY cm_id ASC
                    """,
                    (novels_id,),
                )
            new_comments = cur.fetchall()

            # ถ้าไม่มีคอมเมนต์ใหม่เลย แต่มี base_summary อยู่แล้ว
            # ให้ mark dirty=0 แล้วส่งสรุปเดิมกลับ
            if not new_comments and base_summary:
                if has_summary_table:
                    cur.execute(
                        "UPDATE comment_summaries SET dirty = 0 WHERE novels_id = %s",
                        (novels_id,),
                    )
                    conn.commit()
                return jsonify({
                    "ok": True,
                    "summary": base_summary,
                    "from_cache": True,
                })

            # เรียกฟังก์ชัน generate_comment_summary (เรียก AI ตามที่ตั้งค่าใน app.py)
            new_summary = generate_comment_summary(base_summary, new_comments, novel_title=novel_title)

            # หา cm_id สูงสุดที่จะถือว่าอยู่ในสรุปนี้
            new_last_cm_id = last_cm_id
            for row in new_comments:
                try:
                    cid = int(row.get("cm_id") or 0)
                    if cid > new_last_cm_id:
                        new_last_cm_id = cid
                except (TypeError, ValueError):
                    pass

            # อัปเดต/สร้าง row ใน comment_summaries
            if has_summary_table:
                is_fallback = str(new_summary or "").strip().startswith(fallback_prefix)

                if summary_row:
                    if is_fallback:
                        # อย่าเขียนทับสรุปเก่าด้วย fallback; แค่ mark ว่ายังต้องสรุปใหม่
                        cur.execute(
                            "UPDATE comment_summaries SET dirty = 1 WHERE novels_id = %s",
                            (novels_id,),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE comment_summaries
                            SET summary_text = %s,
                                last_cm_id   = %s,
                                dirty        = 0
                            WHERE novels_id = %s
                            """,
                            (new_summary, new_last_cm_id or None, novels_id),
                        )
                else:
                    if is_fallback:
                        cur.execute(
                            """
                            INSERT INTO comment_summaries (novels_id, summary_text, last_cm_id, dirty)
                            VALUES (%s, NULL, NULL, 1)
                            """,
                            (novels_id,),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO comment_summaries (novels_id, summary_text, last_cm_id, dirty)
                            VALUES (%s, %s, %s, 0)
                            """,
                            (novels_id, new_summary, new_last_cm_id or None),
                        )
                conn.commit()

            return jsonify({
                "ok": True,
                "summary": new_summary,
                "from_cache": False,
            })
    except Exception as e:
        print(f"[novel.comment_summary] error: {e}")
        return jsonify({
            "ok": False,
            "error": "เกิดข้อผิดพลาดจากเซิร์ฟเวอร์"
        }), 500



# ---------- route สำหรับให้ดาว / บันทึก rating ----------

@novel_bp.route("/novel/<int:novels_id>/rate", methods=["POST"])
def rate(novels_id: int):
    is_ajax = request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest"

    rating_raw = (request.form.get("rating") or "").strip()
    try:
        rating = int(rating_raw)
    except (TypeError, ValueError):
        rating = 0

    if rating < 1 or rating > 5:
        msg = "คะแนนต้องอยู่ระหว่าง 1–5 ดาว"
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("novel.detail", novels_id=novels_id))

    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            if not _has_table(cur, "ratings"):
                msg = "ยังไม่พบตาราง ratings ในฐานข้อมูล"
                if is_ajax:
                    return jsonify({"ok": False, "error": msg}), 500
                flash(msg, "error")
                return redirect(url_for("novel.detail", novels_id=novels_id))

            users_id = _current_user_id()
            if not users_id:
                msg = "กรุณาเข้าสู่ระบบก่อนให้คะแนน"
                if is_ajax:
                    return jsonify({"ok": False, "error": msg, "need_login": True}), 401
                flash(msg, "error")
                return redirect(url_for("novel.detail", novels_id=novels_id))

            # update / insert rating
            cur.execute(
                """
                SELECT rating
                FROM ratings
                WHERE novels_id = %s AND users_id = %s
                LIMIT 1
                """,
                (novels_id, users_id),
            )
            row = cur.fetchone()

            rating_inserted = False
            rating_id = None

            if row:
                cur.execute(
                    """
                    UPDATE ratings
                    SET rating = %s
                    WHERE novels_id = %s AND users_id = %s
                    """,
                    (rating, novels_id, users_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO ratings (users_id, novels_id, rating)
                    VALUES (%s, %s, %s)
                    """,
                    (users_id, novels_id, rating),
                )

                rating_inserted = True
                rating_id = getattr(cur, "lastrowid", None)

            # คำนวณค่าเฉลี่ยใหม่
            if rating_inserted and rating_id and _has_table(cur, "notifications"):
                cur.execute(
                    "SELECT users_id, title FROM novels WHERE novels_id = %s",
                    (novels_id,),
                )
                nrow = cur.fetchone() or {}
                author_id = nrow.get("users_id")
                novel_title = (nrow.get("title") or "").strip()

                if author_id and int(author_id) != int(users_id):
                    cur.execute(
                        """
                        SELECT notification_id
                        FROM notifications
                        WHERE users_id = %s AND type = 'rating' AND reference_id = %s
                        LIMIT 1
                        """,
                        (author_id, rating_id),
                    )
                    if not cur.fetchone():
                        message = "นิยายของคุณได้รับการให้คะแนนใหม่"
                        if novel_title:
                            message = f"นิยายของคุณได้รับการให้คะแนนใหม่: {novel_title}"

                        cur.execute(
                            """
                            INSERT INTO notifications (
                              users_id, actor_user_id, type, novel_id, reference_id, message, is_read, created_at
                            ) VALUES (%s, %s, 'rating', %s, %s, %s, 0, NOW())
                            """,
                            (author_id, users_id, novels_id, rating_id, message),
                        )

            cur.execute(
                """
                SELECT AVG(rating) AS avg_rating,
                       COUNT(*) AS rating_count
                FROM ratings
                WHERE novels_id = %s
                """,
                (novels_id,),
            )
            agg = cur.fetchone() or {}
            avg_rating = float(agg.get("avg_rating") or 0.0)
            rating_count = int(agg.get("rating_count") or 0)

            conn.commit()

            if is_ajax:
                avg_text = "—" if rating_count == 0 else f"{avg_rating:.1f}"
                return jsonify({
                    "ok": True,
                    "message": "บันทึกคะแนนเรียบร้อยแล้ว",
                    "avg_rating": avg_rating,
                    "avg_rating_text": avg_text,
                    "rating_count": rating_count,
                    "user_rating": rating,
                })

            flash("บันทึกคะแนนเรียบร้อยแล้ว", "success")

    except Exception as e:
        print(f"[novel.rate] error: {e}")
        msg = "เกิดข้อผิดพลาดขณะบันทึกคะแนน"
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 500
        flash(msg, "error")

    return redirect(url_for("novel.detail", novels_id=novels_id))


@novel_bp.route("/novel/<int:novels_id>/chapter/<int:chapters_id>/like", methods=["POST"])
def toggle_chapter_like(novels_id: int, chapters_id: int):
    """กด/ยกเลิกหัวใจให้ตอน (toggle)"""
    sort = request.form.get("next_sort") or request.args.get("sort", "asc")
    is_ajax = request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest"

    users_id = _current_user_id()
    if not users_id:
        msg = "กรุณาเข้าสู่ระบบก่อนกดหัวใจ"
        if is_ajax:
            return jsonify({"ok": False, "error": msg, "need_login": True}), 401
        flash(msg, "error")
        return redirect(url_for("novel.detail", novels_id=novels_id, sort=sort))

    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            if not _has_table(cur, "chapter_likes"):
                msg = "ยังไม่พบตาราง chapter_likes ในฐานข้อมูล"
                if is_ajax:
                    return jsonify({"ok": False, "error": msg}), 500
                flash(msg, "error")
                return redirect(url_for("novel.detail", novels_id=novels_id, sort=sort))

            # ตรวจสอบว่าตอนนี้อยู่ในนิยายนี้จริง ๆ
            cur.execute(
                "SELECT novels_id FROM chapters WHERE chapters_id = %s LIMIT 1",
                (chapters_id,),
            )
            row = cur.fetchone()
            if not row or int(row["novels_id"]) != novels_id:
                msg = "ไม่พบตอนที่ต้องการ"
                if is_ajax:
                    return jsonify({"ok": False, "error": msg}), 404
                flash(msg, "error")
                return redirect(url_for("novel.detail", novels_id=novels_id, sort=sort))

            # เคยกดหัวใจหรือยัง
            cur.execute(
                """
                SELECT 1
                FROM chapter_likes
                WHERE chapters_id = %s AND users_id = %s
                LIMIT 1
                """,
                (chapters_id, users_id),
            )
            already = cur.fetchone() is not None

            if already:
                cur.execute(
                    """
                    DELETE FROM chapter_likes
                    WHERE chapters_id = %s AND users_id = %s
                    """,
                    (chapters_id, users_id),
                )
                liked = False
                if not is_ajax:
                    flash("ยกเลิกหัวใจตอนนี้แล้ว", "info")
            else:
                cur.execute(
                    """
                    INSERT INTO chapter_likes (chapters_id, users_id)
                    VALUES (%s, %s)
                    """,
                    (chapters_id, users_id),
                )
                liked = True
                if not is_ajax:
                    flash("ขอบคุณที่กดหัวใจให้ตอนนี้", "success")

            # นับ like ล่าสุด
            cur.execute(
                "SELECT COUNT(*) AS c FROM chapter_likes WHERE chapters_id = %s",
                (chapters_id,),
            )
            like_count = int((cur.fetchone() or {}).get("c") or 0)

            conn.commit()

    except Exception as e:
        print(f"[novel.toggle_chapter_like] error: {e}")
        msg = "เกิดข้อผิดพลาดขณะบันทึกหัวใจ"
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 500
        flash(msg, "error")
        return redirect(url_for("novel.detail", novels_id=novels_id, sort=sort))

    if is_ajax:
        return jsonify({
            "ok": True,
            "liked": liked,
            "like_count": like_count,
        })

    return redirect(url_for("novel.detail", novels_id=novels_id, sort=sort))


@novel_bp.route("/novel/<int:novels_id>/comment/<int:cm_id>/delete", methods=["POST"])
def delete_comment(novels_id: int, cm_id: int):
    """
    ลบความคิดเห็น:
      - เจ้าของคอมเมนต์ลบของตัวเองได้
      - เจ้าของนิยายลบคอมเมนต์ใด ๆ ของเรื่องตัวเองได้
    """
    is_ajax = request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest"
    users_id = _current_user_id()
    if not users_id:
        msg = "กรุณาเข้าสู่ระบบก่อนลบความคิดเห็น"
        if is_ajax:
            return jsonify({"ok": False, "error": msg, "need_login": True}), 401
        flash(msg, "error")
        return redirect(url_for("novel.detail", novels_id=novels_id))

    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            if not _has_table(cur, "comments"):
                msg = "ไม่พบตาราง comments ในฐานข้อมูล"
                if is_ajax:
                    return jsonify({"ok": False, "error": msg}), 500
                flash(msg, "error")
                return redirect(url_for("novel.detail", novels_id=novels_id))

            cur.execute(
                """
                SELECT cm_id, users_id, novels_id
                FROM comments
                WHERE cm_id = %s
                LIMIT 1
                """,
                (cm_id,),
            )
            row = cur.fetchone()
            if not row or int(row["novels_id"]) != int(novels_id):
                msg = "ไม่พบความคิดเห็นที่ต้องการลบ"
                if is_ajax:
                    return jsonify({"ok": False, "error": msg}), 404
                flash(msg, "error")
                return redirect(url_for("novel.detail", novels_id=novels_id))

            comment_owner_id = int(row["users_id"])
            is_owner = _is_novel_owner(cur, users_id, novels_id)

            if (users_id != comment_owner_id) and (not is_owner):
                msg = "คุณไม่มีสิทธิ์ลความคิดเห็นนี้"
                if is_ajax:
                    return jsonify({"ok": False, "error": msg}), 403
                flash(msg, "error")
                return redirect(url_for("novel.detail", novels_id=novels_id))

            cur.execute(
                "DELETE FROM comments WHERE cm_id = %s",
                (cm_id,),
            )

            if _has_table(cur, "comment_summaries"):
                cur.execute(
                    """
                    INSERT INTO comment_summaries (novels_id, summary_text, last_cm_id, dirty)
                    VALUES (%s, NULL, NULL, 1)
                    ON DUPLICATE KEY UPDATE dirty = 1
                    """,
                    (novels_id,),
                )

            conn.commit()
            if not is_ajax:
                flash("ลบความคิดเห็นเรียบร้อยแล้ว", "success")

    except Exception as e:
        print(f"[novel.delete_comment] error: {e}")
        msg = "เกิดข้อผิดพลาดขณะลบความคิดเห็น"
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 500
        flash(msg, "error")
        return redirect(url_for("novel.detail", novels_id=novels_id))

    if is_ajax:
        return jsonify({"ok": True, "cm_id": cm_id})

    return redirect(url_for("novel.detail", novels_id=novels_id))


