from flask import Blueprint, request, render_template, url_for, g
from db import get_db_connection
from contextlib import closing
import MySQLdb, MySQLdb.cursors
import os

home_bp = Blueprint('home', __name__, template_folder='../templates')

# ===== sort แบบเดียวกับ Search =====
SORT_OPTIONS = {
    'relevance': 'ความเกี่ยวข้อง/ความนิยม',
    'finished': 'จบแล้ว',
    'ongoing': 'ยังไม่จบ',
    'new': 'นิยายมาใหม่',
    'rating': 'คะแนนสูงสุด',
    'bookshelf': 'ถูกเพิ่มเข้าชั้นหนังสือมากสุด',
}

# ---------- helpers ----------
def _process_cover_url(cover_path: str | None) -> str:
    """ทำให้ path รูปปกกลายเป็น URL ใต้ static/cover/* และมี placeholder ถ้าไม่มี"""
    if cover_path:
        s = str(cover_path).strip()
        if s.startswith(("http://", "https://", "/")):
            return s
        if s.startswith("static/"):
            return "/" + s
        if s.startswith("cover/"):
            return url_for('static', filename=s)
        filename = os.path.basename(s)
        return url_for('static', filename=f"cover/{filename}")
    return url_for('static', filename='cover/placeholder.jpg')


def _has_relation(cur, name: str) -> bool:
    """เช็ค table/view ว่ามีจริงไหม (DESCRIBE ใช้ได้ทั้ง TABLE/VIEW)"""
    try:
        cur.execute(f"DESCRIBE `{name}`")
        cur.fetchall()
        return True
    except Exception:
        return False


def _author_sql_parts(cur):
    """รองรับโครงสร้างคอลัมน์ผู้เขียนหลายแบบของตาราง novels"""
    try:
        cur.execute("DESCRIBE novels")
        cols = {r["Field"] for r in cur.fetchall()}
    except Exception:
        cols = set()

    if "users_id" in cols:
        return ("u.users_id AS author_id, u.username AS author_name", "LEFT JOIN users u ON u.users_id = n.users_id")
    if "author_id" in cols:
        return ("u.users_id AS author_id, u.username AS author_name", "LEFT JOIN users u ON u.users_id = n.author_id")
    if "created_by" in cols:
        return ("u.users_id AS author_id, u.username AS author_name", "LEFT JOIN users u ON u.users_id = n.created_by")
    return ("NULL AS author_id, 'Unknown' AS author_name", "")


def _chapter_publish_cond(ccols: set[str], alias: str = "ch") -> str | None:
    if "status" in ccols:
        return f"{alias}.status IN ('เผยแพร่','published','PUBLISHED')"
    if "chapter_status" in ccols:
        return f"{alias}.chapter_status IN ('เผยแพร่','published','PUBLISHED')"
    if "is_published" in ccols:
        return f"{alias}.is_published = 1"
    if "published" in ccols:
        return f"{alias}.published = 1"
    if "is_draft" in ccols:
        return f"{alias}.is_draft = 0"
    return None


def _published_chapters_expr(cur, novels_alias: str = "n") -> str:
    """
    คืน SQL expression สำหรับนับ 'จำนวนตอนที่เผยแพร่' ต่อ 1 นิยาย
    - ถ้ามี view ที่มี published_count/published_chapters ก็ใช้ได้
    - ไม่งั้นนับจากตาราง chapters โดยเดาคอลัมน์สถานะให้
    """
    # 1) ถ้ามี view ที่มีคอลัมน์ published_* ให้ใช้ก่อน
    if _has_relation(cur, "v_novel_chapter_counts"):
        try:
            cur.execute("DESCRIBE `v_novel_chapter_counts`")
            vcols = {r["Field"] for r in cur.fetchall()}
        except Exception:
            vcols = set()

        for col in ("published_chapters", "published_count", "publish_count"):
            if col in vcols:
                return f"""(
                    SELECT COALESCE(vc.{col}, 0)
                    FROM v_novel_chapter_counts vc
                    WHERE vc.novels_id = {novels_alias}.novels_id
                ) AS published_chapters"""

    # 2) fallback: นับจาก table chapters
    if not _has_relation(cur, "chapters"):
        return "0 AS published_chapters"

    try:
        cur.execute("DESCRIBE chapters")
        ccols = {r["Field"] for r in cur.fetchall()}
    except Exception:
        ccols = set()

    # fk ไป novels
    fk = "novels_id" if "novels_id" in ccols else ("novel_id" if "novel_id" in ccols else "novels_id")

    # หาคอลัมน์สถานะ
    if "status" in ccols:
        # รองรับทั้งไทย/อังกฤษแบบเผื่อ ๆ
        cond = "ch.status IN ('เผยแพร่','published','PUBLISHED')"
    elif "chapter_status" in ccols:
        cond = "ch.chapter_status IN ('เผยแพร่','published','PUBLISHED')"
    elif "is_published" in ccols:
        cond = "ch.is_published = 1"
    elif "published" in ccols:
        cond = "ch.published = 1"
    elif "is_draft" in ccols:
        cond = "ch.is_draft = 0"
    else:
        # ถ้าเดาไม่ได้จริง ๆ: นับทุกตอน (อย่างน้อยไม่เป็น 0)
        cond = None

    if cond:
        return f"(SELECT COUNT(*) FROM chapters ch WHERE ch.{fk} = {novels_alias}.novels_id AND {cond}) AS published_chapters"
    return f"(SELECT COUNT(*) FROM chapters ch WHERE ch.{fk} = {novels_alias}.novels_id) AS published_chapters"


def _get_categories():
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
                cur.execute("SELECT cate_id, name FROM categories ORDER BY name")
                return cur.fetchall()
    except Exception as e:
        print(f"Categories error: {e}")
        return []


def _safe_int(v, default=1, minv=1):
    try:
        x = int(v)
        return x if x >= minv else default
    except Exception:
        return default


def _get_latest_updated(current_uid: int | None, limit: int = 10):
    """
    ส่วน 'อัปเดตล่าสุด'
    อัปเดต: เพิ่ม published_chapters (นับเฉพาะตอนเผยแพร่)
    """
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
                # เลือกคอลัมน์ sort: updated_at ถ้ามี ไม่งั้น fallback created_at
                try:
                    cur.execute("DESCRIBE novels")
                    cols = {row['Field'] for row in cur.fetchall()}
                except Exception:
                    cols = set()
                has_n_updated = "updated_at" in cols
                has_n_created = "created_at" in cols

                if has_n_updated and has_n_created:
                    novel_time_expr = "COALESCE(n.updated_at, n.created_at)"
                elif has_n_updated:
                    novel_time_expr = "n.updated_at"
                elif has_n_created:
                    novel_time_expr = "n.created_at"
                else:
                    novel_time_expr = "n.novels_id"

                order_col = novel_time_expr
                # Include chapter publish/new timestamps as update signals.
                if _has_relation(cur, "chapters") and (has_n_updated or has_n_created):
                    try:
                        cur.execute("DESCRIBE chapters")
                        ccols = {row["Field"] for row in cur.fetchall()}
                    except Exception:
                        ccols = set()

                    has_ch_created = "created_at" in ccols
                    has_ch_updated = "updated_at" in ccols
                    if has_ch_created or has_ch_updated:
                        fk = "novels_id" if "novels_id" in ccols else ("novel_id" if "novel_id" in ccols else "novels_id")

                        if has_ch_created and has_ch_updated:
                            new_ch_ts = "COALESCE(ch.created_at, ch.updated_at)"
                            pub_ch_ts = "COALESCE(ch.updated_at, ch.created_at)"
                        elif has_ch_created:
                            new_ch_ts = "ch.created_at"
                            pub_ch_ts = "ch.created_at"
                        else:
                            new_ch_ts = "ch.updated_at"
                            pub_ch_ts = "ch.updated_at"

                        order_parts = [
                            novel_time_expr,
                            f"(SELECT MAX({new_ch_ts}) FROM chapters ch WHERE ch.{fk} = n.novels_id)",
                        ]

                        pub_cond = _chapter_publish_cond(ccols, alias="ch")
                        if pub_cond:
                            order_parts.append(
                                f"(SELECT MAX({pub_ch_ts}) FROM chapters ch WHERE ch.{fk} = n.novels_id AND {pub_cond})"
                            )

                        order_col = "GREATEST(" + ", ".join(
                            [f"COALESCE({p}, '1970-01-01 00:00:00')" for p in order_parts]
                        ) + ")"

                sel_author, join_author = _author_sql_parts(cur)

                has_views = "views" in cols
                has_rt_view = _has_relation(cur, "v_novel_rating_stats")
                has_rt_table = _has_relation(cur, "ratings")
                has_cate = _has_relation(cur, "categories")

                sel_views = "n.views AS views" if has_views else "0 AS views"
                sel_cate = "c.name AS category_name" if has_cate else "NULL AS category_name"

                if has_rt_view:
                    sel_avg = "(SELECT COALESCE(v.avg_rating, 0) FROM v_novel_rating_stats v WHERE v.novels_id = n.novels_id) AS avg_rating"
                    sel_rc = "(SELECT COALESCE(v.rating_count, 0) FROM v_novel_rating_stats v WHERE v.novels_id = n.novels_id) AS rating_count"
                elif has_rt_table:
                    sel_avg = "(SELECT COALESCE(AVG(r.rating), 0) FROM ratings r WHERE r.novels_id = n.novels_id) AS avg_rating"
                    sel_rc = "(SELECT COUNT(*) FROM ratings r WHERE r.novels_id = n.novels_id) AS rating_count"
                else:
                    sel_avg = "0 AS avg_rating"
                    sel_rc = "0 AS rating_count"

                # ✅ นับจำนวนตอน "เผยแพร่" ต่อเรื่อง
                sel_pub_ch = _published_chapters_expr(cur, "n")

                # (ยังคง chapter_count เดิมไว้เผื่อหน้าอื่นใช้อยู่)
                if _has_relation(cur, "v_novel_chapter_counts"):
                    sel_ch = "(SELECT COALESCE(ch.chapter_count, 0) FROM v_novel_chapter_counts ch WHERE ch.novels_id = n.novels_id) AS chapter_count"
                elif _has_relation(cur, "chapters"):
                    # นับทุกตอน
                    sel_ch = "(SELECT COUNT(*) FROM chapters ch WHERE ch.novels_id = n.novels_id) AS chapter_count"
                else:
                    sel_ch = "0 AS chapter_count"

                sql = f"""
                    SELECT
                        n.novels_id, n.title, n.description, n.status, n.cover,
                        {order_col} AS updated_sort,
                        {sel_views},
                        {sel_avg},
                        {sel_rc},
                        {sel_ch},
                        {sel_pub_ch},
                        {sel_cate},
                        {sel_author}
                    FROM novels n
                    {join_author}
                    {"LEFT JOIN categories c ON c.cate_id = n.cate_id" if has_cate else ""}
                    WHERE n.status IN ('เผยแพร่','จบแล้ว')
                    ORDER BY updated_sort DESC, n.novels_id DESC
                    LIMIT %s
                """
                cur.execute(sql, (int(limit),))
                return cur.fetchall()
    except Exception as e:
        print(f"Latest-updated error: {e}")
        return []


def _get_home_category_page(cate_id: int | None, sort: str, page: int, per_page: int = 20):
    """
    section ใหม่: เลือกหมวด -> sort -> pagination 20/หน้า (รูปแบบเดียวกับ Search)
    อัปเดต: เพิ่ม published_chapters (นับเฉพาะตอนเผยแพร่)
    """
    page = _safe_int(page, 1, 1)
    per_page = int(per_page)
    offset = (page - 1) * per_page

    # sort ที่เป็น filter สถานะ
    status_filter = None
    order_sort = sort
    if sort == 'finished':
        status_filter = 'จบแล้ว'
        order_sort = 'relevance'
    elif sort == 'ongoing':
        status_filter = 'เผยแพร่'
        order_sort = 'relevance'

    allowed = set(SORT_OPTIONS.keys())
    if sort not in allowed:
        sort = 'relevance'
        order_sort = 'relevance'
        status_filter = None

    order_by_map = {
        'new': "n.created_at DESC",
        'rating': "avg_rating DESC, COALESCE(n.updated_at, n.created_at) DESC, n.created_at DESC",
        'bookshelf': "bookshelf_count DESC, n.views DESC, n.created_at DESC",
        'relevance': "n.views DESC, avg_rating DESC, bookshelf_count DESC, n.created_at DESC",
    }
    order_by_sql = order_by_map.get(order_sort, order_by_map['relevance'])

    where = ["n.status IN ('เผยแพร่','จบแล้ว')"]
    params: list = []

    if status_filter:
        where.append("n.status = %s")
        params.append(status_filter)

    if cate_id is not None and int(cate_id) != 0:
        where.append("n.cate_id = %s")
        params.append(int(cate_id))

    where_sql = " AND ".join(where)

    results = []
    total = 0

    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
                # count
                cur.execute(f"SELECT COUNT(*) AS total FROM novels n WHERE {where_sql}", params)
                total = int((cur.fetchone() or {}).get("total") or 0)

                if not total:
                    return {
                        "results": [],
                        "total": 0,
                        "page": 1,
                        "per_page": per_page,
                        "total_pages": 0,
                        "start_item": 0,
                        "end_item": 0
                    }

                total_pages = (total + per_page - 1) // per_page
                if page > total_pages:
                    page = total_pages
                    offset = (page - 1) * per_page

                sel_author, join_author = _author_sql_parts(cur)

                # views availability
                try:
                    cur.execute("DESCRIBE novels")
                    ncols = {r["Field"] for r in cur.fetchall()}
                except Exception:
                    ncols = set()
                has_views = "views" in ncols

                # joins สำหรับ stats
                has_r = _has_relation(cur, "v_novel_rating_stats")
                has_b = _has_relation(cur, "v_novel_bookshelf_counts")
                has_ch_view = _has_relation(cur, "v_novel_chapter_counts")

                join_r = "LEFT JOIN v_novel_rating_stats r ON r.novels_id = n.novels_id" if has_r else ""
                join_b = "LEFT JOIN v_novel_bookshelf_counts b ON b.novels_id = n.novels_id" if has_b else ""
                join_ch = "LEFT JOIN v_novel_chapter_counts ch ON ch.novels_id = n.novels_id" if has_ch_view else ""

                sel_views = "n.views AS views" if has_views else "0 AS views"
                sel_avg = "COALESCE(r.avg_rating, 0) AS avg_rating" if has_r else "0 AS avg_rating"
                sel_rc = "COALESCE(r.rating_count, 0) AS rating_count" if has_r else "0 AS rating_count"
                sel_bc = "COALESCE(b.bookshelf_count, 0) AS bookshelf_count" if has_b else "0 AS bookshelf_count"

                # ยังเก็บ chapter_count เดิมไว้
                sel_cc = "COALESCE(ch.chapter_count, 0) AS chapter_count" if has_ch_view else "0 AS chapter_count"

                # ✅ นับจำนวนตอนเผยแพร่จริง ๆ
                sel_pub_ch = _published_chapters_expr(cur, "n")

                sql = f"""
                    SELECT
                        n.novels_id,
                        n.title,
                        n.description,
                        n.cover,
                        n.status,
                        n.created_at,
                        n.updated_at,
                        {sel_views},

                        {sel_author},
                        c.name AS category_name,

                        {sel_avg},
                        {sel_rc},
                        {sel_bc},
                        {sel_cc},
                        {sel_pub_ch}

                    FROM novels n
                    LEFT JOIN categories c ON c.cate_id = n.cate_id
                    {join_author}
                    {join_r}
                    {join_b}
                    {join_ch}

                    WHERE {where_sql}
                    ORDER BY {order_by_sql}
                    LIMIT %s OFFSET %s
                """
                cur.execute(sql, params + [per_page, offset])
                results = cur.fetchall()

    except Exception as e:
        print(f"Home category section error: {e}")
        results = []
        total = 0

    total_pages = (total + per_page - 1) // per_page if total else 0
    start_item = offset + 1 if total else 0
    end_item = min(offset + per_page, total) if total else 0

    for n in results:
        n["cover_url"] = _process_cover_url(n.get("cover"))
        # เผื่อกรณีเดาไม่ได้จริง ๆ ให้ไม่ว่าง
        if n.get("published_chapters") is None:
            n["published_chapters"] = n.get("chapter_count") or 0

    return {
        "results": results,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "start_item": start_item,
        "end_item": end_item
    }


# ---------- routes ----------
@home_bp.route('/home')
def index():
    current_uid = (g.user or {}).get('users_id') if hasattr(g, 'user') and g.user else None

    # existing section: อัปเดตล่าสุด
    top10 = _get_latest_updated(current_uid, limit=10)

    for n in top10:
        n['cover'] = _process_cover_url(n.get('cover'))
        n.setdefault('avg_rating', 0)
        n.setdefault('rating_count', 0)
        n.setdefault('views', 0)
        n.setdefault('chapter_count', 0)
        n.setdefault('published_chapters', n.get('chapter_count', 0) or 0)

    categories = _get_categories()

    cate_id = request.args.get('cate_id', default=0, type=int)
    sort = request.args.get('sort', default='rating', type=str)
    if sort not in SORT_OPTIONS:
        sort = 'relevance'
    page = _safe_int(request.args.get('page', 1), 1, 1)

    top_cates = categories[:9]
    more_cates = categories[9:]

    cat_ctx = {
        "cat_results": [],
        "cat_total": 0,
        "cat_page": 1,
        "cat_per_page": 20,
        "cat_total_pages": 0,
        "cat_start_item": 0,
        "cat_end_item": 0
    }

    if cate_id is not None:
        data = _get_home_category_page(cate_id=cate_id, sort=sort, page=page, per_page=20)
        cat_ctx.update({
            "cat_results": data["results"],
            "cat_total": data["total"],
            "cat_page": data["page"],
            "cat_per_page": data["per_page"],
            "cat_total_pages": data["total_pages"],
            "cat_start_item": data["start_item"],
            "cat_end_item": data["end_item"],
        })

    return render_template(
        'home.html',
        top10=top10,
        month_label="อัปเดตล่าสุด",
        categories=categories,
        user=getattr(g, 'user', None),

        cate_id=cate_id,
        sort=sort,
        sort_options=SORT_OPTIONS,
        top_cates=top_cates,
        more_cates=more_cates,
        **cat_ctx
    )
