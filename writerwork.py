from flask import Blueprint, render_template, abort, request, jsonify, session, g, url_for
from werkzeug.exceptions import HTTPException
from MySQLdb.cursors import DictCursor
from db import get_db_connection
import os
from datetime import datetime, timedelta

writerwork_bp = Blueprint('writerwork', __name__, template_folder='templates')

# ---------- helpers ----------
def _cover_url(cover_path: str | None) -> str:
    if cover_path:
        s = str(cover_path).strip()
        # ถ้าเป็น url หรือ path แบบ absolute ให้ใช้เลย
        if s.startswith(('http://', 'https://', '/')):
            return s
        filename = os.path.basename(s)
        return url_for('static', filename=f'cover/{filename}')
    return url_for('static', filename='cover/placeholder.jpg')

def _pfpic_url(pfpic: str | None) -> str:
    """
    ✅ ของจริงใน DB มักเก็บเป็น 'profile/xxx.png' (อยู่ใต้ static/)
    - ถ้าเป็น http(s) หรือ /... ใช้เลย
    - ถ้าเป็น 'static/...' ตัด static/ ออกแล้ว url_for('static', filename=...)
    - ถ้ามี / อยู่แล้ว ให้ถือว่าเป็น path ใต้ static เช่น profile/...
    - ถ้าเป็นชื่อไฟล์ล้วน ค่อยเดาโฟลเดอร์ profile/ ก่อน
    """
    if pfpic:
        s = str(pfpic).strip()
        if not s:
            return url_for('static', filename='img/avatar-placeholder.jpg')

        if s.startswith(('http://', 'https://', '/')):
            return s

        if s.startswith('static/'):
            s = s[len('static/'):]

        if '/' in s:
            return url_for('static', filename=s)

        filename = os.path.basename(s)
        return url_for('static', filename=f'profile/{filename}')

    return url_for('static', filename='img/avatar-placeholder.jpg')

def _current_uid() -> int | None:
    """
    ✅ ไม่พึ่ง flask_login (กัน Pylance ฟ้อง + โปรเจกต์คุณใช้ session/g เป็นหลัก)
    รองรับ:
      - session['users_id'] / session['user_id'] / session['uid']
      - session['user'] dict -> users_id
      - g.user dict -> users_id
    """
    uid = session.get('users_id') or session.get('user_id') or session.get('uid')
    if uid:
        try:
            return int(uid)
        except Exception:
            pass

    u = session.get('user')
    if isinstance(u, dict) and u.get('users_id'):
        try:
            return int(u.get('users_id'))
        except Exception:
            pass

    if getattr(g, 'user', None) and isinstance(g.user, dict) and g.user.get('users_id'):
        try:
            return int(g.user.get('users_id'))
        except Exception:
            pass

    return None

def _iso(dt) -> str:
    if not dt:
        return ''
    if hasattr(dt, 'isoformat'):
        return dt.isoformat()
    return str(dt)

# ---------- pages ----------
@writerwork_bp.route('/writer/<int:writer_id>/works')
def writer_works(writer_id: int):
    conn = None
    try:
        current_uid = _current_uid()
        is_owner = bool(current_uid and int(current_uid) == int(writer_id))

        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            # writer profile
            cur.execute("""
                SELECT users_id, username, pfpic
                FROM users
                WHERE users_id=%s
                LIMIT 1
            """, (writer_id,))
            writer = cur.fetchone()
            if not writer:
                abort(404)

            # counts (งานเขียน + เพิ่มเข้าชั้นรวมทุกเรื่อง)
            cur.execute("SELECT COUNT(*) AS c FROM novels WHERE users_id=%s", (writer_id,))
            work_count = int((cur.fetchone() or {}).get('c') or 0)

            # ใช้ VIEW v_novel_bookshelf_counts (ของจริงมี novels_id, bookshelf_count)
            cur.execute("""
                SELECT COALESCE(SUM(vbc.bookshelf_count),0) AS total_bookshelf
                FROM novels n
                LEFT JOIN v_novel_bookshelf_counts vbc ON vbc.novels_id = n.novels_id
                WHERE n.users_id=%s
            """, (writer_id,))
            total_bookshelf = int((cur.fetchone() or {}).get('total_bookshelf') or 0)

            # works list
            cur.execute("""
                SELECT
                    n.novels_id,
                    n.title,
                    n.cover,
                    n.status,
                    n.updated_at,
                    n.views,
                    c.name AS category_name,
                    (SELECT COUNT(*) FROM chapters ch WHERE ch.novels_id = n.novels_id) AS chapters_count,
                    COALESCE(vbc.bookshelf_count,0) AS bookmarks_count,
                    (SELECT COUNT(*) FROM comments cm WHERE cm.novels_id = n.novels_id) AS comments_count,
                    (SELECT AVG(r.rating) FROM ratings r WHERE r.novels_id = n.novels_id) AS rating_avg
                FROM novels n
                LEFT JOIN categories c ON c.cate_id = n.cate_id
                LEFT JOIN v_novel_bookshelf_counts vbc ON vbc.novels_id = n.novels_id
                WHERE n.users_id=%s
                ORDER BY n.updated_at DESC, n.novels_id DESC
            """, (writer_id,))
            rows = cur.fetchall()

        writer_out = {
            "users_id": writer["users_id"],
            "username": writer.get("username") or "Writer",
            "pfpic": _pfpic_url(writer.get("pfpic")),
            "work_count": work_count,
            "total_bookshelf": total_bookshelf
        }

        works = []
        for r in (rows or []):
            works.append({
                "novels_id": r["novels_id"],
                "title": r.get("title") or "(ไม่มีชื่อเรื่อง)",
                "cover": _cover_url(r.get("cover")),
                "status": r.get("status") or "เผยแพร่",
                "updated_at": _iso(r.get("updated_at")),
                "views": int(r.get("views") or 0),
                "chapters": int(r.get("chapters_count") or 0),
                "bookmarks": int(r.get("bookmarks_count") or 0),
                "comments": int(r.get("comments_count") or 0),
                "rating_avg": float(r.get("rating_avg") or 0.0),
                "writer_name": writer_out["username"],
                "owner_id": writer_id,  # เผื่อใช้ใน template
            })

        return render_template(
            "writerwork.html",
            writer=writer_out,
            works=works,
            writer_id=writer_id,
            current_user_id=current_uid,
            is_owner=is_owner
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"writerwork.writer_works error: {e}")
        abort(500)
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

# ---------- APIs ----------
@writerwork_bp.route('/api/writerwork/novels/<int:novel_id>/stats')
def novel_stats(novel_id: int):
    """
    ✅ อนุญาตเฉพาะ "เจ้าของนิยาย" เท่านั้น
    คืนค่า: chapters, views, bookmarks, comments_count, rating_avg, updated_at, timeseries(views 7 วัน)
    """
    conn = None
    try:
        current_uid = _current_uid()
        if not current_uid:
            return jsonify(ok=False, error="unauthorized"), 401

        days = request.args.get('days', '7')
        try:
            days = max(1, min(90, int(days)))
        except Exception:
            days = 7

        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            # owner check + base info
            cur.execute("""
                SELECT n.novels_id, n.users_id, n.title, n.updated_at, n.views,
                       (SELECT COUNT(*) FROM chapters ch WHERE ch.novels_id = n.novels_id) AS chapters,
                       COALESCE(vbc.bookshelf_count,0) AS bookmarks,
                       (SELECT COUNT(*) FROM comments cm WHERE cm.novels_id = n.novels_id) AS comments_count,
                       (SELECT AVG(r.rating) FROM ratings r WHERE r.novels_id = n.novels_id) AS rating_avg
                FROM novels n
                LEFT JOIN v_novel_bookshelf_counts vbc ON vbc.novels_id = n.novels_id
                WHERE n.novels_id=%s
                LIMIT 1
            """, (novel_id,))
            row = cur.fetchone()
            if not row:
                return jsonify(ok=False, error="not_found"), 404

            if int(row["users_id"]) != int(current_uid):
                return jsonify(ok=False, error="forbidden"), 403

            # timeseries: ใช้ reading_history.last_read_at นับการเข้าชมรายวัน (7 วันล่าสุด)
            # ถ้าโปรเจกต์คุณนับ views แบบอื่น ค่อยปรับ query ตรงนี้ได้
            start = (datetime.now() - timedelta(days=days-1)).date()
            cur.execute("""
                SELECT DATE(rh.last_read_at) AS d, COUNT(*) AS views
                FROM reading_history rh
                WHERE rh.novels_id=%s AND rh.last_read_at >= %s
                GROUP BY DATE(rh.last_read_at)
                ORDER BY d ASC
            """, (novel_id, start))
            ts_rows = cur.fetchall() or []

        # fill missing days
        ts_map = {}
        for x in ts_rows:
            d = x.get("d")
            if d:
                ts_map[str(d)] = int(x.get("views") or 0)

        series = []
        for i in range(days):
            di = (start + timedelta(days=i))
            key = str(di)
            series.append({"date": key, "views": ts_map.get(key, 0)})

        return jsonify(
            ok=True,
            title=row.get("title") or "นิยาย",
            chapters=int(row.get("chapters") or 0),
            views=int(row.get("views") or 0),
            bookmarks=int(row.get("bookmarks") or 0),
            comments_count=int(row.get("comments_count") or 0),
            rating_avg=float(row.get("rating_avg") or 0.0),
            updated_at=_iso(row.get("updated_at")),
            timeseries=series
        )

    except Exception as e:
        print(f"writerwork.novel_stats error: {e}")
        return jsonify(ok=False, error="server_error"), 500
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
