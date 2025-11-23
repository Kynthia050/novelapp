# novelcover.py
from flask import Blueprint, render_template, abort, url_for, request, redirect, session, flash, g, jsonify
from MySQLdb.cursors import DictCursor
from db import get_db_connection
import os

novel_bp = Blueprint("novel", __name__, template_folder="./templates")

# ---------- helpers พวกเช็คตาราง / สร้าง URL ----------

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

    จาก users.sql โครงสร้างคือ:
      users_id, username, pfpic, ...
    และไฟล์อยู่ใต้ static/profile/.
    """
    if not _has_table(cur, "users"):
        return ("NULL AS username", "NULL AS profile_image", "")

    sel_username = "u.username AS username"
    sel_avatar   = "u.pfpic AS profile_image"
    join_clause  = "LEFT JOIN users u ON u.users_id = c.users_id"
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


def _build_summary_prompt(novel_title: str, base_summary: str | None, comments: list[dict]) -> str:
    """สร้างข้อความ prompt สำหรับให้ AI สรุปความคิดเห็นของนิยายเรื่องหนึ่ง"""
    title_part = f"นิยายเรื่อง: {novel_title}\n" if novel_title else ""
    bullet_lines: list[str] = []
    for c in comments:
        text = str(c.get("content") or "").strip()
        if not text:
            continue
        # ตัดความยาวคอมเมนต์แต่ละอันให้ไม่ยาวเกินไป (กัน token บวม)
        if len(text) > 300:
            text = text[:300] + "..."
        bullet_lines.append(f"- {text}")
    comments_block = "\n".join(bullet_lines) if bullet_lines else "- (ไม่มีคอมเมนต์ใหม่)"

    system_intro = (
        "คุณคือผู้ช่วยสรุปความคิดเห็นผู้อ่านนิยายออนไลน์ "
        "ช่วยสรุปเป็นภาษาไทยแบบกระชับ แบ่งเป็นหัวข้อย่อย 3–5 ข้อ "
        "เน้นโทนความรู้สึกของผู้อ่าน (ชอบอะไร ไม่ชอบอะไร) และข้อเสนอแนะสำคัญ"
    )

    if base_summary:
        return (
            system_intro + "\n\n"
            + title_part +
            "สรุปเดิมที่เคยมี:\n"
            f"{base_summary}\n\n"
            "คอมเมนต์ใหม่ที่เพิ่งถูกเพิ่มเข้ามา:\n"
            f"{comments_block}\n\n"
            "กรุณาสร้างสรุปฉบับอัปเดตที่รวมทั้งสรุปเดิมและคอมเมนต์ใหม่ "
            "ตอบกลับเฉพาะข้อความสรุปเท่านั้น"
        )
    else:
        return (
            system_intro + "\n\n"
            + title_part +
            "คอมเมนต์ทั้งหมดของผู้อ่าน:\n"
            f"{comments_block}\n\n"
            "กรุณาสร้างสรุปความคิดเห็นจากคอมเมนต์เหล่านี้ "
            "ตอบกลับเฉพาะข้อความสรุปเท่านั้น"
        )


def generate_comment_summary(base_summary, comments, novel_title: str = "") -> str:
    """
    ฟังก์ชันห่อสำหรับเรียก AI สรุปความคิดเห็น

    *** ตรงนี้ให้คุณไปเชื่อมต่อ API จริงเอง ***
    ตัวอย่างเช่น ต่อกับ OpenAI API, Azure OpenAI, หรือโมเดลภายในองค์กร/มหาวิทยาลัย ฯลฯ

    ตอนนี้จะเป็น implementation แบบง่าย ๆ (ยังไม่เรียก AI จริง)
    คือเอาคอมเมนต์ใหม่มาตัดให้สั้นแล้วต่อกับสรุปเดิม
    เพื่อให้ flow ฝั่ง backend + DB ทำงานได้ก่อน
    """
    # ถ้าไม่มีคอมเมนต์ใหม่เลย แต่มีสรุปเดิมอยู่แล้ว → ส่งสรุปเดิมกลับไป
    if not comments and base_summary:
        return base_summary

    # รวมข้อความคอมเมนต์ใหม่เป็น list
    new_texts: list[str] = []
    for c in comments:
        t = str(c.get("content") or "").strip()
        if not t:
            continue
        if len(t) > 200:
            t = t[:200] + "..."
        new_texts.append(t)

    if not new_texts and base_summary:
        return base_summary

    # 🧠 จุดที่ควรไปเรียก AI จริง:
    # prompt = _build_summary_prompt(novel_title, base_summary, comments)
    # result = call_your_ai_model(prompt)
    # return result

    # ชั่วคราว: สร้าง "สรุปตัวอย่าง" จากคอมเมนต์ใหม่ เพื่อให้ระบบใช้ทดสอบได้
    if base_summary:
        return base_summary + "\n\nความคิดเห็นใหม่ (ตัวอย่าง ยังไม่สรุปด้วย AI จริง):\n- " + "\n- ".join(new_texts)
    else:
        return "สรุปความคิดเห็น (ตัวอย่าง ยังไม่สรุปด้วย AI จริง):\n- " + "\n- ".join(new_texts)


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

            from_chapters_like_col = False
            if _has_column(cur, "chapters", "like_count"):
                # ใช้คอลัมน์ใน chapters โดยตรง (มี trigger อัปเดตให้แล้ว)
                like_sel = "COALESCE(c.like_count, 0) AS like_count"
                from_chapters_like_col = True
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
                  AND c.status = 'published'   -- ❗ กรองเฉพาะตอนเผยแพร่
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

@novel_bp.route("/novel/<int:novels_id>/comment-summary", methods=["POST"])
def comment_summary(novels_id: int):
    """คืนสรุปความคิดเห็นของนิยายเรื่องหนึ่งในรูปแบบ JSON

    - ถ้ามีสรุปเก่าและ dirty = 0 → ส่งสรุปเก่าจาก DB เลย (from_cache = True)
    - ถ้ายังไม่เคยสรุป หรือ dirty = 1 → ดึงคอมเมนต์ใหม่แล้วสร้างสรุปใหม่
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

            # อ่านข้อมูลนิยาย (เอา title ไว้ช่วยสร้าง prompt ถ้าจะใช้ AI จริง)
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

            # อ่านสรุปเดิม (ถ้ามี)
            summary_row = None
            if _has_table(cur, "comment_summaries"):
                cur.execute(
                    """SELECT summary_text, last_cm_id, dirty
                        FROM comment_summaries
                        WHERE novels_id = %s
                        LIMIT 1""",
                    (novels_id,),
                )
                summary_row = cur.fetchone()

            base_summary = None
            last_cm_id = 0
            dirty = 1  # ถ้าไม่มี row เลย ให้ถือว่าต้องสรุปใหม่

            if summary_row:
                base_summary = summary_row.get("summary_text")
                try:
                    last_cm_id = int(summary_row.get("last_cm_id") or 0)
                except (TypeError, ValueError):
                    last_cm_id = 0
                try:
                    dirty = int(summary_row.get("dirty") or 1)
                except (TypeError, ValueError):
                    dirty = 1

            # ถ้ามีสรุปเดิม + dirty = 0 → ใช้สรุปจาก cache ได้เลย
            if base_summary and dirty == 0:
                return jsonify({
                    "ok": True,
                    "summary": base_summary,
                    "from_cache": True,
                })

            # ต้องสรุปใหม่ (ครั้งแรก หรือมีคอมเมนต์เปลี่ยน)
            # ถ้ามี base_summary แปลว่าเคยสรุปแล้ว → ดึงเฉพาะคอมเมนต์ที่ cm_id > last_cm_id
            if base_summary and last_cm_id > 0:
                cur.execute(
                    """SELECT cm_id, content
                        FROM comments
                        WHERE novels_id = %s
                          AND cm_id > %s
                        ORDER BY cm_id ASC""",
                    (novels_id, last_cm_id),
                )
            else:
                # ยังไม่เคยสรุป → ดึงคอมเมนต์ทั้งหมดของนิยายเรื่องนี้
                cur.execute(
                    """SELECT cm_id, content
                        FROM comments
                        WHERE novels_id = %s
                        ORDER BY cm_id ASC""",
                    (novels_id,),
                )
            new_comments = cur.fetchall()

            # ถ้าไม่มีคอมเมนต์ใหม่เลย แต่มี base_summary อยู่แล้ว
            # ให้ mark dirty=0 แล้วส่งสรุปเดิมกลับ
            if not new_comments and base_summary:
                if _has_table(cur, "comment_summaries"):
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

            # เรียกฟังก์ชัน generate_comment_summary (ปัจจุบันเป็น stub ยังไม่เรียก AI จริง)
            new_summary = generate_comment_summary(base_summary, new_comments, novel_title=novel_title)

            if not new_summary:
                return jsonify({
                    "ok": False,
                    "error": "ไม่สามารถสร้างสรุปความคิดเห็นได้"
                }), 500

            # หา cm_id สูงสุดของคอมเมนต์ที่นำไปสรุป
            max_cm_id = last_cm_id
            for c in new_comments:
                try:
                    cm_id_val = int(c.get("cm_id") or 0)
                except (TypeError, ValueError):
                    cm_id_val = 0
                if cm_id_val > max_cm_id:
                    max_cm_id = cm_id_val

            # บันทึก / อัปเดตตาราง comment_summaries
            if _has_table(cur, "comment_summaries"):
                cur.execute(
                    """INSERT INTO comment_summaries (novels_id, summary_text, last_cm_id, dirty)
                        VALUES (%s, %s, %s, 0)
                        ON DUPLICATE KEY UPDATE
                          summary_text = VALUES(summary_text),
                          last_cm_id   = VALUES(last_cm_id),
                          dirty        = 0""",
                    (novels_id, new_summary, max_cm_id),
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
