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


def _author_sql_parts(cur):
    """เลือกวิธีดึงชื่อผู้เขียนจากตาราง novels / users"""
    try:
        cur.execute("DESCRIBE novels")
        cols = {r["Field"] for r in cur.fetchall()}
    except Exception:
        cols = set()

    if "users_id" in cols:
        return ("u.username AS author_name", "LEFT JOIN users u ON u.users_id = n.users_id")
    if "author_id" in cols:
        return ("u.username AS author_name", "LEFT JOIN users u ON u.users_id = n.author_id")
    if "created_by" in cols:
        return ("u.username AS author_name", "LEFT JOIN users u ON u.users_id = n.created_by")
    return ("'ผู้เขียนไม่ระบุ' AS author_name", "")


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
    uid = session.get("users_id")
    if not uid and getattr(g, "user", None):
        try:
            uid = g.user["users_id"]
        except Exception:
            uid = None
    return uid


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
        "into 3-5 concise bullet-style lines in Thai."
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
    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:

            # ---------- POST: รับฟอร์มความคิดเห็น ----------
            if request.method == "POST":
                content = (request.form.get("content") or "").strip()

                # ตัดให้ไม่เกิน 500 ตัวอักษรอีกชั้น (กันคนยิงตรง ๆ)
                if len(content) > 500:
                    content = content[:500]

                if not content:
                    flash("กรุณาพิมพ์ความคิดเห็นก่อนส่ง", "error")
                    return redirect(url_for("novel.detail", novels_id=novels_id))

                users_id = _current_user_id()
                if not users_id:
                    flash("กรุณาเข้าสู่ระบบก่อนแสดงความคิดเห็น", "error")
                    return redirect(url_for("novel.detail", novels_id=novels_id))

                if not _has_table(cur, "comments"):
                    flash("ไม่พบตาราง comments ในฐานข้อมูล", "error")
                    return redirect(url_for("novel.detail", novels_id=novels_id))

                cur.execute(
                    """
                    INSERT INTO comments (users_id, novels_id, content)
                    VALUES (%s, %s, %s)
                    """,
                    (users_id, novels_id, content),
                )
                conn.commit()
                flash("ส่งความคิดเห็นเรียบร้อยแล้ว", "success")

                # PRG pattern: กลับไปโหลดหน้าเดิมแบบ GET
                return redirect(url_for("novel.detail", novels_id=novels_id))

            # ---------- GET: โหลดข้อมูลนิยาย + ตอน + ความคิดเห็น ----------

            # ----- อ่านค่า sort จาก query string (asc=เก่า→ใหม่, desc=ใหม่→เก่า) -----
            sort = request.args.get("sort", "asc")
            if sort not in ("asc", "desc"):
                sort = "asc"
            order_dir = "ASC" if sort == "asc" else "DESC"

            # --- novel ---
            sel_author, join_author = _author_sql_parts(cur)
            cur.execute(
                f"""
                SELECT n.novels_id, n.title, n.description, n.status, n.cover,
                       n.cate_id, c.name AS category_name, {sel_author}
                FROM novels n
                LEFT JOIN categories c ON c.cate_id = n.cate_id
                {join_author}
                WHERE n.novels_id = %s
                """,
                (novels_id,),
            )
            novel = cur.fetchone()
            if not novel:
                abort(404, description="ไม่พบนิยายที่ระบุ")

            novel["status"] = _normalize_status(novel.get("status"))
            novel["cover_url"] = _process_cover_url(novel.get("cover"))

            # --- ratings (ถ้ามีตาราง ratings) ---
            novel["avg_rating"] = 0.0
            novel["rating_count"] = 0
            novel["user_rating"] = 0

            if _has_table(cur, "ratings"):
                # ค่าเฉลี่ย + จำนวนคนโหวต
                cur.execute(
                    """
                    SELECT AVG(rating) AS avg_rating,
                           COUNT(*) AS rating_count
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

                # คะแนนที่ user คนนี้เคยให้ (ถ้ามี)
                uid = _current_user_id()
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
                        novel["user_rating"] = int(r["rating"]) if r and r.get("rating") is not None else 0
                    except (TypeError, ValueError):
                        novel["user_rating"] = 0

            # --- favorites ---
            novel["total_favorites"] = 0
            if _has_table(cur, "favorites"):
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

            # --- readers ---
            novel["total_readers"] = 0
            if _has_table(cur, "reading_history") and _has_column(cur, "reading_history", "users_id"):
                cur.execute(
                    "SELECT COUNT(DISTINCT users_id) AS c FROM reading_history WHERE novels_id = %s",
                    (novels_id,),
                )
                novel["total_readers"] = int((cur.fetchone() or {}).get("c") or 0)
            elif _has_table(cur, "novel_reads"):
                if _has_column(cur, "novel_reads", "users_id"):
                    cur.execute(
                        "SELECT COUNT(DISTINCT users_id) AS c "
                        "FROM novel_reads WHERE novels_id = %s",
                        (novels_id,),
                    )
                else:
                    cur.execute(
                        "SELECT COUNT(*) AS c FROM novel_reads WHERE novels_id = %s",
                        (novels_id,),
                    )
                novel["total_readers"] = int((cur.fetchone() or {}).get("c") or 0)

            # --- chapters ---
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

            # like_count: ถ้ามีคอลัมน์ใน chapters ก็ใช้เลย / ถ้าไม่มีก็ไปนับจาก chapter_likes
            like_sel = "0 AS like_count"
            like_join = ""
            group_by = ""

            if _has_column(cur, "chapters", "like_count"):
                like_sel = "c.like_count"
            elif _has_table(cur, "chapter_likes"):
                # มีตาราง chapter_likes → นับจำนวน like ต่อ chapter
                like_sel = "COUNT(cl.like_id) AS like_count"
                like_join = "LEFT JOIN chapter_likes cl ON cl.chapters_id = c.chapters_id"
                group_by = "GROUP BY c.chapters_id"

            cur.execute(
                f"""
                SELECT c.{chap_pk} AS chapters_id,
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

            # เตรียมชุดข้อมูลตอนที่ user คนนี้เคยกดหัวใจ
            uid = _current_user_id()
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

            # ใส่ฟิลด์ is_liked + แปลง like_count เป็น int
            for ch in chapters:
                ch["like_count"] = int(ch.get("like_count") or 0)
                ch["is_liked"] = (ch.get("chapters_id") in liked_set)

            # --- comments + user info ---
            comments = []
            if _has_table(cur, "comments"):
                sel_username, sel_avatar, join_users = _user_profile_parts(cur)
                cur.execute(
                    f"""
                    SELECT
                        c.cm_id,
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
                for cm in comments:
                    cm["avatar_url"] = _process_avatar_url(cm.get("profile_image"))

        return render_template(
            "novelcover.html",
            novel=novel,
            chapters=chapters,
            comments=comments,
        )
    except Exception as e:
        print(f"[novel.detail] error: {e}")
        abort(500)

    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:

            # ---------- POST: รับฟอร์มความคิดเห็น ----------
            if request.method == "POST":
                content = (request.form.get("content") or "").strip()

                # ตัดให้ไม่เกิน 500 ตัวอักษรอีกชั้น (กันคนยิงตรง ๆ)
                if len(content) > 500:
                    content = content[:500]

                if not content:
                    flash("กรุณาพิมพ์ความคิดเห็นก่อนส่ง", "error")
                    return redirect(url_for("novel.detail", novels_id=novels_id))

                users_id = _current_user_id()
                if not users_id:
                    flash("กรุณาเข้าสู่ระบบก่อนแสดงความคิดเห็น", "error")
                    return redirect(url_for("novel.detail", novels_id=novels_id))

                if not _has_table(cur, "comments"):
                    flash("ไม่พบตาราง comments ในฐานข้อมูล", "error")
                    return redirect(url_for("novel.detail", novels_id=novels_id))

                # บันทึกคอมเมนต์ใหม่
                cur.execute(
                    """
                    INSERT INTO comments (users_id, novels_id, content)
                    VALUES (%s, %s, %s)
                    """,
                    (users_id, novels_id, content),
                )

                # ทำเครื่องหมายว่า summary ของนิยายเรื่องนี้ต้องสรุปใหม่ (dirty = 1)
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
                flash("ส่งความคิดเห็นเรียบร้อยแล้ว", "success")

                # PRG pattern: กลับไปโหลดหน้าเดิมแบบ GET
                return redirect(url_for("novel.detail", novels_id=novels_id))

            # ---------- GET: โหลดข้อมูลนิยาย + ตอน + ความคิดเห็น ----------

            # ----- อ่านค่า sort จาก query string (asc=เก่า→ใหม่, desc=ใหม่→เก่า) -----
            sort = request.args.get("sort", "asc")
            if sort not in ("asc", "desc"):
                sort = "asc"
            order_dir = "ASC" if sort == "asc" else "DESC"

            # --- novel ---
            sel_author, join_author = _author_sql_parts(cur)
            cur.execute(
                f"""
                SELECT n.novels_id, n.title, n.description, n.status, n.cover,
                       n.cate_id, c.name AS category_name, {sel_author}
                FROM novels n
                LEFT JOIN categories c ON c.cate_id = n.cate_id
                {join_author}
                WHERE n.novels_id = %s
                """,
                (novels_id,),
            )
            novel = cur.fetchone()
            if not novel:
                abort(404, description="ไม่พบนิยายที่ระบุ")

            novel["status"] = _normalize_status(novel.get("status"))
            novel["cover"] = _process_cover_url(novel.get("cover"))

            # --- rating (เฉลี่ย + จำนวนโหวต) ---
            novel["avg_rating"] = 0.0
            novel["rating_count"] = 0
            novel["user_rating"] = 0
            has_ratings = _has_table(cur, "ratings")

            if has_ratings:
                # ค่าเฉลี่ย + จำนวนโหวตทั้งหมด
                cur.execute(
                    """
                    SELECT COALESCE(AVG(rating),0) AS avg_rating,
                           COUNT(*) AS rating_count
                    FROM ratings
                    WHERE novels_id = %s
                    """,
                    (novels_id,),
                )
                row = cur.fetchone() or {}
                novel["avg_rating"] = float(row.get("avg_rating") or 0)
                novel["rating_count"] = int(row.get("rating_count") or 0)

                # คะแนนที่ user คนนี้เคยให้ (ถ้ามี)
                uid = _current_user_id()
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
                        novel["user_rating"] = int(r["rating"]) if r and r.get("rating") is not None else 0
                    except (TypeError, ValueError):
                        novel["user_rating"] = 0

            # --- favorites ---
            novel["total_favorites"] = 0
            if _has_table(cur, "favorites"):
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

            # --- readers ---
            novel["total_readers"] = 0
            if _has_table(cur, "reading_history") and _has_column(cur, "reading_history", "users_id"):
                cur.execute(
                    "SELECT COUNT(DISTINCT users_id) AS c FROM reading_history WHERE novels_id = %s",
                    (novels_id,),
                )
                novel["total_readers"] = int((cur.fetchone() or {}).get("c") or 0)
            elif _has_table(cur, "novel_reads"):
                if _has_column(cur, "novel_reads", "users_id"):
                    cur.execute(
                        "SELECT COUNT(DISTINCT users_id) AS c "
                        "FROM novel_reads WHERE novels_id = %s",
                        (novels_id,),
                    )
                else:
                    cur.execute(
                        "SELECT COUNT(*) AS c FROM novel_reads WHERE novels_id = %s",
                        (novels_id,),
                    )
                novel["total_readers"] = int((cur.fetchone() or {}).get("c") or 0)

            # --- chapters ---
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

            # like_count: ถ้ามีคอลัมน์ใน chapters ก็ใช้เลย / ถ้าไม่มีก็ไปนับจาก chapter_likes
            like_sel = "0 AS like_count"
            like_join = ""
            group_by = ""

            if _has_column(cur, "chapters", "like_count"):
                # ใช้คอลัมน์ใน chapters โดยตรง (มี trigger อัปเดตให้แล้ว)
                like_sel = "COALESCE(c.like_count, 0) AS like_count"
            elif _has_table(cur, "chapter_likes"):
                # fallback: join ไปตาราง chapter_likes แล้ว COUNT(cl.chapters_id)
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
                SELECT c.{chap_pk} AS chapters_id,
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

            # เตรียมชุดข้อมูลตอนที่ user คนนี้เคยกดหัวใจ
            uid = _current_user_id()
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

            # ใส่ฟิลด์ is_liked + แปลง like_count เป็น int
            for ch in chapters:
                ch["like_count"] = int(ch.get("like_count") or 0)
                ch["is_liked"] = (ch.get("chapters_id") in liked_set)

            # --- comments + user info ---
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
                for cm in comments:
                    cm["avatar_url"] = _process_avatar_url(cm.get("profile_image"))

        return render_template(
            "novelcover.html",
            novel=novel,
            chapters=chapters,
            comments=comments,
        )
    except Exception as e:
        print(f"[novel.detail] error: {e}")
        abort(500)


# ---------- route: สรุปความคิดเห็นด้วย AI (API JSON) ----------

# ---------- route: สรุปความคิดเห็นด้วย AI (API JSON) ----------

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
    rating_raw = (request.form.get("rating") or "").strip()
    try:
        rating = int(rating_raw)
    except (TypeError, ValueError):
        rating = 0

    # รับเฉพาะ 1–5 ดาว
    if rating < 1 or rating > 5:
        flash("คะแนนต้องอยู่ระหว่าง 1–5 ดาว", "error")
        return redirect(url_for("novel.detail", novels_id=novels_id))

    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            if not _has_table(cur, "ratings"):
                flash("ยังไม่พบตาราง ratings ในฐานข้อมูล", "error")
                return redirect(url_for("novel.detail", novels_id=novels_id))

            users_id = _current_user_id()
            if not users_id:
                flash("กรุณาเข้าสู่ระบบก่อนให้คะแนน", "error")
                return redirect(url_for("novel.detail", novels_id=novels_id))

            # ถ้ามีอยู่แล้ว -> update, ถ้ายังไม่มี -> insert
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

            conn.commit()
            flash("บันทึกคะแนนเรียบร้อยแล้ว", "success")

    except Exception as e:
        print(f"[novel.rate] error: {e}")
        flash("เกิดข้อผิดพลาดขณะบันทึกคะแนน", "error")

    return redirect(url_for("novel.detail", novels_id=novels_id))


@novel_bp.route("/novel/<int:novels_id>/chapter/<int:chapters_id>/like", methods=["POST"])
def toggle_chapter_like(novels_id: int, chapters_id: int):
    """กด/ยกเลิกหัวใจให้ตอน (toggle) แล้ว redirect กลับหน้า novel cover"""
    sort = request.form.get("next_sort") or "asc"

    users_id = _current_user_id()
    if not users_id:
        flash("กรุณาเข้าสู่ระบบก่อนกดหัวใจ", "error")
        return redirect(url_for("novel.detail", novels_id=novels_id, sort=sort))

    try:
        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            # ต้องมีตาราง chapter_likes ก่อน
            if not _has_table(cur, "chapter_likes"):
                flash("ยังไม่พบตาราง chapter_likes ในฐานข้อมูล", "error")
                return redirect(url_for("novel.detail", novels_id=novels_id, sort=sort))

            # ตรวจสอบว่าตอนนี้อยู่ในนิยายที่ระบุจริง ๆ
            cur.execute(
                "SELECT novels_id FROM chapters WHERE chapters_id = %s LIMIT 1",
                (chapters_id,),
            )
            row = cur.fetchone()
            if not row or int(row["novels_id"]) != novels_id:
                flash("ไม่พบตอนที่ต้องการ", "error")
                return redirect(url_for("novel.detail", novels_id=novels_id, sort=sort))

            # เช็คว่าผู้ใช้นี้เคยกดหัวใจตอนนี้หรือยัง
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
                # ถ้าเคยกดแล้ว -> ยกเลิกหัวใจ
                cur.execute(
                    """
                    DELETE FROM chapter_likes
                    WHERE chapters_id = %s AND users_id = %s
                    """,
                    (chapters_id, users_id),
                )
                flash("ยกเลิกหัวใจตอนนี้แล้ว", "info")
            else:
                # ยังไม่เคยกด -> กดหัวใจ
                cur.execute(
                    """
                    INSERT INTO chapter_likes (chapters_id, users_id)
                    VALUES (%s, %s)
                    """,
                    (chapters_id, users_id),
                )
                flash("ขอบคุณที่กดหัวใจให้ตอนนี้", "success")

            conn.commit()

    except Exception as e:
        print(f"[novel.toggle_chapter_like] error: {e}")
        flash("เกิดข้อผิดพลาดขณะบันทึกหัวใจ", "error")

    return redirect(url_for("novel.detail", novels_id=novels_id, sort=sort))
