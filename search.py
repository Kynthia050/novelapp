# search.py
from flask import Blueprint, request, render_template
from db import mysql
import MySQLdb.cursors

search_bp = Blueprint('search', __name__)

@search_bp.route('/search')
def search_novels():   # ← เปลี่ยนจาก def search() เป็น search_novels
    q = request.args.get('q', '').strip()
    results = []

    if q:
        keywords = [w.strip() for w in q.split() if w.strip()]

        if keywords:
            where_clauses = []
            params = []

            for kw in keywords:
                like = f"%{kw}%"
                where_clauses.append("(n.title LIKE %s OR u.username LIKE %s)")
                params.extend([like, like])

            where_sql = " AND ".join(where_clauses)

            sql = f"""
                SELECT
                    n.novels_id,
                    n.title,
                    n.description,
                    n.cover,
                    u.users_id   AS author_id,
                    u.username   AS author_name
                FROM novels n
                JOIN users u ON u.users_id = n.users_id
                WHERE {where_sql}
                ORDER BY n.created_at DESC
            """

            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute(sql, params)
            results = cur.fetchall()
            cur.close()

    return render_template('search.html', q=q, results=results)
