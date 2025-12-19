import math
import MySQLdb.cursors
from contextlib import closing
from flask import Blueprint, render_template, request, flash, redirect, url_for
from auth import roles_required 
from db import get_db_connection  # ดึงฟังก์ชันที่มีอยู่จริงใน db.py ของคุณ

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')

def _has_relation(cur, name: str) -> bool:
    try:
        cur.execute(f"DESCRIBE {name}")
        cur.fetchall()
        return True
    except Exception:
        return False


def _table_columns(cur, name: str) -> set[str]:
    try:
        cur.execute(f"DESCRIBE {name}")
        return {row["Field"] for row in cur.fetchall()}
    except Exception:
        return set()


def _build_top10_sql(cur, selected_category_id: int | None):
    joins = []
    select_fields = [
        "n.novels_id",
        "n.title",
        "n.cate_id",
        "c.name AS category_name",
    ]

    rating_cols = set()
    if _has_relation(cur, "v_novel_rating_stats"):
        rating_cols = _table_columns(cur, "v_novel_rating_stats")
        joins.append("LEFT JOIN v_novel_rating_stats r ON r.novels_id = n.novels_id")

        if "avg_rating" in rating_cols:
            select_fields.append("COALESCE(r.avg_rating, 0) AS avg_rating")
            select_fields.append("COALESCE(r.rating_count, 0) AS rating_count")
        else:
            avg_expr = "COALESCE(r.bayesian_avg, r.raw_avg, 0)"
            if "bayesian_avg" not in rating_cols and "raw_avg" in rating_cols:
                avg_expr = "COALESCE(r.raw_avg, 0)"
            if "bayesian_avg" not in rating_cols and "raw_avg" not in rating_cols:
                avg_expr = "0"
            count_expr = "COALESCE(r.votes, 0)" if "votes" in rating_cols else "0"
            select_fields.append(f"{avg_expr} AS avg_rating")
            select_fields.append(f"{count_expr} AS rating_count")

    if not rating_cols:
        if _has_relation(cur, "ratings"):
            rating_cols = _table_columns(cur, "ratings")
            blocked_filter = "WHERE is_blocked = 0" if "is_blocked" in rating_cols else ""
            joins.append(
                f"""
                LEFT JOIN (
                    SELECT novels_id,
                           AVG(rating) AS avg_rating,
                           COUNT(*)    AS rating_count
                    FROM ratings
                    {blocked_filter}
                    GROUP BY novels_id
                ) r ON r.novels_id = n.novels_id
                """
            )
            select_fields.append("COALESCE(r.avg_rating, 0) AS avg_rating")
            select_fields.append("COALESCE(r.rating_count, 0) AS rating_count")
        else:
            select_fields.append("0 AS avg_rating")
            select_fields.append("0 AS rating_count")

    chapter_cols = set()
    if _has_relation(cur, "v_novel_chapter_counts"):
        chapter_cols = _table_columns(cur, "v_novel_chapter_counts")
        joins.append("LEFT JOIN v_novel_chapter_counts ch ON ch.novels_id = n.novels_id")
        if "chapter_count" in chapter_cols:
            select_fields.append("COALESCE(ch.chapter_count, 0) AS chapter_count")
        elif "total_chapters" in chapter_cols:
            select_fields.append("COALESCE(ch.total_chapters, 0) AS chapter_count")
        else:
            select_fields.append("0 AS chapter_count")
    elif _has_relation(cur, "chapters"):
        joins.append(
            """
            LEFT JOIN (
                SELECT novels_id, COUNT(*) AS chapter_count
                FROM chapters
                GROUP BY novels_id
            ) ch ON ch.novels_id = n.novels_id
            """
        )
        select_fields.append("COALESCE(ch.chapter_count, 0) AS chapter_count")
    else:
        select_fields.append("0 AS chapter_count")

    bookshelf_cols = set()
    if _has_relation(cur, "v_novel_bookshelf_counts"):
        bookshelf_cols = _table_columns(cur, "v_novel_bookshelf_counts")
        joins.append("LEFT JOIN v_novel_bookshelf_counts b ON b.novels_id = n.novels_id")
        if "bookshelf_count" in bookshelf_cols:
            select_fields.append("COALESCE(b.bookshelf_count, 0) AS fav_count")
        elif "bookshelf_users" in bookshelf_cols:
            select_fields.append("COALESCE(b.bookshelf_users, 0) AS fav_count")
        else:
            select_fields.append("0 AS fav_count")
    elif _has_relation(cur, "bookshelf"):
        joins.append(
            """
            LEFT JOIN (
                SELECT novels_id, COUNT(*) AS fav_count
                FROM bookshelf
                GROUP BY novels_id
            ) b ON b.novels_id = n.novels_id
            """
        )
        select_fields.append("COALESCE(b.fav_count, 0) AS fav_count")
    else:
        select_fields.append("0 AS fav_count")

    where_sql = ""
    params = []
    if selected_category_id:
        where_sql = "WHERE n.cate_id = %s"
        params.append(selected_category_id)

    sql = f"""
        SELECT
            {", ".join(select_fields)}
        FROM novels n
        LEFT JOIN categories c ON c.cate_id = n.cate_id
        {' '.join(joins)}
        {where_sql}
        ORDER BY avg_rating DESC,
                 rating_count DESC,
                 fav_count DESC,
                 n.views DESC,
                 n.novels_id DESC
        LIMIT 10
    """
    return sql, params

@dashboard_bp.route('/')
@roles_required('admin', 'superadmin')
def dashboard_index():
    # ใช้ closing เพื่อให้แน่ใจว่า connection จะถูกปิดเมื่อจบการทำงาน
    with closing(get_db_connection()) as conn:
        # ใช้ DictCursor เพื่อให้ผลลัพธ์เป็น Dictionary (เรียกใช้ด้วยชื่อ column ได้)
        with conn.cursor(MySQLdb.cursors.DictCursor) as cursor:
            
            # ============ 1. รับค่า Parameter ============
            page = request.args.get('page', 1, type=int)
            if page < 1:
                page = 1
            search_query = request.args.get('search', '').strip()
            selected_category_id = request.args.get('category_id', type=int)

            per_page = 10
            offset = (page - 1) * per_page

            # ============ 2. ดึง Categories ============
            cursor.execute("SELECT * FROM categories ORDER BY name ASC")
            categories = cursor.fetchall()

            selected_category_name = None
            if selected_category_id:
                for cat in categories:
                    if cat['cate_id'] == selected_category_id: 
                        selected_category_name = cat['name']
                        break

            # ============ 3. Stats ============
            cursor.execute("SELECT COUNT(*) as count FROM novels")
            res_novels = cursor.fetchone()
            total_novels = res_novels['count'] if res_novels else 0

            cursor.execute("SELECT COUNT(*) as count FROM users") 
            res_users = cursor.fetchone()
            total_users_all = res_users['count'] if res_users else 0

            total_novels_category = None
            if selected_category_id:
                cursor.execute("SELECT COUNT(*) as count FROM novels WHERE cate_id = %s", (selected_category_id,))
                res_cat = cursor.fetchone()
                total_novels_category = res_cat['count'] if res_cat else 0

            # ============ 4. Top 10 Novels ============
            top10_sql, top10_params = _build_top10_sql(cursor, selected_category_id)
            cursor.execute(top10_sql, tuple(top10_params))
            top10_novels = cursor.fetchall()

            # ============ 5. User Management ============
            user_sql_where = ""
            user_params = []

            if search_query:
                user_sql_where = " WHERE username LIKE %s OR email LIKE %s"
                term = f"%{search_query}%"
                user_params = [term, term]

            cursor.execute(f"SELECT COUNT(*) as count FROM users {user_sql_where}", tuple(user_params))
            res_filtered = cursor.fetchone()
            total_users_filtered = res_filtered['count'] if res_filtered else 0
            total_pages = max(math.ceil(total_users_filtered / per_page), 1)
            if page > total_pages:
                page = total_pages
                offset = (page - 1) * per_page

            sql_users = f"SELECT * FROM users {user_sql_where} ORDER BY created_at DESC LIMIT %s OFFSET %s"
            user_params.extend([per_page, offset])
            
            cursor.execute(sql_users, tuple(user_params))
            users = cursor.fetchall()

    return render_template(
        'dashboard.html',
        page=page,
        total_pages=total_pages,
        search=search_query,
        categories=categories,
        selected_category_id=selected_category_id,
        selected_category_name=selected_category_name,
        top10_novels=top10_novels,
        users=users,
        total_novels=total_novels,
        total_users_all=total_users_all,
        total_novels_category=total_novels_category
    )

@dashboard_bp.route('/category/add', methods=['POST'])
@roles_required('admin', 'superadmin')
def add_category():
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('กรุณาระบุชื่อหมวดหมู่', 'error')
        return redirect(url_for('dashboard.dashboard_index'))

    # เชื่อมต่อ Database สำหรับการเพิ่มข้อมูล
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor(MySQLdb.cursors.DictCursor) as cursor:
                cursor.execute("SELECT cate_id FROM categories WHERE name = %s", (name,))
                if cursor.fetchone():
                    flash(f"หมวดหมู่ '{name}' มีอยู่ในระบบแล้ว", 'error')
                else:
                    cursor.execute("INSERT INTO categories (name) VALUES (%s)", (name,))
                    # conn.commit() ไม่ต้องใส่เพราะใน db.py ตั้งค่า autocommit=True ไว้แล้ว
                    flash(f"เพิ่มหมวดหมู่ '{name}' เรียบร้อยแล้ว", 'success')
    except Exception as e:
        flash(f"เกิดข้อผิดพลาด: {str(e)}", 'error')

    return redirect(url_for('dashboard.dashboard_index'))
