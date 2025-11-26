# search.py
from flask import Blueprint, request, render_template
from db import mysql
import MySQLdb.cursors

search_bp = Blueprint('search', __name__)

SORT_OPTIONS = {
    'relevance': 'จัดตามความเกี่ยวข้อง/ความนิยม',
    'new': 'นิยายมาใหม่',
    'rating': 'เรตติ้งสูงสุด',
    'bookshelf': 'ถูกเพิ่มเข้าชั้นหนังสือมากสุด',
    'active': 'ผู้อ่านแอคทีฟมากสุด (เดือนนี้)',
    'chapters': 'จำนวนตอนมากสุด',
}

@search_bp.route('/search')
def search_novels():
    q = request.args.get('q', '').strip()
    scope = request.args.get('scope', 'all')        # all/title/author/desc/tag
    sort = request.args.get('sort', 'relevance')
    cate_id = request.args.get('cate_id', type=int) # หมวดที่เลือก (หรือ None)

    # mapping sort -> ORDER BY
    order_by_map = {
        'new': "n.created_at DESC",
        'rating': "bayesian_avg DESC, votes DESC, n.created_at DESC",
        'bookshelf': "bookshelf_users DESC, n.created_at DESC",
        'active': "active_readers DESC, n.created_at DESC",
        'chapters': "total_chapters DESC, n.created_at DESC",
        'relevance': "bayesian_avg DESC, active_readers DESC, n.created_at DESC",
    }
    order_by_sql = order_by_map.get(sort, order_by_map['relevance'])

    where_clauses = []
    params = []

    # แสดงเฉพาะนิยายเผยแพร่/จบแล้ว
    where_clauses.append("n.status IN ('เผยแพร่', 'จบแล้ว')")

    # filter ตามหมวดหมู่ถ้ามีเลือก
    if cate_id:
        where_clauses.append("n.cate_id = %s")
        params.append(cate_id)

    # เตรียม keyword
    keywords = [w.strip() for w in q.split() if w.strip()] if q else []

    for kw in keywords:
        like = f"%{kw}%"
        if scope == 'title':
            where_clauses.append("n.title LIKE %s")
            params.append(like)
        elif scope == 'author':
            where_clauses.append("u.username LIKE %s")
            params.append(like)
        elif scope == 'desc':
            where_clauses.append("n.description LIKE %s")
            params.append(like)
        elif scope == 'tag':
            where_clauses.append("t.name LIKE %s")
            params.append(like)
        else:  # all
            where_clauses.append("""
                (
                    n.title       LIKE %s
                    OR n.description LIKE %s
                    OR u.username LIKE %s
                    OR c.name     LIKE %s
                    OR t.name     LIKE %s
                )
            """)
            params.extend([like, like, like, like, like])

    where_sql = " AND ".join(where_clauses) if where_clauses else "1"

    sql = f"""
        SELECT
            n.novels_id,
            n.title,
            n.description,
            n.cover,
            n.status,
            n.created_at,
            n.updated_at,

            u.users_id              AS author_id,
            u.username              AS author_name,

            c.cate_id,
            c.name                  AS category_name,

            COALESCE(r.bayesian_avg, 0)    AS bayesian_avg,
            COALESCE(r.votes, 0)           AS votes,
            COALESCE(b.bookshelf_users, 0) AS bookshelf_users,
            COALESCE(ch.total_chapters, 0) AS total_chapters,
            COALESCE(m.active_readers, 0)  AS active_readers,

            GROUP_CONCAT(DISTINCT t.name ORDER BY t.name SEPARATOR ', ') AS tag_names

        FROM novels n
        JOIN users u
            ON u.users_id = n.users_id
        LEFT JOIN categories c
            ON c.cate_id = n.cate_id
        LEFT JOIN v_novel_rating_stats r
            ON r.novels_id = n.novels_id
        LEFT JOIN v_novel_bookshelf_counts b
            ON b.novels_id = n.novels_id
        LEFT JOIN v_novel_chapter_counts ch
            ON ch.novels_id = n.novels_id
        LEFT JOIN v_monthly_active_readers_by_novel m
            ON m.novels_id = n.novels_id
        LEFT JOIN novels_tags nt
            ON nt.novels_id = n.novels_id
        LEFT JOIN tags t
            ON t.tag_id = nt.tag_id

        WHERE {where_sql}

        GROUP BY
            n.novels_id,
            n.title,
            n.description,
            n.cover,
            n.status,
            n.created_at,
            n.updated_at,
            u.users_id,
            u.username,
            c.cate_id,
            c.name,
            r.bayesian_avg,
            r.votes,
            b.bookshelf_users,
            ch.total_chapters,
            m.active_readers

        ORDER BY {order_by_sql}
        LIMIT 50
    """

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(sql, params)
    results = cur.fetchall()
    cur.close()

    # ดึงหมวดหมู่ทั้งหมดสำหรับ dropdown "ทุกหมวด"
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT cate_id, name FROM categories ORDER BY name")
    categories = cur.fetchall()
    cur.close()

    return render_template(
        'search.html',
        q=q,
        results=results,
        scope=scope,
        sort=sort,
        sort_options=SORT_OPTIONS,
        cate_id=cate_id,
        categories=categories,
    )
