import math
import MySQLdb.cursors
from contextlib import closing
from flask import Blueprint, render_template, request, flash, redirect, url_for
from auth import roles_required 
from db import get_db_connection  # ดึงฟังก์ชันที่มีอยู่จริงใน db.py ของคุณ

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')

@dashboard_bp.route('/')
@roles_required('admin', 'superadmin')
def dashboard_index():
    # ใช้ closing เพื่อให้แน่ใจว่า connection จะถูกปิดเมื่อจบการทำงาน
    with closing(get_db_connection()) as conn:
        # ใช้ DictCursor เพื่อให้ผลลัพธ์เป็น Dictionary (เรียกใช้ด้วยชื่อ column ได้)
        with conn.cursor(MySQLdb.cursors.DictCursor) as cursor:
            
            # ============ 1. รับค่า Parameter ============
            page = request.args.get('page', 1, type=int)
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
            base_top10_sql = """
                SELECT n.*, c.name as category_name 
                FROM novels n 
                LEFT JOIN categories c ON n.cate_id = c.cate_id
            """
            
            if selected_category_id:
                cursor.execute(base_top10_sql + " WHERE n.cate_id = %s ORDER BY n.avg_rating DESC LIMIT 10", (selected_category_id,))
            else:
                cursor.execute(base_top10_sql + " ORDER BY n.avg_rating DESC LIMIT 10")
            
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
            total_pages = math.ceil(total_users_filtered / per_page)

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
    name = request.form.get('name')
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