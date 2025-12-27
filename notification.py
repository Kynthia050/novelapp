from flask import render_template, request, jsonify, Blueprint, session, url_for
from auth import roles_required
from db import get_db_connection
from slug_utils import novel_detail_url
import math
from datetime import datetime
from typing import Optional

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

CLICKABLE_TYPES = {"new_chapter", "chapter_updated", "comment", "favorite", "bookshelf_add", "rating"}
TITLE_TYPES = {"comment", "favorite", "bookshelf_add"}
MAX_PANEL_ITEMS = 100
COMMENT_ANCHOR = "#commentsSection"
COUNT_LABELS = {
    "comment": "ความคิดเห็น",
    "favorite": "คน",
    "bookshelf_add": "คน",
    "rating": "ครั้ง",
    "new_chapter": "ครั้ง",
    "chapter_updated": "ครั้ง",
}


def _current_user_id():
    uid = session.get("users_id") or session.get("user_id") or session.get("uid")
    if uid:
        try:
            return int(uid)
        except Exception:
            pass

    u = session.get("user")
    if isinstance(u, dict) and u.get("users_id"):
        try:
            return int(u.get("users_id"))
        except Exception:
            pass

    return None


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


def _build_target_url(row: dict) -> Optional[str]:
    novel_id = row.get("novel_id")
    if not novel_id:
        return None

    noti_type = (row.get("type") or "").strip().lower()
    chapter_no = row.get("chapter_no")
    comment_id = row.get("comment_id")

    if noti_type in {"new_chapter", "chapter_updated"} and chapter_no:
        try:
            return url_for("reading.read_chapter", novels_id=novel_id, chapter_no=int(chapter_no))
        except Exception:
            pass

    try:
        detail_url = novel_detail_url(novel_id, row.get("novel_title"))
    except Exception:
        return None

    if noti_type == "comment":
        anchor = COMMENT_ANCHOR
        if comment_id:
            anchor = f"#comment-{comment_id}"
        return detail_url + anchor

    return detail_url


def _hydrate_notification(row: dict) -> dict:
    created_at = row.get("created_at")
    if created_at:
        row["created_at_display"] = (
            created_at.strftime("%Y-%m-%d %H:%M")
            if isinstance(created_at, datetime) else str(created_at)
        )

    novel_title = (row.get("novel_title") or "").strip()
    message = (row.get("message") or "").strip()
    if row.get("type") in TITLE_TYPES and novel_title and (not message or novel_title not in message):
        row["display_message"] = f"{message} - {novel_title}" if message else novel_title
    else:
        row["display_message"] = message

    target_url = _build_target_url(row)
    row["target_url"] = target_url
    row["novel_url"] = target_url
    row["can_open"] = bool(target_url)

    if "created_at" in row:
        row["created_at"] = None

    return row


def _group_notifications(rows):
    grouped = {}
    for r in rows:
        key = (
            (r.get("type") or "").strip().lower(),
            r.get("novel_id"),
            (r.get("message") or "").strip(),
            r.get("chapter_id"),
        )
        g = grouped.setdefault(
            key,
            {
                "base": r,
                "ids": [],
                "actor_ids": set(),
                "unread": 0,
                "count": 0,
                "latest_created": r.get("created_at"),
            },
        )
        g["ids"].append(r.get("notification_id"))
        if r.get("actor_user_id"):
            try:
                g["actor_ids"].add(int(r.get("actor_user_id")))
            except Exception:
                pass
        g["count"] += 1
        if not r.get("is_read"):
            g["unread"] += 1
        created_at = r.get("created_at")
        if created_at and (not g["latest_created"] or created_at > g["latest_created"]):
            g["latest_created"] = created_at

    result = []
    for g in grouped.values():
        base = dict(g["base"])
        base["notification_ids"] = [nid for nid in g["ids"] if nid is not None]
        base["actor_user_ids"] = list(g["actor_ids"])
        base["count"] = g["count"]
        base["unread_in_group"] = g["unread"]
        base["latest_created"] = g["latest_created"]

        if g["count"] > 1:
            unit = COUNT_LABELS.get(base.get("type"), "ครั้ง")
            base_message = (base.get("message") or "").strip()
            base["message"] = f"{base_message} {g['count']} {unit}".strip()

        base["is_read"] = 0 if g["unread"] else 1
        base["notification_id"] = base["notification_ids"][0] if base["notification_ids"] else base.get("notification_id")
        _hydrate_notification(base)
        result.append(base)

    result.sort(key=lambda x: x.get("latest_created") or datetime.min, reverse=True)
    return result


@noti_bp.route("/notifications")
@roles_required('user')
def notifications_page():
    user_id = _current_user_id()
    if not user_id:
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
        LEFT JOIN users au ON au.users_id = n.actor_user_id
        WHERE {where_sql}
    """

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS cnt {base_select}", params)
        total = cur.fetchone()["cnt"]
        total_pages = max(math.ceil(total / page_size), 1)
        offset = (page - 1) * page_size

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM notifications WHERE users_id=%s AND is_read=0",
            (user_id,)
        )
        unread_count = cur.fetchone()["cnt"]

        cur.execute(f"""
            SELECT
              n.notification_id, n.type, n.message, n.is_read, n.created_at,
              n.novel_id, n.chapter_id, n.comment_id, n.actor_user_id,
              nv.title AS novel_title, ch.chapter_no,
              au.username AS actor_username
            {base_select}
            ORDER BY n.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [page_size, offset])
        rows = cur.fetchall()

    for r in rows:
        r["type_label"] = TYPE_LABELS.get(r["type"], r["type"])
        r["type_icon"]  = TYPE_EMOJI.get(r["type"], "🔔")
    rows = _group_notifications(rows)

    context = dict(
        notifications=rows,
        page=page,
        total_pages=total_pages,
        tab=tab,
        q=q,
        sel_type=typ,
        type_labels=TYPE_LABELS,
        unread_count=unread_count,
        container_id=request.args.get("container_id", "notiContainer"),
    )
    return render_template("notification_panel.html", **context)


@noti_bp.get("/notifications/list")
@roles_required('user')
def notifications_list():
    uid = _current_user_id()
    if not uid:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    offset = request.args.get("offset", type=int) or 0
    limit = request.args.get("limit", type=int) or 10
    only_unread = (request.args.get("unread") or "").strip().lower() in ("1", "true", "yes", "y")
    offset = max(offset, 0)
    limit = max(limit, 1)
    limit = min(limit, 10)

    if offset >= MAX_PANEL_ITEMS:
        return jsonify({
            "ok": True,
            "notifications": [],
            "unread_count": 0,
            "next_offset": offset,
            "has_more": False,
            "max_items": MAX_PANEL_ITEMS
        })

    limit = min(limit, MAX_PANEL_ITEMS - offset)

    conn = get_db_connection()
    with conn.cursor() as cur:
        where_sql = "WHERE n.users_id = %s"
        params = [uid]
        if only_unread:
            where_sql += " AND n.is_read = 0"

        cur.execute(
            f"SELECT COUNT(*) AS cnt FROM notifications n {where_sql}",
            params
        )
        total = cur.fetchone()["cnt"]

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM notifications WHERE users_id=%s AND is_read=0",
            (uid,)
        )
        unread_count = cur.fetchone()["cnt"]

        cur.execute(
            f"""
            SELECT
              n.notification_id, n.type, n.message, n.is_read, n.created_at,
              n.novel_id, n.chapter_id, n.comment_id, n.actor_user_id,
              nv.title AS novel_title, ch.chapter_no,
              au.username AS actor_username
            FROM notifications n
            LEFT JOIN novels nv ON nv.novels_id = n.novel_id
            LEFT JOIN chapters ch ON ch.chapters_id = n.chapter_id
            LEFT JOIN users au ON au.users_id = n.actor_user_id
            {where_sql}
            ORDER BY n.created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows_raw = cur.fetchall()

    for r in rows_raw:
        r["type_label"] = TYPE_LABELS.get(r["type"], r["type"])
        r["type_icon"]  = TYPE_EMOJI.get(r["type"], "🔔")

    rows = _group_notifications(rows_raw)

    total_recent = min(int(total or 0), MAX_PANEL_ITEMS)
    has_more = (offset + len(rows_raw)) < total_recent

    return jsonify({
        "ok": True,
        "notifications": rows,
        "unread_count": unread_count,
        "next_offset": offset + len(rows_raw),
        "has_more": has_more,
        "max_items": MAX_PANEL_ITEMS
    })


@noti_bp.get("/notifications/unread-count")
@roles_required('user')
def unread_count():
    uid = _current_user_id()
    if not uid:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM notifications WHERE users_id=%s AND is_read=0",
            (uid,),
        )
        count = cur.fetchone()["cnt"]

    return jsonify({"ok": True, "unread_count": count})


# ========= Actions =========

@noti_bp.post("/notifications/<int:nid>/read")
@roles_required('user')
def mark_read(nid):
    read_val = 1 if request.form.get("read", "1") == "1" else 0
    uid = _current_user_id()
    if not uid:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
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
    uid = _current_user_id()
    if not uid:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE notifications SET is_read=1 WHERE users_id=%s AND is_read=0", (uid,))
    return jsonify({"ok": True})


@noti_bp.post("/notifications/<int:nid>/delete")
@roles_required('user')
def delete_notification(nid):
    uid = _current_user_id()
    if not uid:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM notifications WHERE notification_id=%s AND users_id=%s", (nid, uid))
        if cur.rowcount == 0:
            return jsonify({"ok": False, "error": "not_found_or_no_permission"}), 404
    return jsonify({"ok": True, "deleted": nid})
