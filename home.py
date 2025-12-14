from flask import Blueprint, render_template, g
from db import get_db_connection
from contextlib import closing
import MySQLdb, MySQLdb.cursors
import os

home_bp = Blueprint('home', __name__, template_folder='../templates')

# ---------- helpers ----------
def _has_table(cur, name: str) -> bool:
    try:
        cur.execute(f"DESCRIBE {name}")
        cur.fetchall()
        return True
    except Exception:
        return False


def _author_sql_parts(cur):
    """
    คืน (select_expr, join_clause, groupby_expr) สำหรับดึง username ผู้เขียน
    รองรับหลายแบบ:
    - novels.users_id
    - novels.author_id
    - novels.created_by
    """
    try:
        cur.execute("DESCRIBE novels")
        cols = {r["Field"] for r in cur.fetchall()}
    except Exception:
        cols = set()

    if "users_id" in cols:
        return (
            "u.username AS author_username",
            "LEFT JOIN users u ON u.users_id = n.users_id",
            "u.username",
        )
    if "author_id" in cols:
        return (
            "u.username AS author_username",
            "LEFT JOIN users u ON u.users_id = n.author_id",
            "u.username",
        )
    if "created_by" in cols:
        return (
            "u.username AS author_username",
            "LEFT JOIN users u ON u.users_id = n.created_by",
            "u.username",
        )
    return ("'Unknown' AS author_username", "", "'Unknown'")


def _get_categories():
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
                cur.execute("SELECT cate_id, name FROM categories ORDER BY name")
                return cur.fetchall()
    except Exception as e:
        print(f"Categories error: {e}")
        return []


def _get_latest_updated(current_uid: int | None, limit: int = 10):
    """
    คืน N เรื่องที่ "อัปเดตล่าสุด"
    - avg_rating / rating_count (ถ้ามีตาราง ratings)
    - user_rating ของผู้ใช้ปัจจุบัน (ถ้ามี)
    - author_username
    - category_name
    """
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
                # ใช้ updated_at ถ้ามี ไม่งั้น fallback created_at
                try:
                    cur.execute("DESCRIBE novels")
                    cols = {row['Field'] for row in cur.fetchall()}
                except Exception:
                    cols = set()
                order_col = "n.updated_at" if "updated_at" in cols else "n.created_at"

                has_rt = _has_table(cur, "ratings")
                sel_author, join_author, gb_author = _author_sql_parts(cur)

                if has_rt:
                    sql = f"""
                        SELECT
                            n.novels_id,
                            n.title,
                            n.description,
                            n.status,
                            n.cover,
                            c.name AS category_name,
                            {order_col} AS updated_sort,

                            COALESCE(AVG(r_all.rating),0) AS avg_rating,
                            COUNT(r_all.rating)           AS rating_count,
                            r_u.rating                    AS user_rating,

                            {sel_author}

                        FROM novels n
                        LEFT JOIN categories c ON c.cate_id = n.cate_id
                        {join_author}
                        LEFT JOIN ratings r_all ON r_all.novels_id = n.novels_id
                        LEFT JOIN ratings r_u   ON r_u.novels_id  = n.novels_id AND r_u.users_id = %s
                        WHERE n.status IN ('เผยแพร่','จบแล้ว')

                        GROUP BY
                            n.novels_id, n.title, n.description, n.status, n.cover,
                            c.name,
                            updated_sort,
                            r_u.rating,
                            {gb_author}

                        ORDER BY updated_sort DESC, n.novels_id DESC
                        LIMIT %s
                    """
                    cur.execute(sql, (current_uid or 0, int(limit)))
                else:
                    sql = f"""
                        SELECT
                            n.novels_id,
                            n.title,
                            n.description,
                            n.status,
                            n.cover,
                            c.name AS category_name,
                            {order_col} AS updated_sort,

                            0 AS avg_rating,
                            0 AS rating_count,
                            NULL AS user_rating,

                            {sel_author}

                        FROM novels n
                        LEFT JOIN categories c ON c.cate_id = n.cate_id
                        {join_author}
                        WHERE n.status IN ('เผยแพร่','จบแล้ว')
                        ORDER BY updated_sort DESC, n.novels_id DESC
                        LIMIT %s
                    """
                    cur.execute(sql, (int(limit),))

                return cur.fetchall()
    except Exception as e:
        print(f"Latest-updated error: {e}")
        return []


# ---------- routes ----------
@home_bp.route('/')
def index():
    current_uid = (g.user or {}).get('users_id') if hasattr(g, 'user') and g.user else None
    top10 = _get_latest_updated(current_uid, limit=10)

    # ✅ ส่ง cover เป็น "ชื่อไฟล์" เพื่อให้ template ใช้ fallback แบบเดียวกับ Search
    for n in top10:
        raw = n.get('cover')
        n['cover'] = os.path.basename(raw) if raw else 'default.jpg'

        # ✅ สถานะเหมือน Search
        if n.get('status') == 'จบแล้ว':
            n['status_label'] = 'จบแล้ว'
        else:
            # ถ้าเป็น 'เผยแพร่' => ยังไม่จบ
            n['status_label'] = 'ยังไม่จบ'

        n.setdefault('avg_rating', 0)
        n.setdefault('rating_count', 0)
        n.setdefault('user_rating', None)

    return render_template(
        'home.html',
        top10=top10,
        categories=_get_categories(),
        month_label="อัปเดตล่าสุด",
        user=getattr(g, 'user', None),
    )
