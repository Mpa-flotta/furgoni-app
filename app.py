from __future__ import annotations

import os
import secrets
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL non configurata")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-questa-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

DEFAULT_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin12345")
DEFAULT_ADMIN_PASSWORD_HASH = generate_password_hash(DEFAULT_ADMIN_PASSWORD)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as db:
        with db.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS drivers (
                    id SERIAL PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    phone TEXT,
                    email TEXT
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS vans (
                    id SERIAL PRIMARY KEY,
                    plate TEXT NOT NULL UNIQUE,
                    model TEXT NOT NULL,
                    current_km INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'Disponibile'
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS assignments (
                    id SERIAL PRIMARY KEY,
                    driver_id INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
                    van_id INTEGER NOT NULL REFERENCES vans(id) ON DELETE CASCADE,
                    token TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'Assegnato',
                    created_at TEXT NOT NULL,
                    pickup_at TEXT,
                    return_at TEXT,
                    pickup_km INTEGER,
                    pickup_fuel TEXT,
                    pickup_notes TEXT,
                    pickup_signature TEXT,
                    return_km INTEGER,
                    return_fuel TEXT,
                    return_notes TEXT,
                    return_signature TEXT,
                    body_ok INTEGER DEFAULT 0,
                    tyres_ok INTEGER DEFAULT 0,
                    docs_ok INTEGER DEFAULT 0,
                    lights_ok INTEGER DEFAULT 0
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS photos (
                    id SERIAL PRIMARY KEY,
                    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL
                );
            """)

            cur.execute("SELECT COUNT(*) AS count FROM admin_users;")
            admin_count = cur.fetchone()["count"]

            if admin_count == 0:
                cur.execute(
                    """
                    INSERT INTO admin_users (username, password_hash, created_at)
                    VALUES (%s, %s, %s)
                    """,
                    (DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD_HASH, now_iso()),
                )

            cur.execute("SELECT COUNT(*) AS count FROM drivers;")
            driver_count = cur.fetchone()["count"]

            if driver_count == 0:
                cur.executemany(
                    "INSERT INTO drivers (full_name, phone, email) VALUES (%s, %s, %s)",
                    [
                        ("Mario Rossi", "3331112233", "mario.rossi@example.com"),
                        ("Luigi Bianchi", "3334445566", "luigi.bianchi@example.com"),
                    ],
                )

            cur.execute("SELECT COUNT(*) AS count FROM vans;")
            van_count = cur.fetchone()["count"]

            if van_count == 0:
                cur.executemany(
                    "INSERT INTO vans (plate, model, current_km, status) VALUES (%s, %s, %s, %s)",
                    [
                        ("AB123CD", "Fiat Ducato", 125000, "Disponibile"),
                        ("EF456GH", "Ford Transit", 98000, "Disponibile"),
                    ],
                )

        db.commit()


def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped_view


def save_uploaded_files(files: list[Any], assignment_id: int, stage: str) -> None:
    db = get_db()
    with db.cursor() as cur:
        for file in files:
            if not file or not file.filename:
                continue
            safe_name = secure_filename(file.filename)
            final_name = f"{assignment_id}_{stage}_{secrets.token_hex(4)}_{safe_name}"
            file.save(UPLOAD_DIR / final_name)
            cur.execute(
                """
                INSERT INTO photos (assignment_id, stage, filename, uploaded_at)
                VALUES (%s, %s, %s, %s)
                """,
                (assignment_id, stage, final_name, now_iso()),
            )
    db.commit()


def fetch_dashboard_data() -> dict[str, Any]:
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM drivers ORDER BY full_name")
        drivers = cur.fetchall()

        cur.execute("SELECT * FROM vans ORDER BY plate")
        vans = cur.fetchall()

        cur.execute("""
            SELECT
                a.*,
                d.full_name AS driver_name,
                v.plate,
                v.model
            FROM assignments a
            JOIN drivers d ON d.id = a.driver_id
            JOIN vans v ON v.id = a.van_id
            ORDER BY a.id DESC
        """)
        assignments = cur.fetchall()

        cur.execute("SELECT COUNT(*) AS count FROM assignments WHERE status IN ('Assegnato', 'Preso in carico')")
        active_count = cur.fetchone()["count"]

        cur.execute("SELECT COUNT(*) AS count FROM assignments WHERE status = 'Riconsegnato'")
        completed_count = cur.fetchone()["count"]

    return {
        "drivers": drivers,
        "vans": vans,
        "assignments": assignments,
        "active_count": active_count,
        "completed_count": completed_count,
    }


@app.route("/")
def home():
    if session.get("admin_logged_in"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM admin_users WHERE username = %s", (username,))
            user = cur.fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["admin_logged_in"] = True
            session["admin_username"] = username
            flash("Login effettuato correttamente.", "success")
            return redirect(url_for("dashboard"))

        flash("Credenziali non valide.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logout eseguito.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@admin_required
def dashboard():
    data = fetch_dashboard_data()
    return render_template("dashboard.html", **data)


@app.post("/drivers/create")
@admin_required
def create_driver():
    full_name = request.form.get("full_name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()

    if not full_name:
        flash("Il nome autista è obbligatorio.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO drivers (full_name, phone, email) VALUES (%s, %s, %s)",
            (full_name, phone, email),
        )
    db.commit()

    flash("Autista creato correttamente.", "success")
    return redirect(url_for("dashboard"))


@app.post("/vans/create")
@admin_required
def create_van():
    plate = request.form.get("plate", "").strip().upper()
    model = request.form.get("model", "").strip()
    current_km = request.form.get("current_km", "0").strip()

    if not plate or not model:
        flash("Targa e modello sono obbligatori.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vans (plate, model, current_km, status)
                VALUES (%s, %s, %s, %s)
                """,
                (plate, model, int(current_km or 0), "Disponibile"),
            )
        db.commit()
        flash("Furgone creato correttamente.", "success")
    except psycopg.errors.UniqueViolation:
        db.rollback()
        flash("La targa esiste già.", "error")

    return redirect(url_for("dashboard"))


@app.post("/assignments/create")
@admin_required
def create_assignment():
    driver_id = request.form.get("driver_id")
    van_id = request.form.get("van_id")

    if not driver_id or not van_id:
        flash("Seleziona autista e furgone.", "error")
        return redirect(url_for("dashboard"))

    token = secrets.token_urlsafe(16)
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO assignments (driver_id, van_id, token, created_at, status)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (driver_id, van_id, token, now_iso(), "Assegnato"),
        )
        cur.execute("UPDATE vans SET status = 'Assegnato' WHERE id = %s", (van_id,))
    db.commit()

    flash("Assegnazione creata. Copia il link autista dalla dashboard.", "success")
    return redirect(url_for("dashboard"))


@app.route("/driver/<token>", methods=["GET", "POST"])
def driver_portal(token: str):
    db = get_db()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                a.*,
                d.full_name AS driver_name,
                v.plate,
                v.model,
                v.current_km
            FROM assignments a
            JOIN drivers d ON d.id = a.driver_id
            JOIN vans v ON v.id = a.van_id
            WHERE a.token = %s
            """,
            (token,),
        )
        assignment = cur.fetchone()

    if assignment is None:
        return "Link non valido.", 404

    if request.method == "POST":
        action = request.form.get("action")

        if action == "pickup":
            with db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'Preso in carico',
                        pickup_at = %s,
                        pickup_km = %s,
                        pickup_fuel = %s,
                        pickup_notes = %s,
                        pickup_signature = %s,
                        body_ok = %s,
                        tyres_ok = %s,
                        docs_ok = %s,
                        lights_ok = %s
                    WHERE id = %s
                    """,
                    (
                        now_iso(),
                        request.form.get("pickup_km") or None,
                        request.form.get("pickup_fuel", ""),
                        request.form.get("pickup_notes", ""),
                        request.form.get("pickup_signature", ""),
                        1 if request.form.get("body_ok") else 0,
                        1 if request.form.get("tyres_ok") else 0,
                        1 if request.form.get("docs_ok") else 0,
                        1 if request.form.get("lights_ok") else 0,
                        assignment["id"],
                    ),
                )
                cur.execute(
                    "UPDATE vans SET status = 'In uso', current_km = %s WHERE id = %s",
                    (
                        request.form.get("pickup_km") or assignment["current_km"],
                        assignment["van_id"],
                    ),
                )
            db.commit()
            save_uploaded_files(request.files.getlist("pickup_photos"), assignment["id"], "pickup")
            flash("Presa in carico registrata.", "success")
            return redirect(url_for("driver_portal", token=token))

        if action == "return":
            with db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'Riconsegnato',
                        return_at = %s,
                        return_km = %s,
                        return_fuel = %s,
                        return_notes = %s,
                        return_signature = %s
                    WHERE id = %s
                    """,
                    (
                        now_iso(),
                        request.form.get("return_km") or None,
                        request.form.get("return_fuel", ""),
                        request.form.get("return_notes", ""),
                        request.form.get("return_signature", ""),
                        assignment["id"],
                    ),
                )
                cur.execute(
                    "UPDATE vans SET status = 'Disponibile', current_km = %s WHERE id = %s",
                    (
                        request.form.get("return_km") or assignment["current_km"],
                        assignment["van_id"],
                    ),
                )
            db.commit()
            save_uploaded_files(request.files.getlist("return_photos"), assignment["id"], "return")
            flash("Riconsegna registrata.", "success")
            return redirect(url_for("driver_portal", token=token))

    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM photos WHERE assignment_id = %s ORDER BY id DESC",
            (assignment["id"],),
        )
        photos = cur.fetchall()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                a.*,
                d.full_name AS driver_name,
                v.plate,
                v.model,
                v.current_km
            FROM assignments a
            JOIN drivers d ON d.id = a.driver_id
            JOIN vans v ON v.id = a.van_id
            WHERE a.token = %s
            """,
            (token,),
        )
        assignment = cur.fetchone()

    return render_template("driver.html", assignment=assignment, photos=photos)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


init_db()

if __name__ == "__main__":
    app.run(debug=True)
