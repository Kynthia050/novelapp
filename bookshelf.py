from __future__ import annotations

from flask import (
    Blueprint, render_template, url_for,
    g, request, session, redirect, flash
)
from MySQLdb.cursors import DictCursor
from db import get_db_connection, active_user_where

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

    # status filter: all / finished / ongoing
    status_filter = (request.args.get("status", "all") or "all").lower()

    order_raw = (request.args.get("order") or "").lower()
    order_from_query = bool(order_raw)
    order = order_raw or ("desc" if tab == "recent" else "asc")
    if order not in ("asc", "desc"):
        order = "desc" if tab == "recent" else "asc"

    conn = get_db_connection()
    rows = []

    # ✅ สถานะ “เผยแพร่” ของ chapters (รองรับทั้งอังกฤษ/ไทย เผื่อ DB เก็บต่างกัน)
    PUBLISHED_STATUSES = ("published", "เผยแพร่", "เผยแพร่แล้ว")

    try:
        with conn.cursor(DictCursor) as cur:
            has_rating_view = _has_table(cur, "v_novel_rating_stats")
            active_where, active_params = active_user_where(cur, "u")

            # ---- จำนวนตอน: นับเฉพาะตอนเผยแพร่เท่านั้น (ตาม requirement) ----
            # ส่งทั้ง published_chapters และ total_chapters (ให้เท่ากัน เพื่อไม่พังกับ template เก่า)
            published_chapters_sel = (
                "(SELECT COUNT(*) "
                " FROM chapters c2 "
                " WHERE c2.novels_id = n.novels_id "
                f"   AND c2.status IN {PUBLISHED_STATUSES}"
                ") AS published_chapters"
            )
            total_chapters_sel = "published_chapters AS total_chapters"

            # ---- เลขตอนแรกที่ “published” (กัน draft) ----
            first_chapter_sel = (
                "(SELECT MIN(cmin.chapter_no) "
                " FROM chapters cmin "
                " WHERE cmin.novels_id = n.novels_id "
                f"   AND cmin.status IN {PUBLISHED_STATUSES}"
                ") AS first_chapter_no"
            )

            # ---- SELECT/LEFT JOIN สำหรับ rating stats ----
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

            category_sel = "c.name AS category_name"
            category_join = "LEFT JOIN categories c ON c.cate_id = n.cate_id"

            # ---- derived: reading_history ล่าสุดต่อ 1 novels_id (progress ตรง last_read_at) ----
            # ✅ เพิ่ม chapters_id เพื่อทำ “อ่านต่อ” ไปตอนล่าสุดที่อ่าน
            last_rh_derived = """
                SELECT rh1.novels_id, rh1.chapters_id, rh1.progress, rh1.last_read_at
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

            # ✅ join เพื่อเอา chapter_no ของ “ตอนล่าสุดที่อ่าน” (และต้องเป็นตอนเผยแพร่เท่านั้น)
            last_chapter_join = (
                "LEFT JOIN chapters cr ON cr.chapters_id = rh.chapters_id "
                f"AND cr.status IN {PUBLISHED_STATUSES}"
            )
            last_chapter_sel = "cr.chapter_no AS last_chapter_no"

            order_dir = "ASC" if order == "asc" else "DESC"

            # =========== เลือก SQL ตาม tab ===========
            if tab == "recent":
                # เรื่องที่อ่านล่าสุด (ตาม reading_history ล่าสุดต่อเรื่อง)
                sql = f"""
                SELECT
                    rh.novels_id,
                    n.title,
                    n.cover,
                    COALESCE(n.views, 0) AS views,
                    n.status AS novel_status,
                    u.username AS author_name,
                    u.users_id AS author_id,
                    {category_sel},
                    {published_chapters_sel},
                    {first_chapter_sel},
                    {rating_sel},
                    rh.progress,
                    rh.last_read_at,
                    {last_chapter_sel}
                FROM ({last_rh_derived}) AS rh
                JOIN novels n ON n.novels_id = rh.novels_id
                LEFT JOIN users u ON u.users_id = n.users_id
                {category_join}
                {last_chapter_join}
                {rating_join}
                WHERE {active_where}
                ORDER BY rh.last_read_at {order_dir}, n.title;
                """
                cur.execute(sql, (user_id, *active_params))

            elif tab == "rated":
                # เรื่องที่ user เคยให้คะแนน (เอา updated_at ล่าสุดต่อเรื่อง)
                # ✅ rated ไม่มี reading_history => จะอ่านต่อไปตอนเผยแพร่ตอนแรก
                sql = f"""
                SELECT
                    rr.novels_id,
                    n.title,
                    n.cover,
                    COALESCE(n.views, 0) AS views,
                    n.status AS novel_status,
                    u.username AS author_name,
                    u.users_id AS author_id,
                    {category_sel},
                    {published_chapters_sel},
                    {first_chapter_sel},
                    {rating_sel},
                    NULL AS progress,
                    rr.last_rated_at AS last_read_at,
                    NULL AS last_chapter_no
                FROM (
                    SELECT novels_id, MAX(updated_at) AS last_rated_at
                    FROM ratings
                    WHERE users_id = %s
                    GROUP BY novels_id
                ) rr
                JOIN novels n ON n.novels_id = rr.novels_id
                LEFT JOIN users u ON u.users_id = n.users_id
                {category_join}
                {rating_join}
                WHERE {active_where}
                ORDER BY rr.last_rated_at {order_dir}, n.title;
                """
                cur.execute(sql, (user_id, *active_params))

            else:
                # favorite: เรื่องที่อยู่ใน bookshelf ของ user นี้
                sql = f"""
                SELECT
                    b.novels_id,
                    n.title,
                    n.cover,
                    COALESCE(n.views, 0) AS views,
                    n.status AS novel_status,
                    u.username AS author_name,
                    u.users_id AS author_id,
                    {category_sel},
                    {published_chapters_sel},
                    {first_chapter_sel},
                    {rating_sel},
                    rh.progress,
                    rh.last_read_at,
                    {last_chapter_sel},
                    b.created_at
                FROM bookshelf b
                JOIN novels n ON n.novels_id = b.novels_id
                LEFT JOIN users u ON u.users_id = n.users_id
                {category_join}
                {rating_join}
                LEFT JOIN ({last_rh_derived}) AS rh
                       ON rh.novels_id = b.novels_id
                {last_chapter_join}
                WHERE b.users_id = %s AND {active_where}
                ORDER BY b.created_at {order_dir}, n.title;
                """
                cur.execute(sql, (user_id, user_id, *active_params))

            rows = cur.fetchall()

    finally:
        conn.close()

    # =========== แปลงผลลัพธ์ให้พร้อมใช้ใน template ===========
    items = []
    for row in (rows or []):
        # ✅ published_chapters คือ “จำนวนตอนเผยแพร่” เท่านั้น
        published_chapters = int(row.get("published_chapters") or 0)

        # ✅ คง total_chapters ให้เท่ากัน (กัน template เก่าพัง)
        total_chapters = int(row.get("total_chapters") or published_chapters)

        avg_rating = float(row.get("avg_rating") or 0.0)
        rating_count = int(row.get("rating_count") or 0)
        progress = int(row.get("progress") or 0)
        views = int(row.get("views") or 0)

        novel_status = row.get("novel_status")  # 'แบบร่าง' | 'เผยแพร่' | 'จบแล้ว' ฯลฯ

        first_chapter_no = row.get("first_chapter_no")
        first_chapter_no = int(first_chapter_no) if first_chapter_no not in (None, "") else None

        last_chapter_no = row.get("last_chapter_no")
        last_chapter_no = int(last_chapter_no) if last_chapter_no not in (None, "") else None

        # สถานะการอ่าน (ใช้กรอง dropdown legacy)
        if progress >= 100 or str(novel_status).strip() in ("จบแล้ว", "finished", "done", "complete"):
            read_status = "done"
        elif 0 < progress < 100:
            read_status = "continue"
        else:
            read_status = "new"

        # Novel status normalized: finished / ongoing / unknown
        ns = (str(novel_status or "").strip().lower())
        if ns in ("จบแล้ว", "finished", "done", "complete", "completed"):
            novel_status_key = "finished"
        elif ns:
            novel_status_key = "ongoing"
        else:
            novel_status_key = "unknown"

        # ✅ มีตอนเผยแพร่จริงไหม
        has_published_chapter = first_chapter_no is not None and published_chapters > 0

        # ✅ “อ่านต่อ” ไปตอนล่าสุดที่อ่าน (ถ้ามี) ไม่งั้นไปตอนเผยแพร่ตอนแรก
        chapter_no_for_read = last_chapter_no or first_chapter_no

        item = {
            "novels_id": row["novels_id"],
            "title": row.get("title") or "",
            "cover": _cover_url(row.get("cover")),
            "author_name": row.get("author_name") or "-",
            "author_id": row.get("author_id"),
            "category_name": row.get("category_name"),
            "novel_status": novel_status,
            "novel_status_key": novel_status_key,
            "views": views,

            # ส่งทั้ง 2 ฟิลด์ (published_chapters ใช้ใน card ใหม่)
            "published_chapters": published_chapters,
            "total_chapters": total_chapters,

            "avg_rating": avg_rating,
            "rating_count": rating_count,
            "progress": progress,
            "read_status": read_status,
            "last_read_at": row.get("last_read_at"),
            "has_published_chapter": has_published_chapter,

            # ✅ ปลอดภัยกับ template ที่ใส่ href ตรง ๆ (อย่าให้ None)
            "read_url": (
                url_for("reading.read_chapter", novels_id=row["novels_id"], chapter_no=chapter_no_for_read)
                if has_published_chapter and chapter_no_for_read is not None
                else "#"
            ),
            "detail_url": url_for("novel.detail", novels_id=row["novels_id"]),
        }

        # ถ้า detail_url หาชื่อ endpoint ไม่เจอ ให้ fallback = read_url
        if not item["detail_url"]:
            item["detail_url"] = item["read_url"] if item["read_url"] != "#" else url_for("bookshelf.bookshelf_index")

        items.append(item)

    # filter ตาม dropdown (status)
    if status_filter in ("done", "finished"):
        items = [i for i in items if i.get("novel_status_key") == "finished"]
    elif status_filter in ("continue", "ongoing"):
        items = [i for i in items if i.get("novel_status_key") == "ongoing"]
    # all = ไม่กรอง

    total = len(items)

    # pagination (20 per page)
    try:
        page = int(request.args.get("page", 1))
    except Exception:
        page = 1
    if page < 1:
        page = 1
    per_page = 20
    total_pages = (total + per_page - 1) // per_page
    if total_pages < 1:
        total_pages = 1
    if page > total_pages:
        page = total_pages
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated = items[start_idx:end_idx]

    start_item = start_idx + 1 if total > 0 else 0
    end_item = min(end_idx, total)

    return render_template(
        "bookshelf.html",
        items=paginated,
        active_tab=tab,
        status_filter=status_filter,
        order=order,
        order_from_query=order_from_query,
        page=page,
        total=total,
        total_pages=total_pages,
        start_item=start_item,
        end_item=end_item,
    )
