from flask import render_template, request, jsonify, Blueprint, session
from auth import roles_required          # ใช้ระบบสิทธิเดิมของคุณ
from db import get_db_connection
import math
from datetime import datetime

noti_bp = Blueprint('noti', __name__, template_folder='templates')

TYPE_LABELS = {
    "new_chapter": "ตอนใหม่",
    "chapter_updated": "แก้ไขตอน",
    "comment": "คอมเมนต์ใหม่",
    "favorite": "กดหัวใจ",
    "bookshelf_add": "เพิ่มเข้าชั้นหนังสือ",
    "rating": "มีการให้คะแนน",
}

TYPE_EMOJI = {
    "new_chapter": "🆕",
    "chapter_updated": "✏️",
    "comment": "💬",
    "favorite": "🎯",
    "bookshelf_add": "📚",
    "rating": "⭐",
}

def build_filters(user_id, tab, typ, q):
    where = ["n.users_id = %s"]
    params = [user_id]
    if tab == "unread":
        where.append("n.is_read = 0")
    if typ:
        where.append("n.type = %s")
        params.append(typ)
    if q:
        where.append("(n.message LIKE %s OR nv.title LIKE %s)")
        like = f"%{q}%"
        params.extend([like, like])
    return " AND ".join(where), params

@noti_bp.route("/notifications")
@roles_required('user')
def notifications_page():
    # ดึงจาก session จริง ๆ (ตั้งค่าโดยระบบ login ของคุณ)
    try:
        user_id = int(session["user_id"])
    except Exception:
        return jsonify({"error": "unauthorized"}), 401

    tab = request.args.get("tab", "all")
    typ = request.args.get("type") or ""
    q = request.args.get("q") or ""
    page = max(int(request.args.get("page", "1")), 1)
    page_size = 20

    where_sql, params = build_filters(user_id, tab, typ, q)

    base_select = f"""
        FROM notifications n
        LEFT JOIN novels nv ON nv.novels_id = n.novel_id
        LEFT JOIN chapters ch ON ch.chapters_id = n.chapter_id
        WHERE {where_sql}
    """

    conn = get_db_connection()
    with conn.cursor() as cur:
        # นับทั้งหมดเพื่อคำนวณหน้า
        cur.execute(f"SELECT COUNT(*) AS cnt {base_select}", params)
        total = cur.fetchone()["cnt"]
        total_pages = max(math.ceil(total / page_size), 1)
        offset = (page - 1) * page_size

        # นับยังไม่ได้อ่านทั้งหมด (badge)
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM notifications WHERE users_id=%s AND is_read=0",
            (user_id,)
        )
        unread_count = cur.fetchone()["cnt"]

        # ดึงรายการตามหน้า
        cur.execute(f"""
            SELECT
              n.notification_id, n.type, n.message, n.is_read, n.created_at,
              n.novel_id, n.chapter_id, n.comment_id, n.actor_user_id,
              nv.title AS novel_title, ch.chapter_no
            {base_select}
            ORDER BY n.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [page_size, offset])
        rows = cur.fetchall()

    # เตรียมข้อมูลก่อนส่งให้ template
    for r in rows:
        r["type_label"] = TYPE_LABELS.get(r["type"], r["type"])
        r["type_icon"]  = TYPE_EMOJI.get(r["type"], "🔔")
        r["created_at_display"] = (
            r["created_at"].strftime("%Y-%m-%d %H:%M")
            if isinstance(r["created_at"], datetime) else str(r["created_at"])
        )

    # --- เปลี่ยนให้เรนเดอร์ panel เดียว + รองรับ container_id จาก query ---
    context = dict(
        notifications=rows,
        page=page,
        total_pages=total_pages,
        tab=tab,
        q=q,
        sel_type=typ,
        type_labels=TYPE_LABELS,
        unread_count=unread_count,
        container_id=request.args.get("container_id", "notiContainer"),  # จะส่ง id เองก็ได้
    )
    return render_template("notification_panel.html", **context)


# ========= Actions =========

@noti_bp.post("/notifications/<int:nid>/read")
@roles_required('user')
def mark_read(nid):
    read_val = 1 if request.form.get("read", "1") == "1" else 0
    uid = int(session["user_id"])
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE notifications SET is_read=%s WHERE notification_id=%s AND users_id=%s",
            (read_val, nid, uid)
        )
        if cur.rowcount == 0:
            return jsonify({"ok": False, "error": "not_found_or_no_permission"}), 404
    return jsonify({"ok": True, "notification_id": nid, "is_read": read_val})

@noti_bp.post("/notifications/mark-all-read")
@roles_required('user')
def mark_all_read():
    uid = int(session["user_id"])
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE notifications SET is_read=1 WHERE users_id=%s AND is_read=0", (uid,))
    return jsonify({"ok": True})

@noti_bp.post("/notifications/<int:nid>/delete")
@roles_required('user')
def delete_notification(nid):
    uid = int(session["user_id"])
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM notifications WHERE notification_id=%s AND users_id=%s", (nid, uid))
        if cur.rowcount == 0:
            return jsonify({"ok": False, "error": "not_found_or_no_permission"}), 404
    return jsonify({"ok": True, "deleted": nid})
