from __future__ import annotations

from flask import (
    Blueprint, render_template, url_for,
    g, request, session, redirect, flash
)
from MySQLdb.cursors import DictCursor
from db import get_db_connection

bookshelf_bp = Blueprint("bookshelf", __name__, template_folder="templates")


def _has_table(cur, name: str) -> bool:
    """ใช้ได้ทั้ง TABLE/VIEW (DESCRIBE ทำงานกับ VIEW ได้)"""
    try:
        cur.execute(f"DESCRIBE `{name}`")
        cur.fetchall()
        return True
    except Exception:
        return False


def _get_current_user_id():
    """ดึง users_id จาก session / g.user รองรับหลาย key"""
    for key in ("users_id", "user_id", "uid"):
        val = session.get(key)
        if val not in (None, ""):
            try:
                return int(val)
            except Exception:
                return None

    user = getattr(g, "user", None)
    if isinstance(user, dict):
        for key in ("users_id", "user_id", "id"):
            val = user.get(key)
            if val not in (None, ""):
                try:
                    return int(val)
                except Exception:
                    return None

    return None


def _cover_url(cover: str | None) -> str:
    """รองรับ cover เป็น: URL / path / static-relative / แค่ชื่อไฟล์"""
    if not cover:
        return url_for("static", filename="cover/placeholder.jpg")

    c = str(cover).strip()
    if c.startswith(("http://", "https://")):
        return c
    if c.startswith("/"):
        return c
    if c.startswith("static/"):
        return "/" + c
    if c.startswith("cover/"):
        return url_for("static", filename=c)

    # default: เก็บเป็นชื่อไฟล์ใน static/cover/
    return url_for("static", filename=f"cover/{c}")


@bookshelf_bp.route("/bookshelf")
def bookshelf_index():
    user_id = _get_current_user_id()
    if not user_id:
        flash("กรุณาเข้าสู่ระบบก่อนเข้าชั้นหนังสือ", "error")
        return redirect(url_for("auth.login", next=request.path))

    # tab: favorite / recent / rated
    tab = (request.args.get("tab", "favorite") or "favorite").lower()
    if tab == "favorites":
        tab = "favorite"

    # status filter: all / continue / done
    status_filter = (request.args.get("status", "all") or "all").lower()

    conn = get_db_connection()
    rows = []

    try:
        with conn.cursor(DictCursor) as cur:
            has_chapter_view = _has_table(cur, "v_novel_chapter_counts")
            has_rating_view = _has_table(cur, "v_novel_rating_stats")

            # ---- SELECT/LEFT JOIN สำหรับ chapter counts ----
            # ใน DB ของคุณ view ใช้ชื่อคอลัมน์ chapter_count
            chapters_sel = (
                "IFNULL(vc.chapter_count, 0) AS total_chapters"
                if has_chapter_view
                else (
                    "(SELECT COUNT(*) "
                    " FROM chapters c2 "
                    " WHERE c2.novels_id = n.novels_id AND c2.status='published'"
                    ") AS total_chapters"
                )
            )
            chapters_join = (
                "LEFT JOIN v_novel_chapter_counts vc ON vc.novels_id = n.novels_id"
                if has_chapter_view
                else ""
            )

            # ---- เลขตอนแรกที่ “published” (กัน draft) ----
            first_chapter_sel = (
                "(SELECT MIN(cmin.chapter_no) "
                " FROM chapters cmin "
                " WHERE cmin.novels_id = n.novels_id AND cmin.status='published'"
                ") AS first_chapter_no"
            )

            # ---- SELECT/LEFT JOIN สำหรับ rating stats ----
            # ใน DB ของคุณ view ใช้ avg_rating + rating_count
            rating_sel = (
                "IFNULL(vr.avg_rating, 0) AS avg_rating, IFNULL(vr.rating_count, 0) AS rating_count"
                if has_rating_view
                else (
                    "("
                    " SELECT IFNULL(AVG(r2.rating), 0) FROM ratings r2 WHERE r2.novels_id = n.novels_id"
                    ") AS avg_rating, ("
                    " SELECT COUNT(*) FROM ratings r3 WHERE r3.novels_id = n.novels_id"
                    ") AS rating_count"
                )
            )
            rating_join = (
                "LEFT JOIN v_novel_rating_stats vr ON vr.novels_id = n.novels_id"
                if has_rating_view
                else ""
            )

            # ---- derived: reading_history ล่าสุดต่อ 1 novels_id (progress ตรง last_read_at) ----
            # เพราะ reading_history ของคุณเป็นหลายแถวต่อเรื่อง (users_id, novels_id, chapters_id)
            last_rh_derived = """
                SELECT rh1.novels_id, rh1.progress, rh1.last_read_at
                FROM reading_history rh1
                WHERE rh1.users_id = %s
                  AND rh1.rh_id = (
                      SELECT rh2.rh_id
                      FROM reading_history rh2
                      WHERE rh2.users_id = rh1.users_id
                        AND rh2.novels_id = rh1.novels_id
                      ORDER BY rh2.last_read_at DESC, rh2.rh_id DESC
                      LIMIT 1
                  )
            """

            # =========== เลือก SQL ตาม tab ===========
            if tab == "recent":
                # เรื่องที่อ่านล่าสุด (ตาม reading_history ล่าสุดต่อเรื่อง)
                sql = f"""
                SELECT
                    rh.novels_id,
                    n.title,
                    n.cover,
                    n.status AS novel_status,
                    u.username AS author_name,
                    {chapters_sel},
                    {first_chapter_sel},
                    {rating_sel},
                    rh.progress,
                    rh.last_read_at
                FROM ({last_rh_derived}) AS rh
                JOIN novels n ON n.novels_id = rh.novels_id
                LEFT JOIN users u ON u.users_id = n.users_id
                {chapters_join}
                {rating_join}
                ORDER BY rh.last_read_at DESC, n.title;
                """
                cur.execute(sql, (user_id,))

            elif tab == "rated":
                # เรื่องที่ user เคยให้คะแนน (เอา updated_at ล่าสุดต่อเรื่อง)
                sql = f"""
                SELECT
                    rr.novels_id,
                    n.title,
                    n.cover,
                    n.status AS novel_status,
                    u.username AS author_name,
                    {chapters_sel},
                    {first_chapter_sel},
                    {rating_sel},
                    NULL AS progress,
                    rr.last_rated_at AS last_read_at
                FROM (
                    SELECT novels_id, MAX(updated_at) AS last_rated_at
                    FROM ratings
                    WHERE users_id = %s
                    GROUP BY novels_id
                ) rr
                JOIN novels n ON n.novels_id = rr.novels_id
                LEFT JOIN users u ON u.users_id = n.users_id
                {chapters_join}
                {rating_join}
                ORDER BY rr.last_rated_at DESC, n.title;
                """
                cur.execute(sql, (user_id,))

            else:
                # favorite: เรื่องที่อยู่ใน bookshelf ของ user นี้
                sql = f"""
                SELECT
                    b.novels_id,
                    n.title,
                    n.cover,
                    n.status AS novel_status,
                    u.username AS author_name,
                    {chapters_sel},
                    {first_chapter_sel},
                    {rating_sel},
                    rh.progress,
                    rh.last_read_at,
                    b.created_at
                FROM bookshelf b
                JOIN novels n ON n.novels_id = b.novels_id
                LEFT JOIN users u ON u.users_id = n.users_id
                {chapters_join}
                {rating_join}
                LEFT JOIN ({last_rh_derived}) AS rh
                       ON rh.novels_id = b.novels_id
                WHERE b.users_id = %s
                ORDER BY b.created_at DESC, n.title;
                """
                cur.execute(sql, (user_id, user_id))

            rows = cur.fetchall()

    finally:
        conn.close()

    # =========== แปลงผลลัพธ์ให้พร้อมใช้ใน template ===========
    items = []
    for row in (rows or []):
        total_chapters = int(row.get("total_chapters") or 0)
        avg_rating = float(row.get("avg_rating") or 0.0)
        rating_count = int(row.get("rating_count") or 0)
        progress = int(row.get("progress") or 0)
        novel_status = row.get("novel_status")  # 'แบบร่าง' | 'เผยแพร่' | 'จบแล้ว'
        first_chapter_no = row.get("first_chapter_no")
        first_chapter_no = int(first_chapter_no) if first_chapter_no not in (None, "") else None

        # สถานะการอ่าน (ใช้กรอง dropdown)
        if progress >= 100 or novel_status == "จบแล้ว":
            read_status = "done"
        elif 0 < progress < 100:
            read_status = "continue"
        else:
            read_status = "new"

        has_published_chapter = first_chapter_no is not None

        item = {
            "novels_id": row["novels_id"],
            "title": row.get("title") or "",
            "cover": _cover_url(row.get("cover")),
            "author_name": row.get("author_name") or "-",
            "total_chapters": total_chapters,
            "avg_rating": avg_rating,
            "rating_count": rating_count,
            "progress": progress,
            "read_status": read_status,
            "last_read_at": row.get("last_read_at"),
            "has_published_chapter": has_published_chapter,
            # ถ้าไม่มีตอน published ให้ส่ง None (ให้ template ปิดปุ่มอ่านต่อได้)
            "read_url": (
                url_for("reading.read_chapter", novels_id=row["novels_id"], chapter_no=first_chapter_no)
                if has_published_chapter
                else None
            ),
        }
        items.append(item)

    # filter ตาม dropdown (status)
    if status_filter == "done":
        items = [i for i in items if i["read_status"] == "done"]
    elif status_filter == "continue":
        items = [i for i in items if i["read_status"] == "continue"]
    # all = ไม่กรอง

    return render_template(
        "bookshelf.html",
        items=items,
        active_tab=tab,
        status_filter=status_filter,
    )
