from flask import Blueprint, render_template, abort, url_for, g, request, jsonify, session
from werkzeug.exceptions import HTTPException
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
    """
    ✅ ห้าม fallback เป็น 1
    รองรับหลายรูปแบบที่โปรเจกต์มักเก็บ uid:
      - session['users_id'] / session['user_id'] / session['uid']
      - session['user'] เป็น dict แล้วมี ['users_id']
      - g.user เป็น dict แล้วมี ['users_id']
      - Flask-Login current_user.users_id
    """
    # 1) session simple keys
    uid = session.get('users_id') or session.get('user_id') or session.get('uid')
    if uid:
        try:
            return int(uid)
        except Exception:
            pass

    # 2) session['user'] dict
    u = session.get('user')
    if isinstance(u, dict) and u.get('users_id'):
        try:
            return int(u.get('users_id'))
        except Exception:
            pass

    # 3) g.user dict
    if getattr(g, 'user', None) and isinstance(g.user, dict) and g.user.get('users_id'):
        try:
            return int(g.user.get('users_id'))
        except Exception:
            pass

    # 4) Flask-Login (ถ้ามี)
    try:
        from flask_login import current_user
        if getattr(current_user, 'is_authenticated', False):
            return int(getattr(current_user, 'users_id'))
    except Exception:
        pass

    return None


def _writer_ctx() -> dict:
    """
    ✅ ทำให้ template มีตัวแปร writer เสมอ (กัน base.html/header พัง)
    """
    # 1) session['user'] dict
    u = session.get('user')
    if isinstance(u, dict):
        return {
            "users_id": u.get("users_id") or u.get("user_id") or u.get("id"),
            "username": u.get("username", ""),
            "role": u.get("role", ""),
        }

    # 2) g.user dict
    if isinstance(getattr(g, 'user', None), dict):
        return {
            "users_id": g.user.get("users_id"),
            "username": g.user.get("username", ""),
            "role": g.user.get("role", ""),
        }

    # 3) Flask-Login
    try:
        from flask_login import current_user
        if getattr(current_user, 'is_authenticated', False):
            return {
                "users_id": getattr(current_user, "users_id", None),
                "username": getattr(current_user, "username", ""),
                "role": getattr(current_user, "role", ""),
            }
    except Exception:
        pass

    # fallback (อย่างน้อยให้มี users_id)
    return {"users_id": _current_uid(), "username": "", "role": ""}


@mywrite_bp.app_context_processor
def inject_writer():
    """
    ✅ ทำให้ทุก template ใน blueprint นี้มีตัวแปร writer อัตโนมัติ
    """
    return {"writer": _writer_ctx()}


def _detail_url(novels_id: int) -> str:
    # ใช้กับ data-detail / data-copy
    try:
        return url_for('novel.detail', novels_id=novels_id)
    except Exception:
        return f'/novel/{novels_id}'


def _normalize_status(s: str) -> str:
    # กัน whitespace/อักขระแปลก ๆ
    return (s or '').replace('\u200b', '').strip()


# ---------- pages ----------
@mywrite_bp.route('/mywrite')
def mywrite_index():
    conn = None
    try:
        current_uid = _current_uid()
        if not current_uid:
            abort(401)

        status_filter = _normalize_status(request.args.get('status', ''))
        if status_filter and status_filter not in ALLOWED_STATUS:
            status_filter = ''

        conn = get_db_connection()
        with conn.cursor(DictCursor) as cur:
            where_status = "AND n.status = %s" if status_filter else ""
            sql = f"""
                SELECT
                  n.novels_id, n.title, n.status, n.cover,
                  COALESCE(n.updated_at, n.created_at) AS edited_at,
                  c.name AS category_name,
                  u.username AS author_username,

                  -- นับตอนทั้งหมด (รวมแบบร่าง/ยังไม่เผยแพร่ด้วย)
                  (SELECT COUNT(*) FROM chapters ch WHERE ch.novels_id = n.novels_id) AS total_chapters,
                  (SELECT COUNT(DISTINCT rh.users_id) FROM reading_history rh WHERE rh.novels_id = n.novels_id) AS readers_count,
                  (SELECT COUNT(*) FROM comments cm WHERE cm.novels_id = n.novels_id) AS comments_count,
                  (SELECT COUNT(*) FROM bookshelf b WHERE b.novels_id = n.novels_id) AS favorites_count,
                  (SELECT AVG(r.rating) FROM ratings r WHERE r.novels_id = n.novels_id) AS rating_avg
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
            nid = r["novels_id"]
            st = _normalize_status(r.get("status") or "แบบร่าง")
            if st not in ALLOWED_STATUS:
                st = "แบบร่าง"

            works.append({
                "novels_id": nid,
                "title": r.get("title") or "(ไม่มีชื่อเรื่อง)",
                "status": st,
                "cover_url": _cover_url(r.get("cover")),
                "category_name": r.get("category_name") or "ไม่ระบุหมวด",
                "author_username": r.get("author_username") or "—",
                # ใช้จำนวนตอนทั้งหมด (ไม่กรองเฉพาะที่เผยแพร่)
                "chapters": int(r.get("total_chapters") or r.get("chapters_count") or 0),
                "views": int(r.get("readers_count") or 0),
                "comments": int(r.get("comments_count") or 0),
                "favorites": int(r.get("favorites_count") or 0),
                "rating": float(r.get("rating_avg") or 0.0),
                "edited_at": r.get("edited_at"),
                "detail_url": _detail_url(nid),
            })

        return render_template(
            "mywrite.html",
            works=works,
            total_works=len(works),
            writer=_writer_ctx(),       # ✅ เผื่อ template อยากใช้ตรง ๆ
            writer_id=current_uid,      # ✅ เผื่อใช้ทำลิงก์/logic ง่าย ๆ
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"mywrite.index error: {e}")
        abort(500)
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


# ---------- APIs ----------
@mywrite_bp.route('/api/mywrite/<int:novel_id>/status', methods=['POST'])
def mywrite_update_status(novel_id: int):
    """
    อัปเดตสถานะนิยาย
    รองรับทั้ง JSON และ form-encoded:
      JSON: {"status": "..."}
      Form: status=...
    """
    conn = None
    try:
        current_uid = _current_uid()
        if not current_uid:
            return jsonify(ok=False, error="unauthorized", message="กรุณาเข้าสู่ระบบ"), 401

        data = request.get_json(silent=True) or {}
        incoming = data.get('status') or request.form.get('status') or ''
        new_status = _normalize_status(incoming)

        if new_status not in ALLOWED_STATUS:
            return jsonify(
                ok=False,
                error="invalid_status",
                message="status ต้องเป็น แบบร่าง/เผยแพร่/จบแล้ว",
                received=new_status
            ), 400

        conn = get_db_connection()
        with conn.cursor() as cur:
            # เช็ก ownership ก่อน
            cur.execute(
                "SELECT 1 FROM novels WHERE novels_id=%s AND users_id=%s LIMIT 1",
                (novel_id, current_uid)
            )
            if not cur.fetchone():
                return jsonify(
                    ok=False,
                    error="not_found_or_forbidden",
                    message="ไม่พบงานเขียน หรือคุณไม่มีสิทธิ์แก้ไข"
                ), 404

            cur.execute(
                "UPDATE novels SET status=%s, updated_at=NOW() WHERE novels_id=%s AND users_id=%s",
                (new_status, novel_id, current_uid)
            )

        conn.commit()
        return jsonify(ok=True, status=new_status)

    except Exception as e:
        print(f"mywrite.update_status error: {e}")
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return jsonify(ok=False, error="server_error", message="เกิดข้อผิดพลาดที่เซิร์ฟเวอร์"), 500
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


@mywrite_bp.route('/api/mywrite/bulk', methods=['POST'])
def mywrite_bulk_action():
    """
    body: { ids: [1,2,3], action: "publish"|"complete"|"draft"|"delete" }
    """
    conn = None
    try:
        current_uid = _current_uid()
        if not current_uid:
            return jsonify(ok=False, error="unauthorized", message="กรุณาเข้าสู่ระบบ"), 401

        data = request.get_json(silent=True) or {}
        ids = data.get('ids') or []
        action = _normalize_status(data.get('action') or '')

        if not isinstance(ids, list) or not ids:
            return jsonify(ok=False, error="bad_request", message="ids ไม่ถูกต้อง"), 400

        clean_ids = []
        for x in ids:
            try:
                clean_ids.append(int(x))
            except Exception:
                pass
        if not clean_ids:
            return jsonify(ok=False, error="bad_request", message="ids ไม่ถูกต้อง"), 400

        conn = get_db_connection()
        with conn.cursor() as cur:
            if action == 'delete':
                cur.execute(
                    f"DELETE FROM novels WHERE users_id=%s AND novels_id IN ({','.join(['%s']*len(clean_ids))})",
                    [current_uid, *clean_ids]
                )
            else:
                st = (
                    'เผยแพร่' if action == 'publish' else
                    'จบแล้ว'  if action == 'complete' else
                    'แบบร่าง' if action == 'draft' else
                    None
                )
                if not st:
                    return jsonify(ok=False, error="bad_request", message="action ไม่ถูกต้อง"), 400

                cur.execute(
                    f"UPDATE novels SET status=%s, updated_at=NOW() "
                    f"WHERE users_id=%s AND novels_id IN ({','.join(['%s']*len(clean_ids))})",
                    [st, current_uid, *clean_ids]
                )

        conn.commit()
        return jsonify(ok=True)

    except Exception as e:
        print(f"mywrite.bulk error: {e}")
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return jsonify(ok=False, error="server_error", message="เกิดข้อผิดพลาดที่เซิร์ฟเวอร์"), 500
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


@mywrite_bp.route('/api/mywrite/<int:novel_id>', methods=['DELETE'])
def mywrite_delete_one(novel_id: int):
    conn = None
    try:
        current_uid = _current_uid()
        if not current_uid:
            return jsonify(ok=False, error="unauthorized", message="กรุณาเข้าสู่ระบบ"), 401

        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM novels WHERE novels_id=%s AND users_id=%s",
                (novel_id, current_uid)
            )
        conn.commit()
        return jsonify(ok=True)

    except Exception as e:
        print(f"mywrite.delete_one error: {e}")
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return jsonify(ok=False, error="server_error", message="เกิดข้อผิดพลาดที่เซิร์ฟเวอร์"), 500
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
