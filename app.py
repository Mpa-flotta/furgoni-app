from __future__ import annotations

import os
import secrets
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

import psycopg
from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL non configurata su Render")

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

    return render_template_string(LOGIN_TEMPLATE)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logout eseguito.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@admin_required
def dashboard():
    data = fetch_dashboard_data()
    return render_template_string(DASHBOARD_TEMPLATE, **data)


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

    return render_template_string(DRIVER_TEMPLATE, assignment=assignment, photos=photos)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


LOGIN_TEMPLATE = """
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Login admin</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background: #f3f4f6; color: #111827; }
    .wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
    .card { width: 100%; max-width: 420px; background: white; border-radius: 18px; padding: 24px; box-shadow: 0 6px 18px rgba(0,0,0,0.08); }
    h1 { margin-top: 0; }
    input, button { width: 100%; padding: 12px; margin-top: 8px; margin-bottom: 12px; border-radius: 10px; border: 1px solid #d1d5db; box-sizing: border-box; }
    button { background: #111827; color: white; border: none; cursor: pointer; }
    .flash { padding: 10px 12px; border-radius: 12px; margin-bottom: 12px; background: #eef2ff; }
    .muted { color: #6b7280; font-size: 14px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Login amministratore</h1>

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          {% for category, message in messages %}
            <div class="flash">{{ message }}</div>
          {% endfor %}
        {% endif %}
      {% endwith %}

      <form method="post">
        <label>Username</label>
        <input type="text" name="username" required>

        <label>Password</label>
        <input type="password" name="password" required>

        <button type="submit">Entra</button>
      </form>

      <div class="muted">Accesso iniziale: admin / admin12345</div>
    </div>
  </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dashboard presa in carico furgoni</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background: #f5f7fb; color: #1f2937; }
    .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
    .grid { display: grid; gap: 16px; }
    .grid-3 { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
    .grid-2 { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
    .card { background: white; border-radius: 16px; padding: 20px; box-shadow: 0 6px 18px rgba(0,0,0,0.08); }
    h1, h2 { margin-top: 0; }
    input, select, textarea, button {
      width: 100%; padding: 10px 12px; margin-top: 8px; margin-bottom: 12px;
      border: 1px solid #d1d5db; border-radius: 10px; box-sizing: border-box;
    }
    button { background: #111827; color: white; cursor: pointer; border: none; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 10px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: top; }
    .badge { display: inline-block; padding: 6px 10px; border-radius: 999px; font-size: 12px; background: #e5e7eb; }
    .small { font-size: 12px; color: #6b7280; word-break: break-all; }
    .stats { font-size: 28px; font-weight: bold; }
    .flash { padding: 10px 12px; border-radius: 12px; margin-bottom: 12px; background: #eef2ff; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    a { color: #1d4ed8; text-decoration: none; }
    .logout { background: #111827; color: white; padding: 10px 14px; border-radius: 10px; }
  </style>
</head>
<body>
  <div class="container">
    <div class="topbar">
      <h1>Dashboard presa in carico furgoni</h1>
      <a class="logout" href="{{ url_for('logout') }}">Logout</a>
    </div>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="flash">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="grid grid-3">
      <div class="card"><div>Assegnazioni attive</div><div class="stats">{{ active_count }}</div></div>
      <div class="card"><div>Riconsegne completate</div><div class="stats">{{ completed_count }}</div></div>
      <div class="card"><div>Furgoni censiti</div><div class="stats">{{ vans|length }}</div></div>
    </div>

    <div class="grid grid-2" style="margin-top: 16px;">
      <div class="card">
        <h2>Nuovo autista</h2>
        <form method="post" action="{{ url_for('create_driver') }}">
          <input type="text" name="full_name" placeholder="Nome e cognome" required>
          <input type="text" name="phone" placeholder="Telefono">
          <input type="email" name="email" placeholder="Email">
          <button type="submit">Salva autista</button>
        </form>
      </div>

      <div class="card">
        <h2>Nuovo furgone</h2>
        <form method="post" action="{{ url_for('create_van') }}">
          <input type="text" name="plate" placeholder="Targa" required>
          <input type="text" name="model" placeholder="Modello" required>
          <input type="number" name="current_km" placeholder="KM attuali">
          <button type="submit">Salva furgone</button>
        </form>
      </div>
    </div>

    <div class="card" style="margin-top: 16px;">
      <h2>Crea assegnazione</h2>
      <form method="post" action="{{ url_for('create_assignment') }}">
        <select name="driver_id" required>
          <option value="">Seleziona autista</option>
          {% for d in drivers %}
            <option value="{{ d.id }}">{{ d.full_name }}</option>
          {% endfor %}
        </select>
        <select name="van_id" required>
          <option value="">Seleziona furgone</option>
          {% for v in vans %}
            <option value="{{ v.id }}">{{ v.plate }} - {{ v.model }} ({{ v.status }})</option>
          {% endfor %}
        </select>
        <button type="submit">Genera link autista</button>
      </form>
    </div>

    <div class="card" style="margin-top: 16px; overflow-x: auto;">
      <h2>Storico assegnazioni</h2>
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Autista</th>
            <th>Furgone</th>
            <th>Stato</th>
            <th>Creato</th>
            <th>Link autista</th>
          </tr>
        </thead>
        <tbody>
          {% for a in assignments %}
            <tr>
              <td>{{ a.id }}</td>
              <td>{{ a.driver_name }}</td>
              <td>{{ a.plate }} - {{ a.model }}</td>
              <td><span class="badge">{{ a.status }}</span></td>
              <td>{{ a.created_at }}</td>
              <td>
                <a href="{{ url_for('driver_portal', token=a.token) }}" target="_blank">Apri</a>
                <div class="small">{{ request.host_url.rstrip('/') }}{{ url_for('driver_portal', token=a.token) }}</div>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""

DRIVER_TEMPLATE = """
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Portale autista</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background: #f3f4f6; color: #111827; }
    .container { max-width: 900px; margin: 0 auto; padding: 20px; }
    .card { background: white; border-radius: 16px; padding: 20px; margin-bottom: 16px; box-shadow: 0 6px 18px rgba(0,0,0,0.08); }
    input, select, textarea, button { width: 100%; padding: 10px 12px; margin-top: 8px; margin-bottom: 12px; border-radius: 10px; border: 1px solid #d1d5db; box-sizing: border-box; }
    button { background: #111827; color: white; border: none; }
    .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
    .checkboxes label { display: block; margin: 6px 0; }
    .flash { padding: 10px 12px; border-radius: 12px; margin-bottom: 12px; background: #eef2ff; }
    .gallery { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
    .gallery img { width: 100%; height: 120px; object-fit: cover; border-radius: 12px; }
    .muted { color: #6b7280; }
  </style>
</head>
<body>
  <div class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="flash">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="card">
      <h1>Portale autista</h1>
      <p><strong>Autista:</strong> {{ assignment.driver_name }}</p>
      <p><strong>Furgone:</strong> {{ assignment.plate }} - {{ assignment.model }}</p>
      <p><strong>Stato pratica:</strong> {{ assignment.status }}</p>
      <p class="muted">Usa questa pagina da smartphone per registrare presa in carico e riconsegna.</p>
    </div>

    <div class="card">
      <h2>Presa in carico</h2>
      <form method="post" enctype="multipart/form-data">
        <input type="hidden" name="action" value="pickup">
        <div class="row">
          <div>
            <label>KM alla presa in carico</label>
            <input type="number" name="pickup_km" value="{{ assignment.pickup_km or assignment.current_km or '' }}" required>
          </div>
          <div>
            <label>Carburante</label>
            <select name="pickup_fuel" required>
              <option value="">Seleziona</option>
              <option {% if assignment.pickup_fuel == 'Pieno' %}selected{% endif %}>Pieno</option>
              <option {% if assignment.pickup_fuel == '3/4' %}selected{% endif %}>3/4</option>
              <option {% if assignment.pickup_fuel == '1/2' %}selected{% endif %}>1/2</option>
              <option {% if assignment.pickup_fuel == '1/4' %}selected{% endif %}>1/4</option>
              <option {% if assignment.pickup_fuel == 'Riserva' %}selected{% endif %}>Riserva</option>
            </select>
          </div>
        </div>

        <div class="checkboxes">
          <label><input type="checkbox" name="body_ok" {% if assignment.body_ok %}checked{% endif %}> Carrozzeria ok</label>
          <label><input type="checkbox" name="tyres_ok" {% if assignment.tyres_ok %}checked{% endif %}> Gomme ok</label>
          <label><input type="checkbox" name="docs_ok" {% if assignment.docs_ok %}checked{% endif %}> Documenti presenti</label>
          <label><input type="checkbox" name="lights_ok" {% if assignment.lights_ok %}checked{% endif %}> Luci ok</label>
        </div>

        <label>Note</label>
        <textarea name="pickup_notes" rows="4" placeholder="Segnala danni o anomalie">{{ assignment.pickup_notes or '' }}</textarea>

        <label>Firma</label>
        <input type="text" name="pickup_signature" value="{{ assignment.pickup_signature or assignment.driver_name }}" required>

        <label>Foto alla presa in carico</label>
        <input type="file" name="pickup_photos" multiple accept="image/*">

        <button type="submit">Conferma presa in carico</button>
      </form>
    </div>

    <div class="card">
      <h2>Riconsegna</h2>
      <form method="post" enctype="multipart/form-data">
        <input type="hidden" name="action" value="return">
        <div class="row">
          <div>
            <label>KM alla riconsegna</label>
            <input type="number" name="return_km" value="{{ assignment.return_km or '' }}" required>
          </div>
          <div>
            <label>Carburante</label>
            <select name="return_fuel" required>
              <option value="">Seleziona</option>
              <option {% if assignment.return_fuel == 'Pieno' %}selected{% endif %}>Pieno</option>
              <option {% if assignment.return_fuel == '3/4' %}selected{% endif %}>3/4</option>
              <option {% if assignment.return_fuel == '1/2' %}selected{% endif %}>1/2</option>
              <option {% if assignment.return_fuel == '1/4' %}selected{% endif %}>1/4</option>
              <option {% if assignment.return_fuel == 'Riserva' %}selected{% endif %}>Riserva</option>
            </select>
          </div>
        </div>

        <label>Note riconsegna</label>
        <textarea name="return_notes" rows="4" placeholder="Segnala eventuali danni o problemi">{{ assignment.return_notes or '' }}</textarea>

        <label>Firma riconsegna</label>
        <input type="text" name="return_signature" value="{{ assignment.return_signature or assignment.driver_name }}" required>

        <label>Foto alla riconsegna</label>
        <input type="file" name="return_photos" multiple accept="image/*">

        <button type="submit">Conferma riconsegna</button>
      </form>
    </div>

    <div class="card">
      <h2>Foto caricate</h2>
      {% if photos %}
        <div class="gallery">
          {% for photo in photos %}
            <a href="{{ url_for('uploaded_file', filename=photo.filename) }}" target="_blank">
              <img src="{{ url_for('uploaded_file', filename=photo.filename) }}" alt="foto mezzo">
            </a>
          {% endfor %}
        </div>
      {% else %}
        <p class="muted">Nessuna foto caricata.</p>
      {% endif %}
    </div>
  </div>
</body>
</html>
"""


init_db()

if __name__ == "__main__":
    app.run(debug=True)
