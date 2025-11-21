from flask import (
    Blueprint, render_template, abort,
    url_for, g, request
)
from MySQLdb.cursors import DictCursor
from db import get_db_connection

bookshelf_bp = Blueprint("bookshelf", __name__, template_folder="templates")


# ดึง users_id ของคนที่ล็อกอินอยู่ (รองรับทั้ง dict และ object)
def _get_current_user_id():
    user = getattr(g, "user", None)
    if not user:
        return None

    # ถ้าเก็บใน g.user เป็น dict
    if isinstance(user, dict):
        return user.get("users_id") or user.get("id")

    # ถ้าเป็น object ปกติ
    return getattr(user, "users_id", None)


@bookshelf_bp.route("/bookshelf")
def bookshelf_index():
    user_id = _get_current_user_id()
    if not user_id:
        # ถ้ามีหน้า login ให้เปลี่ยนไปใช้ redirect(...) แทน
        return abort(401)

    # tab: favorite / recent / rated (ตามปุ่มบน UI)
    tab = request.args.get("tab", "favorite").lower()
    # status filter: all / continue / done (ตาม dropdown)
    status_filter = request.args.get("status", "all").lower()

    conn = get_db_connection()

    try:
        with conn.cursor(DictCursor) as cur:
            # =========== เลือก SQL ตาม tab ===========
            if tab == "recent":
                # จาก reading_history: เรื่องที่อ่านล่าสุดของ user นี้
                sql = """
                SELECT
                    rh.novels_id,
                    n.title,
                    n.cover,
                    n.status AS novel_status,
                    u.username AS author_name,
                    IFNULL(vc.total_chapters, 0)        AS total_chapters,
                    IFNULL(vr.bayesian_avg, vr.raw_avg) AS avg_rating,
                    IFNULL(vr.votes, 0)                 AS rating_count,
                    rh.progress,
                    rh.last_read_at
                FROM (
                    SELECT
                        novels_id,
                        MAX(last_read_at) AS last_read_at,
                        MAX(progress)     AS progress
                    FROM reading_history
                    WHERE users_id = %s
                    GROUP BY novels_id
                ) AS rh
                JOIN novels n ON n.novels_id = rh.novels_id
                JOIN users  u ON u.users_id    = n.users_id
                LEFT JOIN v_novel_chapter_counts vc
                       ON vc.novels_id = n.novels_id
                LEFT JOIN v_novel_rating_stats vr
                       ON vr.novels_id = n.novels_id
                ORDER BY rh.last_read_at DESC, n.title;
                """
                cur.execute(sql, (user_id,))

            elif tab == "rated":
                # จาก ratings: เรื่องที่ user เคยให้คะแนน
                sql = """
                SELECT
                    r.novels_id,
                    n.title,
                    n.cover,
                    n.status AS novel_status,
                    u.username AS author_name,
                    IFNULL(vc.total_chapters, 0)        AS total_chapters,
                    IFNULL(vr.bayesian_avg, vr.raw_avg) AS avg_rating,
                    IFNULL(vr.votes, 0)                 AS rating_count,
                    NULL AS progress,
                    MAX(r.updated_at) AS last_read_at
                FROM ratings r
                JOIN novels n ON n.novels_id = r.novels_id
                JOIN users  u ON u.users_id  = n.users_id
                LEFT JOIN v_novel_chapter_counts vc
                       ON vc.novels_id = n.novels_id
                LEFT JOIN v_novel_rating_stats vr
                       ON vr.novels_id = n.novels_id
                WHERE r.users_id = %s
                GROUP BY
                    r.novels_id, n.title, n.cover, n.status,
                    u.username, vc.total_chapters, vr.bayesian_avg, vr.raw_avg, vr.votes
                ORDER BY last_read_at DESC, n.title;
                """
                cur.execute(sql, (user_id,))

            else:
                # favorite / bookshelf: เรื่องที่บันทึกในตาราง bookshelf ของ user นี้
                sql = """
                SELECT
                    b.novels_id,
                    n.title,
                    n.cover,
                    n.status AS novel_status,
                    u.username AS author_name,
                    IFNULL(vc.total_chapters, 0)        AS total_chapters,
                    IFNULL(vr.bayesian_avg, vr.raw_avg) AS avg_rating,
                    IFNULL(vr.votes, 0)                 AS rating_count,
                    rh.progress,
                    rh.last_read_at,
                    b.created_at
                FROM bookshelf b
                JOIN novels n ON n.novels_id = b.novels_id
                JOIN users  u ON u.users_id  = n.users_id
                LEFT JOIN v_novel_chapter_counts vc
                       ON vc.novels_id = n.novels_id
                LEFT JOIN v_novel_rating_stats vr
                       ON vr.novels_id = n.novels_id
                LEFT JOIN (
                    SELECT
                        novels_id,
                        MAX(last_read_at) AS last_read_at,
                        MAX(progress)     AS progress
                    FROM reading_history
                    WHERE users_id = %s
                    GROUP BY novels_id
                ) AS rh
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
    for row in rows:
        cover = row.get("cover") or "/static/cover/placeholder.jpg"
        total_chapters = int(row.get("total_chapters") or 0)
        avg_rating = float(row.get("avg_rating") or 0.0)
        rating_count = int(row.get("rating_count") or 0)
        progress = int(row.get("progress") or 0)
        novel_status = row.get("novel_status")  # เช่น 'จบแล้ว' หรือสถานะอื่นจาก novels

        # สถานะการอ่านใช้สำหรับ filter dropdown
        if novel_status == "จบแล้ว":
            read_status = "done"
        elif progress > 0 and progress < 100:
            read_status = "continue"
        elif progress >= 100:
            read_status = "done"
        else:
            read_status = "new"

        item = {
            "novels_id": row["novels_id"],
            "title": row["title"],
            "cover": cover,
            "author_name": row["author_name"],
            "total_chapters": total_chapters,
            "avg_rating": avg_rating,
            "rating_count": rating_count,
            "progress": progress,
            "read_status": read_status,
            "last_read_at": row.get("last_read_at"),
            # ปุ่ม "อ่านต่อ" → ไปหน้าอ่านนิยายของเรื่องนั้น (เปลี่ยน endpoint ให้ตรงของโปรเจกต์)
            "read_url": url_for("reading.read_novel", novels_id=row["novels_id"])
            if "reading.read_novel" in
               {r.endpoint for r in bookshelf_bp.root_path and bookshelf_bp.app.url_map.iter_rules()}
            else f"/reading/{row['novels_id']}"
        }

        items.append(item)

    # filter ตาม dropdown (status)
    if status_filter == "done":
        items = [i for i in items if i["read_status"] == "done"]
    elif status_filter == "continue":
        items = [i for i in items if i["read_status"] == "continue"]
    # "all" ไม่กรองอะไร

    return render_template(
        "bookshelf.html",
        items=items,
        active_tab=tab,
        status_filter=status_filter,
    )
