from flask import (
    Blueprint, render_template, request, redirect, url_for,
    abort, current_app, session
)
from db import mysql
import MySQLdb.cursors
from pathlib import Path
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
import uuid, os

profile_bp = Blueprint('profile', __name__, template_folder='templates')

# ===== Files & Upload rules =====
ALLOWED = {"jpg", "jpeg", "png"}

# ===== Gender mapping (form <-> DB) =====
GENDER_FORM_TO_DB = {
    "": "ไม่ระบุ",
    "male": "ชาย",
    "female": "หญิง",
    "LGBTQ+": "อื่น ๆ",
}
GENDER_DB_TO_FORM = {v: k for k, v in GENDER_FORM_TO_DB.items()}

def dcur():
    return mysql.connection.cursor(MySQLdb.cursors.DictCursor)

def get_profile_dir():
    return Path(current_app.root_path) / "static" / "profile"

def build_avatar_url(pfpic: str | None):
    if not pfpic:
        return url_for("static", filename="profile/default.png")
    if pfpic.startswith(("http://", "https://", "/static/")):
        return pfpic
    return url_for("static", filename=pfpic)

def save_avatar(users_id: int, fs):
    if not fs or not fs.filename:
        return None
    ext = fs.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED:
        raise ValueError("รองรับเฉพาะไฟล์ .jpg .jpeg .png")
    new_name = f"u{users_id}_{uuid.uuid4().hex}.{ext}"
    profile_dir = get_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    fs.save((profile_dir / secure_filename(new_name)).as_posix())
    return f"profile/{new_name}"

def verify_password_hash(stored: str | None, provided: str | None) -> bool:
    if not stored or provided is None:
        return False
    try:
        # ไฟล์ SQL ใช้ scrypt/werkzeug เป็นหลัก
        return check_password_hash(stored, provided)
    except Exception:
        # เผื่อเคสเก่าเป็น plaintext (ไม่แนะนำ)
        return stored == provided

def get_current_user():
    """
    ใช้ session['uid'] เป็นหลัก (ผู้ใช้ที่ล็อกอิน)
    สำรอง: header X-User-Id (สำหรับทดสอบ/preview)
    """
    uid = session.get("uid") or request.headers.get("X-User-Id")
    if not uid:
        abort(401, "ยังไม่ได้ล็อกอิน")

    cur = dcur()
    cur.execute("SELECT * FROM users WHERE users_id=%s", (int(uid),))
    u = cur.fetchone()
    cur.close()
    if not u:
        abort(404, "ไม่พบผู้ใช้ที่กำหนด")
    return u

@profile_bp.route("/profileusers", methods=["GET", "POST"])
def profileusers():
    if request.method == "GET":
        u = get_current_user()
        user_ctx = {
            "users_id": u["users_id"],
            "username": u.get("username"),
            "email": u.get("email"),
            "avatar_url": build_avatar_url(u.get("pfpic")),
            "created_at": u.get("created_at"),
            "updated_at": u.get("updated_at"),
            # ให้ฟรอนต์เลือก option ได้ตรงกับค่าปัจจุบัน
            "gender": GENDER_DB_TO_FORM.get((u.get("gender") or "").strip() or "ไม่ระบุ", ""),
        }
        return render_template("profileusers.html", user=user_ctx)

    # ---------- POST: update ----------
    u = get_current_user()
    users_id = u["users_id"]

    username    = (request.form.get("username") or "").strip()
    email       = (request.form.get("email") or "").strip()
    gender_form = (request.form.get("gender") or "").strip()   # '', male, female, LGBTQ+
    pwd_current = request.form.get("password_current") or ""
    pwd_new     = request.form.get("password_new") or ""
    pwd_confirm = request.form.get("password_confirm") or ""
    remove_flag = (request.form.get("remove_avatar") or "").strip()
    avatar_file = request.files.get("avatar")

    updates, params = [], []

    # --- Avatar ---
    if remove_flag == "1":
        updates += ["pfpic = NULL", "pfpic_updated_at = NOW()"]
    elif avatar_file and avatar_file.filename:
        try:
            new_path = save_avatar(users_id, avatar_file)
        except ValueError as e:
            return (str(e), 400)
        updates += ["pfpic = %s", "pfpic_updated_at = NOW()"]
        params.append(new_path)

    # --- Username (unique) ---
    if username and username != (u.get("username") or ""):
        cur = dcur()
        cur.execute(
            "SELECT 1 FROM users WHERE username=%s AND users_id<>%s",
            (username, users_id)
        )
        taken = cur.fetchone() is not None
        cur.close()
        if taken:
            return ("ชื่อผู้ใช้นี้ถูกใช้งานแล้ว", 400)
        updates.append("username = %s")
        params.append(username)

    # --- Email (unique) ---
    if email and email != (u.get("email") or ""):
        cur = dcur()
        cur.execute(
            "SELECT 1 FROM users WHERE email=%s AND users_id<>%s",
            (email, users_id)
        )
        taken = cur.fetchone() is not None
        cur.close()
        if taken:
            return ("อีเมลนี้ถูกใช้งานโดยผู้ใช้อื่นแล้ว", 400)
        updates.append("email = %s")
        params.append(email)

    # --- Gender (แก้ได้ครั้งเดียว หากเดิมเป็น 'ไม่ระบุ') ---
    db_gender_current = (u.get("gender") or "").strip() or "ไม่ระบุ"
    db_gender_new = GENDER_FORM_TO_DB.get(gender_form, "ไม่ระบุ")
    if db_gender_new != db_gender_current:
        if db_gender_current == "ไม่ระบุ":
            updates.append("gender = %s")
            params.append(db_gender_new)
        else:
            return ("เพศแก้ไขได้เพียงครั้งเดียวเท่านั้น", 400)

    # --- Password change ---
    if pwd_new or pwd_confirm:
        if len(pwd_new) < 8:
            return ("รหัสผ่านใหม่ต้องอย่างน้อย 8 ตัวอักษร", 400)
        if pwd_new != pwd_confirm:
            return ("รหัสผ่านใหม่และยืนยันไม่ตรงกัน", 400)
        if not verify_password_hash(u.get("password_hash"), pwd_current):
            return ("รหัสผ่านปัจจุบันไม่ถูกต้อง", 400)
        new_hash = generate_password_hash(pwd_new)
        updates.append("password_hash = %s")
        params.append(new_hash)

    if not updates:
        return redirect(url_for("profile.profileusers"))

    updates.append("updated_at = NOW()")
    params.append(users_id)

    sql = f"UPDATE users SET {', '.join(updates)} WHERE users_id = %s"
    cur = dcur()
    cur.execute(sql, tuple(params))
    mysql.connection.commit()
    cur.close()

    return redirect(url_for("profile.profileusers"))

def get_active_user():
    # alias เพื่อความเข้ากันได้กับโค้ดเก่า
    return get_current_user()
