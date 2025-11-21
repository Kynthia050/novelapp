from flask import Blueprint, render_template, abort, url_for, g, request, jsonify
from MySQLdb.cursors import DictCursor
from db import get_db_connection
import os

mywrite_bp = Blueprint('mywrite', __name__, template_folder='templates')

ALLOWED_STATUS = {'แบบร่าง', 'เผยแพร่', 'จบแล้ว'}

# ---------- helpers ----------
def _cover_url(cover_path: str | None) -> str:
    if cover_path:
        filename = os.path.basename(str(cover_path))
        return url_for('static', filename=f'cover/{filename}')
    return url_for('static', filename='cover/placeholder.jpg')

def _current_uid():
    # ปรับให้เข้ากับระบบล็อกอินของคุณได้
    return (g.user or {}).get('users_id') if hasattr(g, 'user') and g.user else 1

def _detail_url(novels_id: int) -> str:
    # ส่งไปที่หน้า edit_novel แทน
    try:
        return url_for('editnovel.edit_novel', novels_id=novels_id)
    except Exception:
        return f'/edit_novel/{novels_id}'

# ---------- pages ----------
@mywrite_bp.route('/mywrite')
def mywrite_index():

    try:
        current_uid = _current_uid()
        status_filter = request.args.get('status')
        if status_filter and status_filter not in ALLOWED_STATUS:
            status_filter = None  # กันพลาดจากค่าที่ไม่ถูกต้อง

        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            where_status = "AND n.status = %s" if status_filter else ""

            sql = f"""
                SELECT
                  n.novels_id, n.title, n.status, n.cover,
                  COALESCE(n.updated_at, n.created_at) AS edited_at,
                  c.name AS category_name,
                  u.username AS author_username,

                  (SELECT COUNT(*) FROM chapters ch WHERE ch.novels_id = n.novels_id) AS chapters_count,
                  (SELECT COUNT(DISTINCT rh.users_id) FROM reading_history rh WHERE rh.novels_id = n.novels_id) AS readers_count,
                  (SELECT COUNT(*) FROM comments cm WHERE cm.novels_id = n.novels_id) AS comments_count,
                  (SELECT COUNT(*) FROM bookshelf b WHERE b.novels_id = n.novels_id) AS favorites_count

                FROM novels n
                LEFT JOIN categories c ON c.cate_id = n.cate_id
                LEFT JOIN users u      ON u.users_id = n.users_id
                WHERE n.users_id = %s
                {where_status}
                ORDER BY edited_at DESC, n.novels_id DESC
            """
            params = [current_uid] + ([status_filter] if status_filter else [])
            cur.execute(sql, params)
            rows = cur.fetchall()

        works = []
        for r in rows:
            works.append({
                "novels_id": r["novels_id"],
                "title": r["title"] or "(ไม่มีชื่อเรื่อง)",
                "status": r.get("status") or "แบบร่าง",
                "cover_url": _cover_url(r.get("cover")),
                "category_name": r.get("category_name") or "ไม่ระบุหมวด",
                "author_username": r.get("author_username") or "—",
                "chapters": int(r.get("chapters_count") or 0),
                "views": int(r.get("readers_count") or 0),
                "comments": int(r.get("comments_count") or 0),
                "favorites": int(r.get("favorites_count") or 0),
                "edited_at": r.get("edited_at"),
                "detail_url": _detail_url(r["novels_id"]),
            })

        return render_template("mywrite.html", works=works, total_works=len(works))

    except Exception as e:
        print(f"mywrite.index error: {e}")
        abort(500)

# ---------- APIs ----------
@mywrite_bp.route('/api/mywrite/<int:novel_id>/status', methods=['POST'])
def mywrite_update_status(novel_id: int):
    """
    อัปเดตสถานะนิยาย (ใช้กับ dropdown ในหน้า mywrite.html)
    Request JSON: {"status": "แบบร่าง" | "เผยแพร่" | "จบแล้ว"}
    Response JSON: {"ok": true, "status": "..."} หรือ error code
    """
    try:
        data = request.get_json(silent=True) or {}
        new_status = (data.get('status') or '').strip()

        if new_status not in ALLOWED_STATUS:
            return jsonify(ok=False, error="invalid_status",
                           message="status ต้องเป็น แบบร่าง/เผยแพร่/จบแล้ว"), 400

        current_uid = _current_uid()
        conn = get_db_connection()
        with conn.cursor() as cur:
            # อัปเดตเฉพาะงานเขียนของผู้ใช้คนนั้น
            cur.execute(
                "UPDATE novels SET status=%s WHERE novels_id=%s AND users_id=%s",
                (new_status, novel_id, current_uid),
            )
            if cur.rowcount == 0:
                # ไม่ใช่เจ้าของงานเขียนหรือไม่พบงานเขียน
                return jsonify(ok=False, error="not_found_or_forbidden"), 404
        conn.commit()

        return jsonify(ok=True, status=new_status)

    except Exception as e:
        print(f"mywrite.update_status error: {e}")
        return jsonify(ok=False, error="server_error"), 500
