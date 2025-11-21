from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import urlparse
from datetime import datetime, timedelta
import re

from db import mysql, query_one, execute

auth_bp = Blueprint('auth', __name__, template_folder='templates')

# ---------- Utilities ----------
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

def is_safe_next(next_url: str) -> bool:
    if not next_url:
        return False
    u = urlparse(next_url)
    return (u.scheme == '' and u.netloc == '' and u.path.startswith('/'))

def login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapper(*args, **kwargs):
        # ใช้คีย์ใหม่ user_id เป็นหลัก
        if not session.get('user_id'):
            return redirect(url_for('auth.login', next=request.path))
        return view(*args, **kwargs)
    return wrapper

def roles_required(*roles):
    from functools import wraps
    roles_set = set(roles)
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get('user_id'):
                return redirect(url_for('auth.login', next=request.path))
            if session.get('role') not in roles_set:
                flash('คุณไม่มีสิทธิ์เข้าถึงหน้านี้', 'error')
                return redirect(url_for('home'))
            return view(*args, **kwargs)
        return wrapper
    return decorator

@auth_bp.before_app_request
def load_current_user():
    # ✅ รองรับทั้งสองคีย์ เพื่อความเข้ากันได้ย้อนหลัง
    uid = session.get('user_id') or session.get('uid')
    if not uid:
        g.user = None
        return
    g.user = query_one("""
        SELECT users_id, username, email, role, is_active, pfpic
        FROM users
        WHERE users_id = %s
        LIMIT 1
    """, (uid,))

def _is_active_flag(val) -> bool:
    """
    รองรับทั้งฐานเก่า (tinyint 0/1) และฐานใหม่ (ENUM ภาษาไทย)
    """
    if val is None:
        return False
    if isinstance(val, str):
        v = val.strip()
        # ปรับได้ตามสคีมาจริงของคุณ
        return v in ('บัญชีปกติ', 'active', 'ACTIVE', 'Active')
    return bool(val)

# ---------- Routes ----------
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')

    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''
    remember = bool(request.form.get('remember'))

    if not EMAIL_RE.match(email):
        flash('รูปแบบอีเมลไม่ถูกต้อง', 'error')
        return redirect(url_for('auth.login'))

    user = query_one("""
        SELECT users_id, username, email, role, is_active, password_hash
        FROM users
        WHERE email = %s
        LIMIT 1
    """, (email,))

    if not user or not _is_active_flag(user.get('is_active')):
        flash('อีเมลหรือรหัสผ่านไม่ถูกต้อง', 'error')
        return redirect(url_for('auth.login'))

    pw_hash = (user.get('password_hash') or '').strip()
    if not (pw_hash and check_password_hash(pw_hash, password)):
        flash('อีเมลหรือรหัสผ่านไม่ถูกต้อง', 'error')
        return redirect(url_for('auth.login'))

    # ✅ ตั้งค่าเซสชัน: ใส่ทั้ง user_id และ uid เพื่อกันโค้ดเก่า
    session.clear()
    session['user_id'] = user['users_id']
    session['uid'] = user['users_id']          # <- เพิ่มความเข้ากันได้ย้อนหลัง
    session['role'] = user.get('role', 'user')
    session['username'] = user.get('username')
    session.permanent = bool(remember)

    # อัปเดต last_login_at
    try:
        execute("UPDATE users SET last_login_at = %s WHERE users_id = %s",
                (datetime.now(), user['users_id']))
    except Exception:
        current_app.logger.exception("update last_login_at failed")

    next_url = request.args.get('next') or request.form.get('next')
    if next_url and is_safe_next(next_url):
        return redirect(next_url)

    # ไปหน้า dashboard สำหรับ admin / superadmin, นอกนั้นไปหน้า home
    return redirect(url_for('dashboard' if user.get('role') in ('admin', 'superadmin') else 'home'))

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template('register.html')

    username  = (request.form.get('username') or '').strip()
    email     = (request.form.get('email') or '').strip().lower()
    password  = request.form.get('password') or ''
    password2 = request.form.get('password2') or ''
    gender_in = (request.form.get('gender') or '').strip()

    # ให้ตรงกับฟรอนต์ (ชาย/หญิง/LGBTQ+/ไม่ระบุ)
    gender_map = {'ชาย': 'ชาย', 'หญิง': 'หญิง', 'LGBTQ+': 'LGBTQ+', 'ไม่ระบุ': 'ไม่ระบุ'}
    gender = gender_map.get(gender_in, 'ไม่ระบุ')

    if not username or not EMAIL_RE.match(email):
        flash('กรุณากรอกชื่อผู้ใช้และอีเมลให้ถูกต้อง', 'error')
        return redirect(url_for('auth.register'))

    if len(password) < 8 or not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
        flash('รหัสผ่านต้องอย่างน้อย 8 ตัวอักษร และมีทั้งตัวอักษรและตัวเลข', 'error')
        return redirect(url_for('auth.register'))

    if password != password2:
        flash('ยืนยันรหัสผ่านไม่ตรงกัน', 'error')
        return redirect(url_for('auth.register'))

    exist = query_one(
        "SELECT users_id FROM users WHERE email = %s OR username = %s LIMIT 1",
        (email, username)
    )
    if exist:
        flash('อีเมลหรือชื่อผู้ใช้นี้ถูกใช้งานแล้ว', 'error')
        return redirect(url_for('auth.register'))

    # ใช้ scrypt ให้ตรงกับฐานข้อมูลใหม่ (ถ้าเวอร์ชัน werkzeug ของคุณไม่รองรับ ให้ตัด method= ออก)
    pw_hash = generate_password_hash(password, method='scrypt')

    try:
        execute("""
            INSERT INTO users (username, email, gender, password_hash, role, is_active, created_at)
            VALUES (%s, %s, %s, %s, 'user', 'บัญชีปกติ', NOW())
        """, (username, email, gender, pw_hash))
    except Exception:
        current_app.logger.exception("register failed")
        flash('สมัครสมาชิกไม่สำเร็จ กรุณาลองใหม่อีกครั้ง', 'error')
        return redirect(url_for('auth.register'))

    flash('สมัครสมาชิกสำเร็จ! กรุณาเข้าสู่ระบบ', 'success')
    return redirect(url_for('auth.login'))

# แนะนำให้ใช้ POST เท่านั้น แต่คง GET ไว้ถ้าปุ่ม/ลิงก์เดิมเรียกด้วย GET
@auth_bp.route('/logout', methods=['POST', 'GET'])
def logout():
    session.clear()
    return redirect(url_for('auth.login'))

@auth_bp.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'GET':
        return render_template('forgot.html')

    username = (request.form.get('username') or '').strip()
    email = (request.form.get('email') or '').strip().lower()
    
    if not username or not email:
        flash('กรุณากรอกชื่อผู้ใช้และอีเมล', 'error')
        return redirect(url_for('auth.forgot_password'))

    if not EMAIL_RE.match(email):
        flash('รูปแบบอีเมลไม่ถูกต้อง', 'error')
        return redirect(url_for('auth.forgot_password'))

    user = query_one(
        """
        SELECT users_id, email, is_active
        FROM users
        WHERE LOWER(username)=LOWER(%s) AND LOWER(email)=LOWER(%s)
        LIMIT 1
        """,
        (username, email)
    )
    if not user or not _is_active_flag(user.get('is_active')):
        flash('ชื่อผู้ใช้หรืออีเมลไม่ถูกต้อง', 'error')
        return redirect(url_for('auth.forgot_password'))

    session['reset_uid'] = user['users_id']
    session['reset_expires'] = (datetime.now() + timedelta(minutes=15)).timestamp()
    return redirect(url_for('auth.reset_password'))

@auth_bp.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    uid = session.get('reset_uid')
    exp_ts = session.get('reset_expires')
    now_ts = datetime.now().timestamp()

    if not uid or not exp_ts or now_ts > float(exp_ts):
        session.pop('reset_uid', None)
        session.pop('reset_expires', None)
        flash('เซสชันรีเซ็ตรหัสผ่านหมดอายุ/ไม่ถูกต้อง กรุณาขอใหม่อีกครั้ง', 'error')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'GET':
        return render_template('reset_password.html')

    new_pw  = request.form.get('password') or ''
    new_pw2 = request.form.get('password2') or ''

    if len(new_pw) < 8 or not re.search(r'[A-Za-z]', new_pw) or not re.search(r'\d', new_pw):
        flash('รหัสผ่านต้องอย่างน้อย 8 ตัวอักษร และมีทั้งตัวอักษรและตัวเลข', 'error')
        return render_template('reset_password.html')

    if new_pw != new_pw2:
        flash('ยืนยันรหัสผ่านไม่ตรงกัน', 'error')
        return render_template('reset_password.html')

    # เก็บให้เข้ากันกับฐานใหม่
    pw_hash = generate_password_hash(new_pw, method='scrypt')
    try:
        execute("""
            UPDATE users
            SET password_hash = %s,
                updated_at = NOW()
            WHERE users_id = %s
        """, (pw_hash, uid))
    except Exception:
        current_app.logger.exception("update password failed")
        flash('เกิดข้อผิดพลาด ไม่สามารถเปลี่ยนรหัสผ่านได้', 'error')
        return render_template('reset_password.html')

    session.pop('reset_uid', None)
    session.pop('reset_expires', None)
    flash('ตั้งรหัสผ่านใหม่เรียบร้อย! กรุณาเข้าสู่ระบบ', 'success')
    return redirect(url_for('auth.login'))
