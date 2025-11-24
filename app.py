from flask import (
    Flask, render_template, request, send_from_directory,
    redirect, url_for, g
)
from flask_wtf.csrf import CSRFProtect, generate_csrf
from datetime import timedelta
from openai import OpenAI
from db import init_db
from auth import auth_bp, roles_required
from home import home_bp
from writingform import writing_bp
from profileusers import profile_bp
from novelcover import novel_bp
from new_novel import new_novel_bp
from edit_novel import editnovel_bp, api_bp
from mywrite import mywrite_bp
from notification import noti_bp
from readingform import reading_bp
from authorwork import authorwork_bp
from bookshelf import bookshelf_bp
from comment import comment_bp
from search import search_bp
import os


app = Flask(__name__)

# ต้องมี SECRET_KEY เพื่อให้ CSRF และ session ทำงานได้
app.config['SECRET_KEY'] = 'change-me-to-a-long-random-string'
app.permanent_session_lifetime = timedelta(days=14)

# ---------- สร้าง OpenAI client ----------
api_key = os.environ.get("OPENAI_API_KEY")

if not api_key:
    # ถ้าไม่มี key ให้แค่เตือน และปิดฟีเจอร์ AI summary ไปก่อน
    print(
        "[WARNING] OPENAI_API_KEY ยังไม่ได้ตั้งค่า "
        "ฟีเจอร์สรุปความคิดเห็นด้วย AI จะไม่สามารถใช้งานได้"
    )
    client = None
else:
    try:
        client = OpenAI(api_key=api_key)
        print("[INFO] OpenAI client ถูกสร้างเรียบร้อยแล้ว")
    except Exception as e:
        # กันเคส key ผิด / config มีปัญหา
        print("[ERROR] สร้าง OpenAI client ไม่สำเร็จ:", repr(e))
        client = None

# เก็บ client ไว้ให้ blueprint อื่นใช้ เช่น novelcover.generate_comment_summary
app.config['OPENAI_CLIENT'] = client
# -----------------------------------------

# เปิดใช้ CSRF protection ทั้งแอป
csrf = CSRFProtect(app)

# Initial DB connection / teardown handlers
init_db(app)

# ---------- Register Blueprints ----------

app.register_blueprint(auth_bp, url_prefix='/auth')
app.register_blueprint(home_bp)
app.register_blueprint(writing_bp, url_prefix='/writing')
app.register_blueprint(profile_bp)
app.register_blueprint(novel_bp)
app.register_blueprint(new_novel_bp)
app.register_blueprint(editnovel_bp)
app.register_blueprint(mywrite_bp)
app.register_blueprint(noti_bp)
app.register_blueprint(reading_bp, url_prefix='/reading')
app.register_blueprint(authorwork_bp)
app.register_blueprint(api_bp)
app.register_blueprint(bookshelf_bp)
app.register_blueprint(comment_bp)
app.register_blueprint(search_bp)


# ทำให้ใช้ {{ csrf_token() }} ในทุก template ได้
@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=generate_csrf)


# ---------- Pages (wrapper ไปยัง blueprint / template เดิม) ----------

@app.route('/auth')
def login():
    return render_template('login.html')


@app.route('/home')
def home():
    return render_template('home.html')


@app.route('/novelcover')
@roles_required('user')
def novelcover():
    return render_template('novelcover.html')


@app.route('/readingform')
@roles_required('user')
def readingform():
    return render_template('readingform.html')


@app.route('/authorwork')
@roles_required('user')
def authorwork():
    # Redirect to the canonical author works page with the current user's id
    if not getattr(g, 'user', None):
        return redirect(url_for('auth.login'))
    return redirect(url_for('authorwork.author_works', author_id=g.user['users_id']))


@app.route('/bookshelf')
@roles_required('user')
def bookshelf():
    return render_template('bookshelf.html')


@app.route('/mywrite')
@roles_required('user')
def mywrite():
    return redirect(url_for('mywrite.mywrite_index'))


@app.route('/writingform')
@roles_required('user')
def writingform():
    from flask import request, abort
    nid = request.args.get('novels_id', type=int)
    if not nid:
        abort(400, description="ต้องระบุ novels_id")
    return redirect(url_for('writing.writing_form', novels_id=nid))


@app.route('/new_novel')
@roles_required('user')
def new_novel():
    return render_template('new_novel.html')


@app.route('/dashboard')
@roles_required('admin', 'superadmin')
def dashboard():
    return render_template('dashboard.html')


if __name__ == '__main__':
    app.run(debug=True)