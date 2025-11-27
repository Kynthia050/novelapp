from flask import Blueprint, render_template, request, jsonify, abort
from db import get_db_connection, query_all, query_one
from datetime import datetime

writerwork_bp = Blueprint('writerwork', __name__, template_folder='templates')

# ---------- Helpers ----------
def _writer_overview(users_id: int):
    """ดึงข้อมูลโปรไฟล์ + สรุปผลงานของผู้เขียน"""
    sql = """
    SELECT u.users_id, u.username, u.pfpic,
           COUNT(n.novels_id) AS work_count,
           COALESCE(SUM(b.bookshelf_users), 0) AS total_bookshelf
    FROM users u
    LEFT JOIN novels n ON n.users_id = u.users_id
    LEFT JOIN v_novel_bookshelf_counts b ON b.novels_id = n.novels_id
    WHERE u.users_id = %s
    GROUP BY u.users_id, u.username, u.pfpic
    """
    return query_one(sql, (users_id,))

def _writer_works(users_id: int):
    """รายการนิยายของผู้เขียน + สถิติโดยย่อ (ตอน, ยอดดู, ไลก์, บุ๊คมาร์ก, คอมเมนต์, เรตติ้ง)"""
    sql = """
    SELECT
      n.novels_id, n.title, n.cover, n.updated_at, n.status,
      COALESCE(cc.total_chapters, 0) AS chapters,
      COALESCE(vh.views, 0) AS views,
      COALESCE(lk.likes, 0) AS likes,
      COALESCE(bc.bookshelf_users, 0) AS bookmarks,
      COALESCE(cm.comments, 0) AS comments_count,
      COALESCE(rs.bayesian_avg, rs.raw_avg, 0) AS rating_avg
    FROM novels n
    LEFT JOIN v_novel_chapter_counts cc ON cc.novels_id = n.novels_id
    LEFT JOIN (
        SELECT novels_id, COUNT(*) AS views
        FROM reading_history
        GROUP BY novels_id
    ) vh ON vh.novels_id = n.novels_id
    LEFT JOIN (
        SELECT c.novels_id, COUNT(*) AS likes
        FROM chapter_likes cl
        JOIN chapters c ON c.chapters_id = cl.chapters_id
        GROUP BY c.novels_id
    ) lk ON lk.novels_id = n.novels_id
    LEFT JOIN v_novel_bookshelf_counts bc ON bc.novels_id = n.novels_id
    LEFT JOIN (
        SELECT novels_id, COUNT(*) AS comments
        FROM comments
        GROUP BY novels_id
    ) cm ON cm.novels_id = n.novels_id
    LEFT JOIN v_novel_rating_stats rs ON rs.novels_id = n.novels_id
    WHERE n.users_id = %s
    ORDER BY n.updated_at DESC, n.novels_id DESC
    """
    return query_all(sql, (users_id,))

def _novel_exists(novel_id: int):
    row = query_one("SELECT novels_id, users_id, title, updated_at FROM novels WHERE novels_id = %s", (novel_id,))
    return row

# ---------- Page: /writer/<writer_id>/works ----------
@writerwork_bp.get("/writer/<int:writer_id>/works")
def writer_works(writer_id: int):
    writer = _writer_overview(writer_id)
    if not writer:
        abort(404)

    works = _writer_works(writer_id)

    # แปลงข้อมูลสำหรับฝั่ง template (ทำ data-* ที่การ์ด)
    cards = []
    for w in works:
        cards.append({
            "novels_id": w["novels_id"],
            "title": w["title"],
            "cover": w["cover"] or "/static/cover/placeholder.jpg",
            "updated_at": w["updated_at"].isoformat() if isinstance(w["updated_at"], datetime) else w["updated_at"],
            "status": ("finished" if (w["status"] or "").lower() in ("จบแล้ว", "finished", "complete", "completed") else "ongoing"),
            "chapters": int(w["chapters"] or 0),
            "views": int(w["views"] or 0),
            "likes": int(w["likes"] or 0),
            "bookmarks": int(w["bookmarks"] or 0),
            "comments": int(w["comments_count"] or 0),
            "rating_avg": float(w["rating_avg"] or 0.0),
            "writer_name": writer["username"],
        })

    # NOTE: current_user_id คุณสามารถโยงกับระบบ auth ได้จริง
    current_user_id = request.args.get("as_user", type=int)  # ชั่วคราว: ผ่าน ?as_user=42 เพื่อทดสอบปุ่มสถิติ
    return render_template(
        "writerwork.html",
        current_user_id=current_user_id or writer["users_id"],
        writer_id=writer["users_id"],
        writer=writer,
        works=cards
    )

# ---------- API: /api/novels/<novel_id>/stats ----------
@writerwork_bp.get("/api/novels/<int:novel_id>/stats")
def novel_stats(novel_id: int):
    novel = _novel_exists(novel_id)
    if not novel:
        abort(404)

    days = max(1, min(request.args.get("days", default=28, type=int), 180))  # ป้องกันยิงยาวเกิน

    conn = get_db_connection()
    cur = conn.cursor()

    # 1) ตัวเลขรวม
    # - chapters (จาก view), views/unique_readers (จาก reading_history), likes (chapter_likes), bookmarks (view), comments, rating
    totals_sql = """
    SELECT
      n.novels_id,
      COALESCE(cc.total_chapters, 0) AS chapters,
      (SELECT COUNT(*) FROM reading_history rh WHERE rh.novels_id = n.novels_id) AS views,
      (SELECT COUNT(DISTINCT rh.users_id) FROM reading_history rh WHERE rh.novels_id = n.novels_id) AS readers_unique,
      (SELECT COUNT(*) FROM chapter_likes cl JOIN chapters c ON c.chapters_id = cl.chapters_id WHERE c.novels_id = n.novels_id) AS likes,
      COALESCE(bc.bookshelf_users, 0) AS bookmarks,
      (SELECT COUNT(*) FROM comments cm WHERE cm.novels_id = n.novels_id) AS comments_count,
      COALESCE(rs.bayesian_avg, rs.raw_avg, 0) AS rating_avg,
      n.updated_at
    FROM novels n
    LEFT JOIN v_novel_chapter_counts cc ON cc.novels_id = n.novels_id
    LEFT JOIN v_novel_bookshelf_counts bc ON bc.novels_id = n.novels_id
    LEFT JOIN v_novel_rating_stats rs ON rs.novels_id = n.novels_id
    WHERE n.novels_id = %s
    """
    cur.execute(totals_sql, (novel_id,))
    totals = cur.fetchone()

    # 2) completion_rate จาก reading_history:
    #    - เอาความคืบหน้าสูงสุดต่อ user ในเรื่องนี้
    #    - completed = max_progress >= 95
    completion_sql = """
    WITH last_per_user AS (
      SELECT users_id, MAX(progress) AS max_progress
      FROM reading_history
      WHERE novels_id = %s
      GROUP BY users_id
    )
    SELECT
      COUNT(*) AS readers,
      SUM(CASE WHEN COALESCE(max_progress,0) >= 95 THEN 1 ELSE 0 END) AS completed
    FROM last_per_user
    """
    cur.execute(completion_sql, (novel_id,))
    comp_row = cur.fetchone() or {"readers": 0, "completed": 0}
    readers_total = int(comp_row["readers"] or 0)
    completed_total = int(comp_row["completed"] or 0)
    completion_rate = (completed_total / readers_total) if readers_total else 0.0

    # 3) timeseries แบบละเอียด (รายวัน) จาก reading_history:
    #    - views: จำนวน event ต่อวัน
    #    - unique_readers: ผู้ใช้ไม่ซ้ำต่อวัน
    #    - new_completions: จำนวนผู้ใช้ที่ "เพิ่ง" ถึง 95% ครั้งแรกในวันนั้น
    #    - avg_progress: ค่าเฉลี่ย progress สูงสุดต่อ user ในวันนั้น
    ts_sql = f"""
    WITH day_events AS (
      SELECT DATE(last_read_at) AS d,
             COUNT(*) AS views,
             COUNT(DISTINCT users_id) AS readers
      FROM reading_history
      WHERE novels_id = %s
        AND last_read_at >= CURDATE() - INTERVAL %s DAY
      GROUP BY DATE(last_read_at)
    ),
    base AS (
      SELECT DATE(last_read_at) AS d, users_id, MAX(COALESCE(progress,0)) AS day_max
      FROM reading_history
      WHERE novels_id = %s
        AND last_read_at >= CURDATE() - INTERVAL %s DAY
      GROUP BY DATE(last_read_at), users_id
    ),
    cumprog AS (
      SELECT d, users_id, day_max,
             MAX(day_max) OVER (PARTITION BY users_id ORDER BY d
                                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_max
      FROM base
    ),
    flagged AS (
      SELECT d, users_id, day_max, cum_max,
             LAG(cum_max, 1, 0) OVER (PARTITION BY users_id ORDER BY d) AS prev_cum
      FROM cumprog
    )
    SELECT
      de.d AS date,
      de.views,
      de.readers AS unique_readers,
      SUM(CASE WHEN f.cum_max >= 95 AND f.prev_cum < 95 THEN 1 ELSE 0 END) AS new_completions,
      ROUND(AVG(f.day_max), 2) AS avg_progress
    FROM day_events de
    LEFT JOIN flagged f ON f.d = de.d
    GROUP BY de.d
    ORDER BY de.d
    """
    cur.execute(ts_sql, (novel_id, days, novel_id, days))
    rows = cur.fetchall() or []

    cur.close()

    payload = {
        "novels_id": novel_id,
        "title": novel["title"],
        "chapters": int(totals["chapters"] or 0),
        "views": int(totals["views"] or 0),
        "readers_unique": int(totals["readers_unique"] or 0),
        "likes": int(totals["likes"] or 0),
        "bookmarks": int(totals["bookmarks"] or 0),
        "comments_count": int(totals["comments_count"] or 0),
        "rating_avg": float(totals["rating_avg"] or 0.0),
        "completion_rate": float(completion_rate),
        "updated_at": totals["updated_at"].isoformat() if isinstance(totals["updated_at"], datetime) else totals["updated_at"],
        "timeseries": [
            {
                "date": r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"]),
                "views": int(r["views"] or 0),
                "unique_readers": int(r["unique_readers"] or 0),
                "new_completions": int(r["new_completions"] or 0),
                "avg_progress": float(r["avg_progress"] or 0.0),
            } for r in rows
        ]
    }
    return jsonify(payload)
