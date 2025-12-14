# search.py
from flask import Blueprint, request, render_template
from db import mysql
import MySQLdb.cursors

search_bp = Blueprint('search', __name__)

SORT_OPTIONS = {
    'relevance': 'ความเกี่ยวข้อง/ความนิยม',
    'finished': 'จบแล้ว',
    'ongoing': 'ยังไม่จบ',
    'new': 'นิยายมาใหม่',
    'rating': 'คะแนนสูงสุด',
    'bookshelf': 'ถูกเพิ่มเข้าชั้นหนังสือมากสุด',
}

@search_bp.route('/search')
def search_novels():
    q = request.args.get('q', '').strip()
    scope = request.args.get('scope', 'all')          # all/title/author/desc/tag
    sort = request.args.get('sort', 'relevance')      # relevance/finished/ongoing/new/rating/bookshelf
    cate_id = request.args.get('cate_id', type=int)

    # dropdown หมวดหมู่ (ยังต้องใช้แม้ยังไม่ค้นหา)
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT cate_id, name FROM categories ORDER BY name")
    categories = cur.fetchall()
    cur.close()

    # ✅ ถ้ายังไม่พิมพ์ค้นหา: ไม่ต้อง query ผลลัพธ์ใด ๆ
    if not q:
        return render_template(
            'search.html',
            q='',
            results=[],
            scope=scope,
            sort=sort,
            sort_options=SORT_OPTIONS,
            cate_id=cate_id,
            categories=categories,
            page=1,
            per_page=20,
            total=0,
            total_pages=0,
            start_item=0,
            end_item=0,
        )

    # Pagination
    page = request.args.get('page', 1, type=int)
    if not page or page < 1:
        page = 1
    per_page = 20
    offset = (page - 1) * per_page

    # --- sort ที่เป็น "ตัวกรองสถานะ" ---
    status_filter = None
    order_sort = sort
    if sort == 'finished':
        status_filter = 'จบแล้ว'
        order_sort = 'relevance'
    elif sort == 'ongoing':
        status_filter = 'เผยแพร่'
        order_sort = 'relevance'

    # ORDER BY
    order_by_map = {
        'new': "n.created_at DESC",
        'rating': "avg_rating DESC, rating_count DESC, n.views DESC, n.created_at DESC",
        'bookshelf': "bookshelf_count DESC, n.views DESC, n.created_at DESC",
        'relevance': "n.views DESC, avg_rating DESC, bookshelf_count DESC, n.created_at DESC",
    }
    order_by_sql = order_by_map.get(order_sort, order_by_map['relevance'])

    where_clauses = []
    params = []

    # แสดงเฉพาะนิยายเผยแพร่/จบแล้ว
    where_clauses.append("n.status IN ('เผยแพร่', 'จบแล้ว')")

    # filter สถานะจาก dropdown (จบแล้ว / ยังไม่จบ)
    if status_filter:
        where_clauses.append("n.status = %s")
        params.append(status_filter)

    # filter หมวดหมู่
    if cate_id:
        where_clauses.append("n.cate_id = %s")
        params.append(cate_id)

    # keyword search
    keywords = [w.strip() for w in q.split() if w.strip()]
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
        else:
            where_clauses.append("""
                (
                    n.title          LIKE %s
                    OR n.description LIKE %s
                    OR u.username    LIKE %s
                    OR c.name        LIKE %s
                    OR t.name        LIKE %s
                )
            """)
            params.extend([like, like, like, like, like])

    where_sql = " AND ".join(where_clauses) if where_clauses else "1"

    # COUNT สำหรับ pagination
    count_sql = f"""
        SELECT COUNT(DISTINCT n.novels_id) AS total
        FROM novels n
        JOIN users u ON u.users_id = n.users_id
        LEFT JOIN categories c ON c.cate_id = n.cate_id
        LEFT JOIN novels_tags nt ON nt.novels_id = n.novels_id
        LEFT JOIN tags t ON t.tag_id = nt.tag_id
        WHERE {where_sql}
    """
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(count_sql, params)
    total = cur.fetchone()["total"] or 0
    cur.close()

    total_pages = (total + per_page - 1) // per_page if total else 0
    if total_pages and page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page

    # RESULTS
    sql = f"""
        SELECT
            n.novels_id,
            n.title,
            n.description,
            n.cover,
            n.status,
            CASE
              WHEN n.status = 'จบแล้ว' THEN 'จบแล้ว'
              WHEN n.status = 'เผยแพร่' THEN 'ยังไม่จบ'
              ELSE n.status
            END AS status_label,
            n.created_at,
            n.updated_at,
            n.views,

            u.users_id              AS author_id,
            u.username              AS author_name,

            c.cate_id,
            c.name                  AS category_name,

            COALESCE(r.avg_rating, 0)       AS avg_rating,
            COALESCE(r.rating_count, 0)     AS rating_count,
            COALESCE(b.bookshelf_count, 0)  AS bookshelf_count,
            COALESCE(ch.chapter_count, 0)   AS chapter_count,

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
            n.views,
            u.users_id,
            u.username,
            c.cate_id,
            c.name,
            r.avg_rating,
            r.rating_count,
            b.bookshelf_count,
            ch.chapter_count

        ORDER BY {order_by_sql}
        LIMIT %s OFFSET %s
    """
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(sql, params + [per_page, offset])
    results = cur.fetchall()
    cur.close()

    start_item = offset + 1 if total else 0
    end_item = min(offset + per_page, total) if total else 0

    return render_template(
        'search.html',
        q=q,
        results=results,
        scope=scope,
        sort=sort,
        sort_options=SORT_OPTIONS,
        cate_id=cate_id,
        categories=categories,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        start_item=start_item,
        end_item=end_item,
    )
