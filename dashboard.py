from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from typing import Tuple

import MySQLdb
import MySQLdb.cursors
from flask import Blueprint, jsonify, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from auth import roles_required
from db import get_db_connection
from moderation_utils import (
    clear_chapter_closed,
    ensure_chapter_moderation_table,
    fetch_chapter_moderation_map,
    fetch_pending_chapter_counts,
    mark_chapter_closed,
)


dashboard_bp = Blueprint("dashboard", __name__, template_folder="templates")


# ---------------- helpers ----------------
def _month_range(month_str: str | None = None, now: datetime | None = None) -> Tuple[datetime, datetime]:
    if month_str:
        match = re.match(r"^(\d{4})-(\d{2})$", month_str.strip())
        if match:
            year = _safe_int(match.group(1), 0)
            month = _safe_int(match.group(2), 0)
            if 1 <= month <= 12 and year > 0:
                start = datetime(year, month, 1)
                if month == 12:
                    end = datetime(year + 1, 1, 1)
                else:
                    end = datetime(year, month + 1, 1)
                return start, end
    now = now or datetime.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _get_active_enum_values(conn) -> Tuple[str, str]:
    """
    Safely detect enum values for users.is_active to avoid invalid INSERT/UPDATE.
    Fallback to ('active', 'inactive').
    """
    active_val = "active"
    inactive_val = "inactive"
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute("SHOW COLUMNS FROM users LIKE 'is_active'")
            row = cur.fetchone() or {}
            t = row.get("Type", "") or ""
            matches = re.findall(r"'([^']*)'", str(t))
            if matches:
                active_val = matches[0]
                if len(matches) > 1:
                    inactive_val = matches[1]
    except Exception:
        pass
    return active_val, inactive_val


def _is_active_value(val, active_marker: str) -> bool:
    if val is None:
        return False
    s = str(val).strip().lower()
    markers = {str(active_marker or "").strip().lower(), "1", "true", "active", "yes", "y"}
    return s in markers


def _fmt_dt(val) -> str:
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d %H:%M")
    return str(val) if val is not None else ""


def _safe_int(val, default=0) -> int:
    try:
        return int(val)
    except Exception:
        return default


# ---------------- core page ----------------
@dashboard_bp.route("/", methods=["GET"])
@roles_required("admin", "superadmin")
def dashboard_index():
    user_q = (request.args.get("user_q") or "").strip()
    novel_q = (request.args.get("novel_q") or "").strip()
    selected_cate_id = request.args.get("cate_id", type=int)
    is_superadmin = session.get("role") == "superadmin"
    current_uid = _safe_int(session.get("user_id") or session.get("uid"), -1)
    users_page = max(1, _safe_int(request.args.get("user_page", 1), 1))
    novels_page = max(1, _safe_int(request.args.get("novel_page", 1), 1))
    users_per_page = 15
    novels_per_page = 15

    month_str = (request.args.get("month") or "").strip()
    month_start, month_end = _month_range(month_str=month_str)
    month_label = month_start.strftime("%Y-%m")
    days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]

    total_novels = 0
    total_users = 0
    active_users_this_month = 0
    categories = []
    selected_category_count = 0
    users = []
    novels = []
    active_daily = []
    novel_daily = []
    users_total = 0
    users_total_pages = 0
    novels_total = 0
    novels_total_pages = 0
    users_start = 0
    users_end = 0
    novels_start = 0
    novels_end = 0

    conn = get_db_connection()
    try:
        active_marker, inactive_marker = _get_active_enum_values(conn)
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            # --- headline metrics ---
            try:
                cur.execute("SELECT COUNT(*) AS cnt FROM novels")
                total_novels = _safe_int((cur.fetchone() or {}).get("cnt"), 0)
            except Exception:
                total_novels = 0

            try:
                cur.execute("SELECT COUNT(*) AS cnt FROM users")
                total_users = _safe_int((cur.fetchone() or {}).get("cnt"), 0)
            except Exception:
                total_users = 0

            try:
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT users_id) AS cnt
                    FROM reading_history
                    WHERE last_read_at >= %s AND last_read_at < %s
                    """,
                    (month_start, month_end),
                )
                active_users_this_month = _safe_int((cur.fetchone() or {}).get("cnt"), 0)
            except Exception:
                active_users_this_month = 0

            try:
                cur.execute(
                    """
                    SELECT c.cate_id, c.name, COUNT(n.novels_id) AS novel_count
                    FROM categories c
                    LEFT JOIN novels n ON n.cate_id = c.cate_id
                    GROUP BY c.cate_id, c.name
                    ORDER BY c.name
                    """
                )
                categories = cur.fetchall() or []
            except Exception:
                categories = []

            if categories and selected_cate_id is None:
                selected_cate_id = categories[0]["cate_id"]

            for cat in categories:
                if selected_cate_id is not None and int(cat.get("cate_id")) == int(selected_cate_id):
                    selected_category_count = _safe_int(cat.get("novel_count"), 0)
                    break

            # --- users ---
            user_params = []
            user_sql = """
                SELECT users_id, username, email, role, is_active, last_login_at, created_at
                FROM users
            """
            count_sql = "SELECT COUNT(*) AS cnt FROM users"
            where_sql = ""
            if user_q:
                where_sql = " WHERE username LIKE %s OR email LIKE %s"
                pattern = f"%{user_q}%"
                user_params.extend([pattern, pattern])
            user_sql += where_sql
            count_sql += where_sql

            try:
                cur.execute(count_sql, user_params)
                users_total = _safe_int((cur.fetchone() or {}).get("cnt"), 0)
            except Exception:
                users_total = 0

            users_total_pages = (users_total + users_per_page - 1) // users_per_page if users_total else 0
            if users_total_pages and users_page > users_total_pages:
                users_page = users_total_pages
            user_offset = (users_page - 1) * users_per_page
            if users_total:
                users_start = user_offset + 1
                users_end = min(users_page * users_per_page, users_total)

            user_sql += " ORDER BY FIELD(role,'superadmin','admin','user'), created_at DESC LIMIT %s OFFSET %s"
            user_params.extend([users_per_page, user_offset])

            try:
                cur.execute(user_sql, user_params)
                users = cur.fetchall() or []
            except Exception:
                users = []

            for u in users:
                u["is_active_bool"] = _is_active_value(u.get("is_active"), active_marker)
                u["created_at_display"] = _fmt_dt(u.get("created_at"))
                u["last_login_display"] = _fmt_dt(u.get("last_login_at"))

            # --- novels ---
            novel_params = []
            novel_where_sql = ""
            if novel_q:
                novel_where_sql = " WHERE n.title LIKE %s OR u.username LIKE %s"
                pattern = f"%{novel_q}%"
                novel_params.extend([pattern, pattern])

            try:
                count_sql = """
                    SELECT COUNT(*) AS cnt
                    FROM novels n
                    LEFT JOIN users u ON u.users_id = n.users_id
                """
                count_sql += novel_where_sql
                cur.execute(count_sql, novel_params)
                novels_total = _safe_int((cur.fetchone() or {}).get("cnt"), 0)
            except Exception:
                novels_total = 0

            novels_total_pages = (novels_total + novels_per_page - 1) // novels_per_page if novels_total else 0
            if novels_total_pages and novels_page > novels_total_pages:
                novels_page = novels_total_pages
            novels_offset = (novels_page - 1) * novels_per_page
            if novels_total:
                novels_start = novels_offset + 1
                novels_end = min(novels_page * novels_per_page, novels_total)

            try:
                novel_sql = """
                    SELECT
                        n.novels_id,
                        n.title,
                        n.status,
                        n.created_at,
                        c.name AS category_name,
                        u.username AS author_name,
                        COALESCE(cm.cm_count, 0) AS comment_count
                    FROM novels n
                    LEFT JOIN categories c ON c.cate_id = n.cate_id
                    LEFT JOIN users u ON u.users_id = n.users_id
                    LEFT JOIN (
                        SELECT novels_id, COUNT(*) AS cm_count
                        FROM comments
                        GROUP BY novels_id
                    ) cm ON cm.novels_id = n.novels_id
                """
                novel_sql += novel_where_sql
                novel_sql += " ORDER BY n.created_at DESC LIMIT %s OFFSET %s"
                novel_params_with_page = list(novel_params)
                novel_params_with_page.extend([novels_per_page, novels_offset])
                cur.execute(novel_sql, novel_params_with_page)
                novels = cur.fetchall() or []
            except Exception:
                novels = []

            for n in novels:
                n["created_at_display"] = _fmt_dt(n.get("created_at"))
            try:
                pending_counts = fetch_pending_chapter_counts(
                    conn, [n.get("novels_id") for n in novels]
                )
            except Exception:
                pending_counts = {}
            for n in novels:
                n["pending_review_count"] = _safe_int(pending_counts.get(n.get("novels_id")), 0)

            # --- charts ---
            try:
                cur.execute(
                    """
                    SELECT DATE(last_read_at) AS day, COUNT(DISTINCT users_id) AS active_users
                    FROM reading_history
                    WHERE last_read_at >= %s AND last_read_at < %s
                    GROUP BY DATE(last_read_at)
                    ORDER BY day
                    """,
                    (month_start, month_end),
                )
                active_daily = [
                    {"day": (row["day"].strftime("%Y-%m-%d") if row.get("day") else ""), "count": _safe_int(row.get("active_users"), 0)}
                    for row in (cur.fetchall() or [])
                ]
            except Exception:
                active_daily = []

            try:
                cur.execute(
                    """
                    SELECT DATE(created_at) AS day, COUNT(*) AS novels_added
                    FROM novels
                    WHERE created_at >= %s AND created_at < %s
                    GROUP BY DATE(created_at)
                    ORDER BY day
                    """,
                    (month_start, month_end),
                )
                novel_daily = [
                    {"day": (row["day"].strftime("%Y-%m-%d") if row.get("day") else ""), "count": _safe_int(row.get("novels_added"), 0)}
                    for row in (cur.fetchall() or [])
                ]
            except Exception:
                novel_daily = []

    finally:
        conn.close()

    category_counts_map = {str(c["cate_id"]): _safe_int(c.get("novel_count"), 0) for c in categories}

    return render_template(
        "dashboard.html",
        total_novels=total_novels,
        total_users=total_users,
        active_users_this_month=active_users_this_month,
        categories=categories,
        selected_cate_id=selected_cate_id,
        selected_category_count=selected_category_count,
        category_counts_map=category_counts_map,
        users=users,
        novels=novels,
        is_superadmin=is_superadmin,
        month_label=month_label,
        month_meta={"year": month_start.year, "month": month_start.month, "days": days_in_month},
        active_daily=active_daily,
        novel_daily=novel_daily,
        user_q=user_q,
        novel_q=novel_q,
        current_uid=current_uid,
        users_page=users_page,
        users_total=users_total,
        users_total_pages=users_total_pages,
        users_per_page=users_per_page,
        users_start=users_start,
        users_end=users_end,
        novels_page=novels_page,
        novels_total=novels_total,
        novels_total_pages=novels_total_pages,
        novels_per_page=novels_per_page,
        novels_start=novels_start,
        novels_end=novels_end,
    )


@dashboard_bp.route("/charts-data", methods=["GET"])
@roles_required("admin", "superadmin")
def charts_data():
    month_str = (request.args.get("month") or "").strip()
    month_start, month_end = _month_range(month_str=month_str)
    days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
    active_daily = []
    novel_daily = []

    conn = get_db_connection()
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            try:
                cur.execute(
                    """
                    SELECT DATE(last_read_at) AS day, COUNT(DISTINCT users_id) AS active_users
                    FROM reading_history
                    WHERE last_read_at >= %s AND last_read_at < %s
                    GROUP BY DATE(last_read_at)
                    ORDER BY day
                    """,
                    (month_start, month_end),
                )
                active_daily = [
                    {"day": (row["day"].strftime("%Y-%m-%d") if row.get("day") else ""), "count": _safe_int(row.get("active_users"), 0)}
                    for row in (cur.fetchall() or [])
                ]
            except Exception:
                active_daily = []

            try:
                cur.execute(
                    """
                    SELECT DATE(created_at) AS day, COUNT(*) AS novels_added
                    FROM novels
                    WHERE created_at >= %s AND created_at < %s
                    GROUP BY DATE(created_at)
                    ORDER BY day
                    """,
                    (month_start, month_end),
                )
                novel_daily = [
                    {"day": (row["day"].strftime("%Y-%m-%d") if row.get("day") else ""), "count": _safe_int(row.get("novels_added"), 0)}
                    for row in (cur.fetchall() or [])
                ]
            except Exception:
                novel_daily = []
    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "month_label": month_start.strftime("%Y-%m"),
            "month_meta": {"year": month_start.year, "month": month_start.month, "days": days_in_month},
            "active_daily": active_daily,
            "novel_daily": novel_daily,
        }
    )


@dashboard_bp.route("/novels/<int:novel_id>/review", methods=["GET"])
@roles_required("admin", "superadmin")
def novel_review(novel_id: int):
    conn = get_db_connection()
    try:
        ensure_chapter_moderation_table(conn)
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT n.novels_id, n.title, u.username AS author_name
                  FROM novels n
                  LEFT JOIN users u ON u.users_id = n.users_id
                 WHERE n.novels_id = %s
                """,
                (novel_id,),
            )
            novel = cur.fetchone()
            if not novel:
                return jsonify({"ok": False, "error": "not_found"}), 404

            cur.execute(
                """
                SELECT chapters_id, chapter_no, title, status, content_html, created_at, updated_at
                  FROM chapters
                 WHERE novels_id = %s
                 ORDER BY chapter_no ASC
                """,
                (novel_id,),
            )
            chapters = cur.fetchall() or []

        moderation_map = fetch_chapter_moderation_map(conn, novel_id)
    finally:
        conn.close()

    payload = []
    for ch in chapters:
        ch_id = ch.get("chapters_id")
        reason = (moderation_map or {}).get(ch_id) or ""
        is_closed = reason == "closed_by_admin"
        is_pending = reason == "pending_review"
        payload.append(
            {
                "chapters_id": ch_id,
                "chapter_no": ch.get("chapter_no"),
                "title": ch.get("title") or "",
                "status": ch.get("status") or "",
                "content_html": ch.get("content_html") or "",
                "created_at": _fmt_dt(ch.get("created_at")),
                "updated_at": _fmt_dt(ch.get("updated_at")),
                "is_closed": is_closed,
                "is_pending": is_pending,
                "close_url": url_for("dashboard.close_chapter_admin", chapter_id=ch_id),
                "publish_url": url_for("dashboard.publish_chapter_admin", chapter_id=ch_id),
            }
        )

    return jsonify(
        {
            "ok": True,
            "novel": {
                "novels_id": novel.get("novels_id"),
                "title": novel.get("title") or "",
                "author_name": novel.get("author_name") or "",
            },
            "chapters": payload,
        }
    )


@dashboard_bp.route("/chapters/<int:chapter_id>/close", methods=["POST"])
@roles_required("admin", "superadmin")
def close_chapter_admin(chapter_id: int):
    acting_id = _safe_int(session.get("user_id") or session.get("users_id") or session.get("uid"), None)
    conn = get_db_connection()
    try:
        ensure_chapter_moderation_table(conn)
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute("SHOW COLUMNS FROM chapters LIKE 'updated_at'")
            has_updated = bool(cur.fetchone())

            cur.execute(
                "SELECT chapters_id, novels_id, status FROM chapters WHERE chapters_id = %s",
                (chapter_id,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not_found"}), 404

            if has_updated:
                cur.execute(
                    """
                    UPDATE chapters
                       SET status = 'draft',
                           updated_at = NOW()
                     WHERE chapters_id = %s
                    """,
                    (chapter_id,),
                )
            else:
                cur.execute(
                    """
                    UPDATE chapters
                       SET status = 'draft'
                     WHERE chapters_id = %s
                    """,
                    (chapter_id,),
                )

            mark_chapter_closed(conn, chapter_id, row.get("novels_id"), acting_id, reason="closed_by_admin")
    finally:
        conn.close()

    return jsonify({"ok": True, "chapter_id": chapter_id, "status": "draft"}), 200


@dashboard_bp.route("/chapters/<int:chapter_id>/publish", methods=["POST"])
@roles_required("admin", "superadmin")
def publish_chapter_admin(chapter_id: int):
    conn = get_db_connection()
    try:
        ensure_chapter_moderation_table(conn)
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute("SHOW COLUMNS FROM chapters LIKE 'updated_at'")
            has_updated = bool(cur.fetchone())

            cur.execute("SELECT chapters_id FROM chapters WHERE chapters_id = %s", (chapter_id,))
            if not cur.fetchone():
                return jsonify({"ok": False, "error": "not_found"}), 404

            if has_updated:
                cur.execute(
                    """
                    UPDATE chapters
                       SET status = 'published',
                           updated_at = NOW()
                     WHERE chapters_id = %s
                    """,
                    (chapter_id,),
                )
            else:
                cur.execute(
                    """
                    UPDATE chapters
                       SET status = 'published'
                     WHERE chapters_id = %s
                    """,
                    (chapter_id,),
                )
            clear_chapter_closed(conn, chapter_id)
    finally:
        conn.close()

    return jsonify({"ok": True, "chapter_id": chapter_id, "status": "published"}), 200


# ---------------- actions ----------------
@dashboard_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@roles_required("admin", "superadmin")
def toggle_user_active(user_id: int):
    acting_role = session.get("role")
    acting_id = session.get("user_id") or session.get("uid")
    acting_id_int = _safe_int(acting_id, -1)
    desired_raw = request.form.get("active")
    desired_bool = None
    if desired_raw is not None:
        desired_bool = str(desired_raw).strip().lower() in ("1", "true", "yes", "on")

    conn = get_db_connection()
    try:
        active_marker, inactive_marker = _get_active_enum_values(conn)
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute("SELECT users_id, role, is_active FROM users WHERE users_id=%s", (user_id,))
            target = cur.fetchone()
            if not target:
                return jsonify({"ok": False, "error": "User not found"}), 404

            target_uid = _safe_int(target.get("users_id"), -2)
            if target_uid == acting_id_int:
                return jsonify({"ok": False, "error": "You cannot change your own account"}), 403

            if acting_role == "admin":
                if target.get("role") == "superadmin":
                    return jsonify({"ok": False, "error": "You cannot change this user"}), 403

            current_active = _is_active_value(target.get("is_active"), active_marker)
            next_active = (not current_active) if desired_bool is None else desired_bool
            new_state = active_marker if next_active else inactive_marker

            cur.execute(
                "UPDATE users SET is_active=%s, updated_at=NOW() WHERE users_id=%s",
                (new_state, user_id),
            )

        return jsonify({"ok": True, "is_active": next_active})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@dashboard_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@roles_required("superadmin")
def reset_admin_password(user_id: int):
    new_pw = (request.form.get("password") or "").strip()
    if len(new_pw) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute("SELECT role FROM users WHERE users_id=%s", (user_id,))
            target = cur.fetchone()
            if not target:
                return jsonify({"ok": False, "error": "User not found"}), 404
            if target.get("role") != "admin":
                return jsonify({"ok": False, "error": "Only admin accounts can be reset here"}), 400

            pw_hash = generate_password_hash(new_pw, method="scrypt")
            cur.execute(
                """
                UPDATE users
                SET password_hash=%s,
                    updated_at=NOW()
                WHERE users_id=%s
                """,
                (pw_hash, user_id),
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@dashboard_bp.route("/categories/add", methods=["POST"])
@roles_required("admin", "superadmin")
def add_category():
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Category name is required"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute("SELECT 1 FROM categories WHERE LOWER(name)=LOWER(%s) LIMIT 1", (name,))
            exist = cur.fetchone()
            if exist:
                return jsonify({"ok": False, "error": "Category already exists"}), 400

            cur.execute("SELECT COALESCE(MAX(cate_id), 0) + 1 AS next_id FROM categories")
            next_id = _safe_int((cur.fetchone() or {}).get("next_id"), 1)

            cur.execute(
                "INSERT INTO categories (cate_id, name) VALUES (%s, %s)",
                (next_id, name),
            )
        return jsonify({"ok": True, "category": {"cate_id": next_id, "name": name}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@dashboard_bp.route("/categories/<int:cate_id>/rename", methods=["POST"])
@roles_required("admin", "superadmin")
def rename_category(cate_id: int):
    new_name = (request.form.get("name") or "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "Category name is required"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute("SELECT cate_id, name FROM categories WHERE cate_id=%s", (cate_id,))
            current = cur.fetchone()
            if not current:
                return jsonify({"ok": False, "error": "Category not found"}), 404

            cur.execute(
                "SELECT cate_id FROM categories WHERE LOWER(name)=LOWER(%s) AND cate_id != %s LIMIT 1",
                (new_name, cate_id),
            )
            duplicate = cur.fetchone()
            if duplicate:
                return jsonify({"ok": False, "error": "Category already exists"}), 400

            cur.execute("UPDATE categories SET name=%s WHERE cate_id=%s", (new_name, cate_id))

        return jsonify({"ok": True, "category": {"cate_id": cate_id, "name": new_name}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@dashboard_bp.route("/novels/<int:novel_id>/comments", methods=["GET"])
@roles_required("admin", "superadmin")
def novel_comments(novel_id: int):
    page = request.args.get("page", type=int, default=1)
    q = (request.args.get("q") or "").strip()
    per_page = 20
    conn = get_db_connection()
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            where_clause = "WHERE c.novels_id = %s"
            params: list = [novel_id]
            if q:
                where_clause += " AND c.content LIKE %s"
                params.append(f"%{q}%")

            try:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM comments c {where_clause}", params)
                total = _safe_int((cur.fetchone() or {}).get("cnt"), 0)
            except Exception:
                total = 0

            total_pages = (total + per_page - 1) // per_page if total else 0
            if total_pages and page > total_pages:
                page = total_pages
            offset = (page - 1) * per_page

            params_with_page = list(params)
            params_with_page.extend([per_page, offset])
            cur.execute(
                f"""
                SELECT
                    c.cm_id,
                    c.content,
                    c.created_at,
                    u.username AS author
                FROM comments c
                LEFT JOIN users u ON u.users_id = c.users_id
                {where_clause}
                ORDER BY c.created_at DESC
                LIMIT %s OFFSET %s
                """,
                params_with_page,
            )
            rows = cur.fetchall() or []
            comments = []
            for row in rows:
                delete_url = url_for("dashboard.delete_comment", comment_id=row.get("cm_id"))
                comments.append(
                    {
                        "cm_id": row.get("cm_id"),
                        "content": row.get("content", ""),
                        "author": row.get("author") or "-",
                        "created_at": _fmt_dt(row.get("created_at")),
                        "delete_url": delete_url,
                    }
                )
            start = offset + 1 if total else 0
            end = min(offset + per_page, total) if total else 0
            return jsonify({
                "ok": True,
                "comments": comments,
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "start": start,
                "end": end,
                "query": q,
            })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@dashboard_bp.route("/comments/<int:comment_id>/delete", methods=["POST"])
@roles_required("admin", "superadmin")
def delete_comment(comment_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute("DELETE FROM comments WHERE cm_id=%s", (comment_id,))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()
