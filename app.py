from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

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
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "presa_in_carico.db"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-questa-chiave-subito")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.config["APP_BASE_URL"] = os.environ.get("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
app.config["ADMIN_USERNAME"] = os.environ.get("ADMIN_USERNAME", "admin")
app.config["ADMIN_PASSWORD_HASH"] = os.environ.get(
    "ADMIN_PASSWORD_HASH",
    generate_password_hash("admin12345"),
)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: Exception | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            phone TEXT,
            email TEXT
        );

        CREATE TABLE IF NOT EXISTS vans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL,
            current_km INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Disponibile'
        );

        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL,
            van_id INTEGER NOT NULL,
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
            lights_ok INTEGER DEFAULT 0,
            is_closed INTEGER DEFAULT 0,
            FOREIGN KEY(driver_id) REFERENCES drivers(id),
            FOREIGN KEY(van_id) REFERENCES vans(id)
        );

        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id INTEGER NOT NULL,
            stage TEXT NOT NULL,
            filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY(assignment_id) REFERENCES assignments(id)
        );
        """
    )
    db.commit()

    if db.execute("SELECT COUNT(*) FROM drivers").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO drivers (full_name, phone, email) VALUES (?, ?, ?)",
            [
                ("Mario Rossi", "3331112233", "mario.rossi@example.com"),
                ("Luigi Bianchi", "3334445566", "luigi.bianchi@example.com"),
            ],
        )
    if db.execute("SELECT COUNT(*) FROM vans").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO vans (plate, model, current_km, status) VALUES (?, ?, ?, ?)",
            [
                ("AB123CD", "Fiat Ducato", 125000, "Disponibile"),
                ("EF456GH", "Ford Transit", 98000, "Disponibile"),
            ],
        )
    db.commit()
    db.close()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def save_uploaded_files(files: list[Any], assignment_id: int, stage: str) -> None:
    db = get_db()
    for file in files:
        if not file or not file.filename:
            continue
        safe_name = secure_filename(file.filename)
        final_name = f"{assignment_id}_{stage}_{secrets.token_hex(4)}_{safe_name}"
        file.save(UPLOAD_DIR / final_name)
        db.execute(
            "INSERT INTO photos (assignment_id, stage, filename, uploaded_at) VALUES (?, ?, ?, ?)",
            (assignment_id, stage, final_name, now_iso()),
        )
    db.commit()


def fetch_dashboard_data() -> dict[str, Any]:
    db = get_db()
    drivers = db.execute("SELECT * FROM drivers ORDER BY full_name").fetchall()
    vans = db.execute("SELECT * FROM vans ORDER BY plate").fetchall()
    assignments = db.execute(
        """
        SELECT a.*, d.full_name AS driver_name, v.plate, v.model
        FROM assignments a
        JOIN drivers d ON d.id = a.driver_id
        JOIN vans v ON v.id = a.van_id
        ORDER BY a.id DESC
        """
    ).fetchall()
    active_count = db.execute(
        "SELECT COUNT(*) FROM assignments WHERE status IN ('Assegnato', 'Preso in carico') AND is_closed = 0"
    ).fetchone()[0]
    completed_count = db.execute(
        "SELECT COUNT(*) FROM assignments WHERE status = 'Riconsegnato'"
    ).fetchone()[0]
    return {
        "drivers": drivers,
        "vans": vans,
        "assignments": assignments,
        "active_count": active_count,
        "completed_count": completed_count,
        "app_base_url": app.config["APP_BASE_URL"],
    }


@app.route("/setup")
def setup() -> str:
    init_db()
    return "Database inizializzato correttamente. Vai su /login"


@app.route("/login", methods=["GET", "POST"])
def login() -> str | Any:
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == app.config["ADMIN_USERNAME"] and check_password_hash(
            app.config["ADMIN_PASSWORD_HASH"], password
        ):
            session["admin_logged_in"] = True
            return redirect(url_for("index"))
        flash("Credenziali non valide.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout() -> Any:
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index() -> str:
    data = fetch_dashboard_data()
    return render_template("dashboard.html", **data)


@app.post("/drivers/create")
@login_required
def create_driver() -> Any:
    full_name = request.form.get("full_name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    if not full_name:
        flash("Il nome autista è obbligatorio.", "error")
        return redirect(url_for("index"))
    db = get_db()
    db.execute(
        "INSERT INTO drivers (full_name, phone, email) VALUES (?, ?, ?)",
        (full_name, phone, email),
    )
    db.commit()
    flash("Autista creato correttamente.", "success")
    return redirect(url_for("index"))


@app.post("/vans/create")
@login_required
def create_van() -> Any:
    plate = request.form.get("plate", "").strip().upper()
    model = request.form.get("model", "").strip()
    current_km = request.form.get("current_km", "0").strip()
    if not plate or not model:
        flash("Targa e modello sono obbligatori.", "error")
        return redirect(url_for("index"))
    db = get_db()
    try:
        db.execute(
            "INSERT INTO vans (plate, model, current_km, status) VALUES (?, ?, ?, ?)",
            (plate, model, int(current_km or 0), "Disponibile"),
        )
        db.commit()
        flash("Furgone creato correttamente.", "success")
    except sqlite3.IntegrityError:
        flash("La targa esiste già.", "error")
    return redirect(url_for("index"))


@app.post("/assignments/create")
@login_required
def create_assignment() -> Any:
    driver_id = request.form.get("driver_id")
    van_id = request.form.get("van_id")
    if not driver_id or not van_id:
        flash("Seleziona autista e furgone.", "error")
        return redirect(url_for("index"))
    token = secrets.token_urlsafe(16)
    db = get_db()
    db.execute(
        "INSERT INTO assignments (driver_id, van_id, token, created_at, status) VALUES (?, ?, ?, ?, ?)",
        (driver_id, van_id, token, now_iso(), "Assegnato"),
    )
    db.execute("UPDATE vans SET status = 'Assegnato' WHERE id = ?", (van_id,))
    db.commit()
    flash("Assegnazione creata correttamente.", "success")
    return redirect(url_for("index"))


@app.route("/driver/<token>", methods=["GET", "POST"])
def driver_portal(token: str) -> str | Any:
    db = get_db()
    assignment = db.execute(
        """
        SELECT a.*, d.full_name AS driver_name, v.plate, v.model, v.current_km
        FROM assignments a
        JOIN drivers d ON d.id = a.driver_id
        JOIN vans v ON v.id = a.van_id
        WHERE a.token = ?
        """,
        (token,),
    ).fetchone()
    if assignment is None:
        return "Link non valido.", 404

    photos = db.execute(
        "SELECT * FROM photos WHERE assignment_id = ? ORDER BY id DESC",
        (assignment["id"],),
    ).fetchall()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "pickup" and assignment["status"] == "Assegnato":
            db.execute(
                """
                UPDATE assignments
                SET status = 'Preso in carico',
                    pickup_at = ?,
                    pickup_km = ?,
                    pickup_fuel = ?,
                    pickup_notes = ?,
                    pickup_signature = ?,
                    body_ok = ?,
                    tyres_ok = ?,
                    docs_ok = ?,
                    lights_ok = ?
                WHERE id = ?
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
            db.execute(
                "UPDATE vans SET status = 'In uso', current_km = ? WHERE id = ?",
                (request.form.get("pickup_km") or assignment["current_km"], assignment["van_id"]),
            )
            db.commit()
            save_uploaded_files(request.files.getlist("pickup_photos"), assignment["id"], "pickup")
            flash("Presa in carico registrata.", "success")
            return redirect(url_for("driver_portal", token=token))

        if action == "return" and assignment["status"] in ("Preso in carico", "Assegnato"):
            db.execute(
                """
                UPDATE assignments
                SET status = 'Riconsegnato',
                    return_at = ?,
                    return_km = ?,
                    return_fuel = ?,
                    return_notes = ?,
                    return_signature = ?,
                    is_closed = 1
                WHERE id = ?
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
            db.execute(
                "UPDATE vans SET status = 'Disponibile', current_km = ? WHERE id = ?",
                (request.form.get("return_km") or assignment["current_km"], assignment["van_id"]),
            )
            db.commit()
            save_uploaded_files(request.files.getlist("return_photos"), assignment["id"], "return")
            flash("Riconsegna registrata.", "success")
            return redirect(url_for("driver_portal", token=token))

    return render_template("driver.html", assignment=assignment, photos=photos)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str) -> Any:
    return send_from_directory(UPLOAD_DIR, filename)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
