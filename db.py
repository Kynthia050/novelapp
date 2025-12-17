import os
from contextlib import closing
import MySQLdb
import MySQLdb.cursors

# ---------------- Core Connection ----------------
def get_db_connection():
    """
    สร้าง connection ใหม่โดยอ่านค่าจาก Environment Variables โดยตรง
    (รองรับทั้ง Railway และ Localhost)
    """
    connect_kwargs = {
        # อ่านค่าจาก Railway Variables (ถ้าไม่มีจะใช้ค่า Default สำหรับ XAMPP/Localhost)
        'host': os.environ.get('MYSQL_HOST', '127.0.0.1'),
        'user': os.environ.get('MYSQL_USER', 'root'),
        'password': os.environ.get('MYSQL_PASSWORD', ''),
        'db': os.environ.get('MYSQL_DB', 'readweb'), # <--- เช็คชื่อ Database ตรงนี้ให้ถูกนะครับ
        'port': int(os.environ.get('MYSQL_PORT', 3306)),
        
        # การตั้งค่าเพิ่มเติมที่จำเป็น
        'charset': 'utf8mb4',
        'cursorclass': MySQLdb.cursors.DictCursor, # สำคัญมาก: เพื่อให้เรียกข้อมูลแบบ user['email'] ได้
        'connect_timeout': 10
    }

    # สร้างการเชื่อมต่อ
    conn = MySQLdb.connect(**connect_kwargs)

    # พยายามตั้งค่าให้ Auto Reconnect กรณีเน็ตหลุด
    try:
        conn.ping(True)
    except Exception:
        pass

    return conn


# ---------------- Helpers (ฟังก์ชันช่วยดึงข้อมูล) ----------------
# ส่วนนี้คงเดิมไว้ เพราะ app.py และ auth.py ของคุณเรียกใช้ฟังก์ชันพวกนี้อยู่

def query_one(sql: str, params=None):
    """ดึงข้อมูล 1 แถว (return Dictionary)"""
    with closing(get_db_connection()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

def query_all(sql: str, params=None):
    """ดึงข้อมูลหลายแถว (return List of Dictionaries)"""
    with closing(get_db_connection()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

def execute(sql: str, params=None):
    """สั่ง Insert/Update/Delete (return rowcount, last_id)"""
    with closing(get_db_connection()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rowcount = cur.rowcount
            last_id = getattr(cur, "lastrowid", None)
        conn.commit() # สั่งบันทึกข้อมูลลง Database
    return rowcount, last_id

# ---------------- Setup (Optional) ----------------
def init_db(app):
    """
    ฟังก์ชันเปล่า เพื่อให้ app.py เรียกใช้ได้โดยไม่ error
    (เพราะเราอ่านค่า env ใน get_db_connection โดยตรงแล้ว จึงไม่ต้อง setup อะไรเพิ่ม)
    """
    pass