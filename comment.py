# comment.py
from flask import Blueprint, render_template, request, jsonify
from db import get_db_connection  # ใช้ตัวเดียวกับ blueprint อื่นในโปรเจกต์

comment_bp = Blueprint(
    "comment",
    __name__,
    template_folder="templates"   # ใช้โฟลเดอร์ templates เดิมของโปรเจกต์
)

def _fetch_comments(novels_id=None, limit=20):
    """
    ดึงคอมเมนต์จากตาราง comments
    - ถ้าระบุ novels_id จะ filter ตามเรื่อง
    - แปลงผลลัพธ์เป็น list[dict] พร้อมชื่อคอลัมน์
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT
                    cm_id,
                    users_id,
                    novels_id,
                    content,
                    created_at
                FROM comments
            """
            params = []
            if novels_id:
                sql += " WHERE novels_id = %s"
                params.append(novels_id)

            sql += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            comments = [dict(zip(cols, row)) for row in rows]
            return comments
    finally:
        conn.close()


# -------- หน้า HTML สำหรับ carousel ความเห็น --------
@comment_bp.route("/comments")
def comments_page():
    """
    /comments หรือ /comments?novels_id=1001
    จะ render comment.html พร้อมข้อมูลจาก DB
    """
    novels_id = request.args.get("novels_id", type=int)
    comments = _fetch_comments(novels_id=novels_id, limit=20)

    return render_template(
        "comment.html",
        comments=comments,
        novels_id=novels_id
    )


# -------- Endpoint แบบ JSON (เผื่ออนาคตจะดึงด้วย JS) --------
@comment_bp.route("/api/comments")
def comments_api():
    novels_id = request.args.get("novels_id", type=int)
    comments = _fetch_comments(novels_id=novels_id, limit=50)
    return jsonify(comments)
