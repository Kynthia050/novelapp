from flask import Blueprint, request, render_template, url_for, g, abort
from db import get_db_connection
from contextlib import closing
import MySQLdb, MySQLdb.cursors  # ใช้ DictCursor
import os

home_bp = Blueprint('home', __name__, template_folder='../templates')

# ---------- helpers ----------
def _status_sql_clause(param: str | None) -> str:
    """แปลงค่าสถานะจากพารามิเตอร์ UI เป็น SQL เงื่อนไข"""
    if param == 'completed':
        return " AND n.status = 'จบแล้ว' "
    return ""  # ทั้งหมด (เผยแพร่ + จบแล้ว)


def _process_cover_url(cover_path: str | None) -> str:
    """ทำให้ path รูปปกกลายเป็น URL ใต้ static/cover/* และมี placeholder ถ้าไม่มี"""
    if cover_path:
        filename = os.path.basename(cover_path)
        return url_for('static', filename=f"cover/{filename}")
    return url_for('static', filename='cover/placeholder.jpg')


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
    รองรับโครงสร้างคอลัมน์ผู้เขียนหลายแบบของตาราง novels:
    - novels.users_id (ตามสคีมาปัจจุบัน)
    - novels.author_id
    - novels.created_by
    ถ้าไม่มีคอลัมน์ใด ๆ ข้างต้น จะคืน 'Unknown' แทน
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
    คืน N เรื่องที่ "อัปเดตล่าสุด" พร้อมแนบ avg_rating / rating_count (ถ้ามีตาราง ratings)
    + ค่า user_rating ของผู้ใช้ปัจจุบัน (ถ้ามี) + author_username
    """
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
                # คอลัมน์ sort: ใช้ updated_at ถ้ามี ไม่งั้น fallback created_at
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
                            n.novels_id, n.title, n.description, n.status, n.cover,
                            {order_col} AS updated_sort,
                            COALESCE(AVG(r_all.rating),0) AS avg_rating,
                            COUNT(r_all.rating)           AS rating_count,
                            r_u.rating                    AS user_rating,
                            {sel_author}
                        FROM novels n
                        {join_author}
                        LEFT JOIN ratings r_all ON r_all.novels_id = n.novels_id
                        LEFT JOIN ratings r_u   ON r_u.novels_id  = n.novels_id AND r_u.users_id = %s
                        WHERE n.status IN ('เผยแพร่','จบแล้ว')
                        GROUP BY n.novels_id, n.title, n.description, n.status, n.cover, updated_sort, r_u.rating, {gb_author}
                        ORDER BY updated_sort DESC, n.novels_id DESC
                        LIMIT %s
                    """
                    cur.execute(sql, (current_uid or 0, int(limit)))
                else:
                    sql = f"""
                        SELECT
                            n.novels_id, n.title, n.description, n.status, n.cover,
                            {order_col} AS updated_sort,
                            0 AS avg_rating, 0 AS rating_count, NULL AS user_rating,
                            {sel_author}
                        FROM novels n
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
    # ผู้ใช้ปัจจุบัน (ถ้ามี)
    current_uid = (g.user or {}).get('users_id') if hasattr(g, 'user') and g.user else None

    top10 = _get_latest_updated(current_uid, limit=10)

    # สร้าง cover URL ให้รูป
    for n in top10:
        n['cover'] = _process_cover_url(n.get('cover'))

    # enrich บาง field ให้ template ใช้ง่าย (สำรองไว้ แต่ home.html ก็คำนวณเองได้)
    for n in top10:
        n.setdefault('user_rating', None)
        n.setdefault('avg_rating', 0)

    return render_template(
        'home.html',
        top10=top10,
        categories=_get_categories(),
        month_label="อัปเดตล่าสุด",
        user=getattr(g, 'user', None),  # แก้จาก g.get('user')
    )
