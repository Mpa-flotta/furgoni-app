from __future__ import annotations

import io
import os
import secrets
import urllib.request
from datetime import datetime
from functools import wraps

import cloudinary
import cloudinary.uploader
import psycopg
from PIL import Image, ImageOps
from psycopg.rows import dict_row
from flask import (
    Flask, flash, g, redirect, render_template,
    request, send_file, session, url_for
)
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------- CONFIG ----------------

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL non configurata")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-questa-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 🔥 fix upload foto

cloudinary.config(secure=True)

# ---------------- UTILS ----------------

def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_db():
    if "db" not in g:
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db:
        db.close()

# ---------------- DB INIT ----------------

def init_db():
    db = get_db()
    with db.cursor() as cur:

        cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password_hash TEXT,
            created_at TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            id SERIAL PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            email TEXT,
            pin TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS vans (
            id SERIAL PRIMARY KEY,
            plate TEXT UNIQUE,
            model TEXT,
            current_km INTEGER,
            status TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS assignments (
            id SERIAL PRIMARY KEY,
            driver_id INTEGER,
            van_id INTEGER,
            token TEXT,
            status TEXT,
            created_at TEXT,
            pickup_at TEXT,
            return_at TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id SERIAL PRIMARY KEY,
            assignment_id INTEGER,
            stage TEXT,
            filename TEXT,
            uploaded_at TEXT
        );
        """)

    db.commit()

# ---------------- AUTH ----------------

def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper

# ---------------- IMAGE ----------------

def optimize_image(file):
    img = Image.open(file.stream)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((1600, 1600))

    output = io.BytesIO()
    img.save(output, format="JPEG", quality=70)
    output.seek(0)
    return output

def save_photo(file, assignment_id, stage):
    if not file:
        return

    optimized = optimize_image(file)

    result = cloudinary.uploader.upload(
        optimized,
        folder="furgoni_app",
        public_id=f"{assignment_id}_{stage}_{secrets.token_hex(4)}"
    )

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
        INSERT INTO photos (assignment_id, stage, filename, uploaded_at)
        VALUES (%s,%s,%s,%s)
        """, (assignment_id, stage, result["secure_url"], now_iso()))
    db.commit()

# ---------------- ROUTES ----------------

@app.route("/")
def home():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        user = request.form["username"]
        pwd = request.form["password"]

        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM admin_users WHERE username=%s", (user,))
            u = cur.fetchone()

        if u and check_password_hash(u["password_hash"], pwd):
            session["admin"] = True
            return redirect(url_for("dashboard"))

        flash("Credenziali errate")

    return render_template("login.html")

@app.route("/dashboard")
@admin_required
def dashboard():
    db = get_db()

    with db.cursor() as cur:
        cur.execute("SELECT * FROM drivers")
        drivers = cur.fetchall()

        cur.execute("SELECT * FROM vans")
        vans = cur.fetchall()

        cur.execute("""
        SELECT a.*, d.full_name AS driver_name, v.plate, v.model,
        (SELECT COUNT(*) FROM photos p WHERE p.assignment_id=a.id) AS photo_count,
        (SELECT filename FROM photos p WHERE p.assignment_id=a.id LIMIT 1) AS first_photo
        FROM assignments a
        JOIN drivers d ON d.id=a.driver_id
        JOIN vans v ON v.id=a.van_id
        ORDER BY a.id DESC
        """)
        assignments = cur.fetchall()

    return render_template(
        "dashboard.html",
        drivers=drivers,
        vans=vans,
        assignments=assignments,
        active_count=len(assignments),
        completed_count=0,
        grouped_assignments={},
        daily_counts={}
    )

# ---------------- CREATE / DELETE ----------------

@app.post("/drivers/create")
def create_driver():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
        INSERT INTO drivers (full_name,phone,email,pin)
        VALUES (%s,%s,%s,%s)
        """, (
            request.form["full_name"],
            request.form["phone"],
            request.form["email"],
            request.form["pin"]
        ))
    db.commit()
    return redirect(url_for("dashboard"))

@app.post("/drivers/delete/<int:id>")
def delete_driver(id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM drivers WHERE id=%s", (id,))
    db.commit()
    return redirect(url_for("dashboard"))

@app.post("/vans/create")
def create_van():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
        INSERT INTO vans (plate,model,current_km,status)
        VALUES (%s,%s,%s,'Disponibile')
        """, (
            request.form["plate"],
            request.form["model"],
            request.form.get("current_km") or 0
        ))
    db.commit()
    return redirect(url_for("dashboard"))

@app.post("/vans/delete/<int:id>")
def delete_van(id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM vans WHERE id=%s", (id,))
    db.commit()
    return redirect(url_for("dashboard"))

# ---------------- PDF ----------------

@app.route("/pdf/<int:id>")
def pdf(id):
    db = get_db()

    with db.cursor() as cur:
        cur.execute("SELECT * FROM assignments WHERE id=%s", (id,))
        a = cur.fetchone()

        cur.execute("SELECT * FROM photos WHERE assignment_id=%s", (id,))
        photos = cur.fetchall()

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)

    y = 800

    for p in photos:
        try:
            img = ImageReader(urllib.request.urlopen(p["filename"]))
            pdf.drawImage(img, 40, y-120, width=150, height=100)
            y -= 130
        except:
            pass

    pdf.save()
    buffer.seek(0)

    return send_file(buffer, as_attachment=True, download_name="report.pdf")

# ---------------- START ----------------

init_db()

if __name__ == "__main__":
    app.run(debug=True)




