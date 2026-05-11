from __future__ import annotations

import io
import os
import secrets
import urllib.request
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import cloudinary
import cloudinary.uploader
import psycopg
from PIL import Image, ImageOps
from psycopg.rows import dict_row
from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# =========================
# CONFIG
# =========================

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL non configurata")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-questa-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

cloudinary.config(secure=True)

DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin12345")

DEFAULT_APPALTI = [
    "AmazonDPI3",
    "AmazonDLO7",
    "TORTONA TABACCHI",
    "VIAREGGIO TABACCHI",
    "SAN MAURO TABACCHI",
    "GENOVA TABACCHI",
    "Sapiopiacenza",
]

PHOTO_LABELS = {
    "pickup_front": "Presa in carico - Anteriore",
    "pickup_rear": "Presa in carico - Posteriore",
    "pickup_right": "Presa in carico - Lato destro",
    "pickup_left": "Presa in carico - Lato sinistro",
    "pickup_inside": "Presa in carico - Interno",
    "return_front": "Riconsegna - Anteriore",
    "return_rear": "Riconsegna - Posteriore",
    "return_right": "Riconsegna - Lato destro",
    "return_left": "Riconsegna - Lato sinistro",
    "return_inside": "Riconsegna - Interno",
}

MAX_IMAGE_SIZE = (1600, 1600)
JPEG_QUALITY = 72


# =========================
# HELPERS
# =========================

def now_dt() -> datetime:
    return datetime.now(ZoneInfo("Europe/Rome"))


def now_iso() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def today_start_iso() -> str:
    return now_dt().strftime("%Y-%m-%d") + " 00:00:00"


def format_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(value)


def only_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return str(value)


def slugify_username(text: str) -> str:
    cleaned = (
        text.lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
        .replace(".", "_")
    )
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def get_db():
    if "db" not in g:
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def current_appalto_id() -> int | None:
    return session.get("appalto_id")


def current_appalto_nome() -> str | None:
    return session.get("appalto_nome")


def optimize_image(file_obj) -> io.BytesIO:
    img = Image.open(file_obj.stream)
    img = ImageOps.exif_transpose(img)

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")

    img.thumbnail(MAX_IMAGE_SIZE)

    output = io.BytesIO()
    img.save(
        output,
        format="JPEG",
        quality=JPEG_QUALITY,
        optimize=True,
    )
    output.seek(0)
    return output


def save_single_photo(file_obj, assignment_id: int, stage: str) -> None:
    if not file_obj or not file_obj.filename:
        return

    db = get_db()
    safe_name = secure_filename(file_obj.filename)
    base_name = Path(safe_name).stem

    optimized_file = optimize_image(file_obj)

    result = cloudinary.uploader.upload(
        optimized_file,
        folder="furgoni_app",
        public_id=f"{assignment_id}_{stage}_{secrets.token_hex(4)}_{base_name}",
        resource_type="image",
        format="jpg",
    )

    image_url = result.get("secure_url")
    if not image_url:
        return

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO photos (assignment_id, stage, filename, uploaded_at)
            VALUES (%s, %s, %s, %s)
            """,
            (assignment_id, stage, image_url, now_iso()),
        )
    db.commit()


def get_assignment_photos(assignment_id: int):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM photos WHERE assignment_id = %s ORDER BY id ASC",
            (assignment_id,),
        )
        rows = cur.fetchall()

    photos_by_stage = {key: None for key in PHOTO_LABELS.keys()}
    for row in rows:
        photos_by_stage[row["stage"]] = row

    return rows, photos_by_stage


def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped_view


def release_stale_vans(appalto_id: int | None) -> None:
    if not appalto_id:
        return

    db = get_db()
    stale_cutoff = today_start_iso()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT van_id
            FROM assignments
            WHERE appalto_id = %s
              AND status IN ('Assegnato', 'Preso in carico')
              AND created_at < %s
            """,
            (appalto_id, stale_cutoff),
        )
        stale_vans = [row["van_id"] for row in cur.fetchall()]

        cur.execute(
            """
            UPDATE assignments
            SET status = 'Scaduto non riconsegnato'
            WHERE appalto_id = %s
              AND status IN ('Assegnato', 'Preso in carico')
              AND created_at < %s
            """,
            (appalto_id, stale_cutoff),
        )

        if stale_vans:
            cur.execute(
                """
                UPDATE vans
                SET status = 'Disponibile'
                WHERE appalto_id = %s
                  AND id = ANY(%s)
                """,
                (appalto_id, stale_vans),
            )

    db.commit()


# =========================
# DB INIT
# =========================

def init_db() -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as db:
        with db.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS appalti (
                    id SERIAL PRIMARY KEY,
                    nome TEXT NOT NULL UNIQUE
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    appalto_id INTEGER REFERENCES appalti(id),
                    created_at TEXT NOT NULL
                );
            """)

            cur.execute("""
                ALTER TABLE admin_users
                ADD COLUMN IF NOT EXISTS appalto_id INTEGER;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS drivers (
                    id SERIAL PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    phone TEXT,
                    email TEXT,
                    pin TEXT,
                    appalto_id INTEGER REFERENCES appalti(id)
                );
            """)

            cur.execute("""
                ALTER TABLE drivers
                ADD COLUMN IF NOT EXISTS pin TEXT;
            """)
            cur.execute("""
                ALTER TABLE drivers
                ADD COLUMN IF NOT EXISTS appalto_id INTEGER;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS vans (
                    id SERIAL PRIMARY KEY,
                    plate TEXT NOT NULL,
                    model TEXT NOT NULL,
                    current_km INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'Disponibile',
                    appalto_id INTEGER REFERENCES appalti(id)
                );
            """)

            cur.execute("""
                ALTER TABLE vans
                ADD COLUMN IF NOT EXISTS appalto_id INTEGER;
            """)

            # Se esiste il vecchio vincolo globale sulla targa, lo togliamo
            cur.execute("""
                ALTER TABLE vans
                DROP CONSTRAINT IF EXISTS vans_plate_key;
            """)

            # Vincolo corretto: targa unica solo dentro lo stesso appalto
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_vans_plate_appalto_unique
                ON vans (plate, appalto_id);
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
                    lights_ok INTEGER DEFAULT 0,
                    appalto_id INTEGER REFERENCES appalti(id)
                );
            """)

            cur.execute("""
                ALTER TABLE assignments
                ADD COLUMN IF NOT EXISTS appalto_id INTEGER;
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

            for nome in DEFAULT_APPALTI:
                cur.execute(
                    "INSERT INTO appalti (nome) VALUES (%s) ON CONFLICT (nome) DO NOTHING",
                    (nome,),
                )

            cur.execute("SELECT id, nome FROM appalti ORDER BY nome")
            appalti = cur.fetchall()

            password_hash = generate_password_hash(DEFAULT_ADMIN_PASSWORD)

            for appalto in appalti:
                username = f"admin_{slugify_username(appalto['nome'])}"
                cur.execute(
                    """
                    INSERT INTO admin_users (username, password_hash, appalto_id, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (username) DO NOTHING
                    """,
                    (username, password_hash, appalto["id"], now_iso()),
                )

        db.commit()


# =========================
# DASHBOARD DATA
# =========================

def fetch_dashboard_data() -> dict[str, Any]:
    db = get_db()
    appalto_id = current_appalto_id()

    release_stale_vans(appalto_id)

    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM drivers WHERE appalto_id = %s ORDER BY full_name",
            (appalto_id,),
        )
        drivers = cur.fetchall()

        cur.execute(
            "SELECT * FROM vans WHERE appalto_id = %s ORDER BY plate",
            (appalto_id,),
        )
        vans = cur.fetchall()

        cur.execute("""
            SELECT
                a.*,
                d.full_name AS driver_name,
                d.pin AS driver_pin,
                v.plate,
                v.model,
                (
                    SELECT COUNT(*)
                    FROM photos p
                    WHERE p.assignment_id = a.id
                ) AS photo_count,
                (
                    SELECT p.filename
                    FROM photos p
                    WHERE p.assignment_id = a.id
                    ORDER BY p.id ASC
                    LIMIT 1
                ) AS first_photo
            FROM assignments a
            JOIN drivers d ON d.id = a.driver_id
            JOIN vans v ON v.id = a.van_id
            WHERE a.appalto_id = %s
            ORDER BY a.created_at DESC, a.id DESC
        """, (appalto_id,))
        assignments = cur.fetchall()

        cur.execute("""
            SELECT COUNT(*) AS count
            FROM assignments
            WHERE appalto_id = %s
            AND status IN ('Assegnato', 'Preso in carico')
        """, (appalto_id,))
        active_count = cur.fetchone()["count"]

        cur.execute("""
            SELECT COUNT(*) AS count
            FROM assignments
            WHERE appalto_id = %s
            AND status = 'Riconsegnato'
        """, (appalto_id,))
        completed_count = cur.fetchone()["count"]

    grouped_assignments = {}
    daily_counts = {}

    for a in assignments:
        day_key = only_date(a["created_at"])
        if day_key not in grouped_assignments:
            grouped_assignments[day_key] = []
        grouped_assignments[day_key].append(a)

    for day_key, items in grouped_assignments.items():
        unique_plates = {item["plate"] for item in items}
        daily_counts[day_key] = len(unique_plates)

    return {
        "drivers": drivers,
        "vans": vans,
        "assignments": assignments,
        "grouped_assignments": grouped_assignments,
        "daily_counts": daily_counts,
        "active_count": active_count,
        "completed_count": completed_count,
        "appalto_nome": current_appalto_nome(),
    }


# =========================
# AUTH
# =========================

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
            cur.execute("""
                SELECT au.*, a.nome AS appalto_nome
                FROM admin_users au
                LEFT JOIN appalti a ON a.id = au.appalto_id
                WHERE au.username = %s
            """, (username,))
            user = cur.fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["admin_logged_in"] = True
            session["admin_username"] = username
            session["appalto_id"] = user["appalto_id"]
            session["appalto_nome"] = user["appalto_nome"]
            flash("Login effettuato correttamente.", "success")
            return redirect(url_for("dashboard"))

        flash("Credenziali non valide.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logout eseguito.", "success")
    return redirect(url_for("login"))


# =========================
# ADMIN ROUTES
# =========================

@app.route("/dashboard")
@admin_required
def dashboard():
    data = fetch_dashboard_data()
    return render_template("dashboard.html", **data)


@app.route("/admin/manage", methods=["GET", "POST"])
@admin_required
def manage_admin():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "create":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            appalto_id = request.form.get("appalto_id", "").strip()

            if not username or not password or not appalto_id:
                flash("Username, password e appalto sono obbligatori.", "error")
                return redirect(url_for("manage_admin"))

            try:
                password_hash = generate_password_hash(password)

                with db.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO admin_users (username, password_hash, appalto_id, created_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (username, password_hash, int(appalto_id), now_iso()),
                    )
                db.commit()
                flash("Admin creato correttamente.", "success")
            except psycopg.errors.UniqueViolation:
                db.rollback()
                flash("Username già esistente.", "error")
            except Exception:
                db.rollback()
                flash("Errore durante la creazione dell'admin.", "error")

            return redirect(url_for("manage_admin"))

        if action == "change_password":
            user_id = request.form.get("user_id", "").strip()
            new_password = request.form.get("new_password", "").strip()

            if not user_id or not new_password:
                flash("Nuova password mancante.", "error")
                return redirect(url_for("manage_admin"))

            try:
                password_hash = generate_password_hash(new_password)

                with db.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE admin_users
                        SET password_hash = %s
                        WHERE id = %s
                        """,
                        (password_hash, int(user_id)),
                    )
                db.commit()
                flash("Password aggiornata correttamente.", "success")
            except Exception:
                db.rollback()
                flash("Errore durante l'aggiornamento della password.", "error")

            return redirect(url_for("manage_admin"))

        if action == "delete":
            user_id = request.form.get("user_id", "").strip()

            if not user_id:
                flash("Admin non valido.", "error")
                return redirect(url_for("manage_admin"))

            try:
                with db.cursor() as cur:
                    cur.execute("DELETE FROM admin_users WHERE id = %s", (int(user_id),))
                db.commit()
                flash("Admin eliminato correttamente.", "success")
            except Exception:
                db.rollback()
                flash("Errore durante l'eliminazione dell'admin.", "error")

            return redirect(url_for("manage_admin"))

    with db.cursor() as cur:
        cur.execute("""
            SELECT
                au.id,
                au.username,
                au.appalto_id,
                a.nome AS appalto_nome
            FROM admin_users au
            LEFT JOIN appalti a ON a.id = au.appalto_id
            ORDER BY au.username
        """)
        admins = cur.fetchall()

        cur.execute("""
            SELECT id, nome
            FROM appalti
            ORDER BY nome
        """)
        appalti = cur.fetchall()

    return render_template(
        "manage_admin.html",
        admins=admins,
        appalti=appalti,
        appalto_nome=current_appalto_nome(),
    )


@app.post("/drivers/create")
@admin_required
def create_driver():
    full_name = request.form.get("full_name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    pin = request.form.get("pin", "").strip()
    appalto_id = current_appalto_id()

    if not full_name or not pin:
        flash("Nome e PIN sono obbligatori.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO drivers (full_name, phone, email, pin, appalto_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (full_name, phone, email, pin, appalto_id),
        )
    db.commit()

    flash("Autista creato correttamente.", "success")
    return redirect(url_for("dashboard"))


@app.post("/drivers/delete/<int:driver_id>")
@admin_required
def delete_driver(driver_id: int):
    db = get_db()
    appalto_id = current_appalto_id()
    try:
        with db.cursor() as cur:
            cur.execute(
                "DELETE FROM drivers WHERE id = %s AND appalto_id = %s",
                (driver_id, appalto_id),
            )
        db.commit()
        flash("Autista eliminato correttamente.", "success")
    except Exception:
        db.rollback()
        flash("Impossibile eliminare l'autista. Potrebbe avere pratiche collegate.", "error")
    return redirect(url_for("dashboard"))


@app.post("/vans/create")
@admin_required
def create_van():
    plate = request.form.get("plate", "").strip().upper()
    model = request.form.get("model", "").strip()
    current_km = request.form.get("current_km", "0").strip()
    appalto_id = current_appalto_id()

    if not plate or not model:
        flash("Targa e modello sono obbligatori.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vans (plate, model, current_km, status, appalto_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (plate, model, int(current_km or 0), "Disponibile", appalto_id),
            )
        db.commit()
        flash("Furgone creato correttamente.", "success")
    except psycopg.errors.UniqueViolation:
        db.rollback()
        flash("Questa targa esiste già nello stesso appalto.", "error")

    return redirect(url_for("dashboard"))


@app.post("/vans/delete/<int:van_id>")
@admin_required
def delete_van(van_id: int):
    db = get_db()
    appalto_id = current_appalto_id()
    try:
        with db.cursor() as cur:
            cur.execute(
                "DELETE FROM vans WHERE id = %s AND appalto_id = %s",
                (van_id, appalto_id),
            )
        db.commit()
        flash("Furgone eliminato correttamente.", "success")
    except Exception:
        db.rollback()
        flash("Impossibile eliminare il furgone. Potrebbe avere pratiche collegate.", "error")
    return redirect(url_for("dashboard"))


@app.post("/vans/release/<int:van_id>")
@admin_required
def release_van(van_id: int):
    db = get_db()
    appalto_id = current_appalto_id()

    try:
        with db.cursor() as cur:
            cur.execute(
                """
                UPDATE assignments
                SET status = 'Chiuso manualmente',
                    return_at = COALESCE(return_at, %s),
                    return_signature = COALESCE(return_signature, 'CHIUSURA ADMIN')
                WHERE appalto_id = %s
                  AND van_id = %s
                  AND status IN ('Assegnato', 'Preso in carico')
                """,
                (now_iso(), appalto_id, van_id),
            )

            cur.execute(
                """
                UPDATE vans
                SET status = 'Disponibile'
                WHERE id = %s
                  AND appalto_id = %s
                """,
                (van_id, appalto_id),
            )

        db.commit()
        flash("Furgone reso disponibile correttamente.", "success")
    except Exception:
        db.rollback()
        flash("Errore nello sblocco del furgone.", "error")

    return redirect(url_for("dashboard"))


# =========================
# DRIVER ROUTES
# =========================

@app.route("/driver", methods=["GET", "POST"])
def driver_select():
    db = get_db()
    driver = None
    available_vans = []
    assignments = []

    if request.method == "POST":
        action = request.form.get("action")
        pin = request.form.get("pin", "").strip()

        with db.cursor() as cur:
            cur.execute("SELECT * FROM drivers WHERE pin = %s", (pin,))
            driver = cur.fetchone()

            if not driver:
                flash("PIN non valido.", "error")
                return render_template(
                    "driver_select.html",
                    driver=None,
                    assignments=[],
                    available_vans=[],
                )

            release_stale_vans(driver["appalto_id"])

            if action == "select_van":
                van_id = request.form.get("van_id")

                if van_id:
                    cur.execute(
                        """
                        SELECT *
                        FROM vans
                        WHERE id = %s
                        AND appalto_id = %s
                        """,
                        (van_id, driver["appalto_id"]),
                    )
                    van = cur.fetchone()

                    if not van or van["status"] != "Disponibile":
                        flash("Furgone non disponibile.", "error")
                    else:
                        token = secrets.token_urlsafe(16)

                        cur.execute(
                            """
                            INSERT INTO assignments (driver_id, van_id, token, created_at, status, appalto_id)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            RETURNING token
                            """,
                            (
                                driver["id"],
                                van_id,
                                token,
                                now_iso(),
                                "Assegnato",
                                driver["appalto_id"],
                            ),
                        )
                        new_assignment = cur.fetchone()

                        cur.execute(
                            "UPDATE vans SET status = 'Assegnato' WHERE id = %s",
                            (van_id,),
                        )

                        db.commit()
                        return redirect(url_for("driver_portal", token=new_assignment["token"]))

            cur.execute(
                """
                SELECT *
                FROM vans
                WHERE status = 'Disponibile'
                AND appalto_id = %s
                ORDER BY plate
                """,
                (driver["appalto_id"],),
            )
            available_vans = cur.fetchall()

            cur.execute(
                """
                SELECT
                    a.id,
                    a.token,
                    a.status,
                    v.plate,
                    v.model
                FROM assignments a
                JOIN vans v ON v.id = a.van_id
                WHERE a.driver_id = %s
                AND a.appalto_id = %s
                AND a.status != 'Riconsegnato'
                ORDER BY a.id DESC
                """,
                (driver["id"], driver["appalto_id"]),
            )
            assignments = cur.fetchall()

    return render_template(
        "driver_select.html",
        driver=driver,
        assignments=assignments,
        available_vans=available_vans,
    )


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

    _, photos_by_stage = get_assignment_photos(assignment["id"])

    if request.method == "POST":
        action = request.form.get("action")

        # =========================
        # PRESA IN CARICO
        # =========================
        if action == "pickup":

            required_pickup = [
                "pickup_front",
                "pickup_rear",
                "pickup_inside",
            ]

            missing_files = []

            for field_name in required_pickup:
                already_present = photos_by_stage.get(field_name) is not None
                new_file = request.files.get(field_name)

                if not already_present and (not new_file or not new_file.filename):
                    missing_files.append(PHOTO_LABELS[field_name])

            if missing_files:
                flash(
                    "Mancano foto obbligatorie: " + ", ".join(missing_files),
                    "error",
                )

                all_photos, photos_by_stage = get_assignment_photos(
                    assignment["id"]
                )

                return render_template(
                    "driver.html",
                    assignment=assignment,
                    photos=all_photos,
                    photos_by_stage=photos_by_stage,
                    photo_labels=PHOTO_LABELS,
                )

            # SALVA DATI PRIMA
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
                        assignment["driver_name"],
                        1 if request.form.get("body_ok") else 0,
                        1 if request.form.get("tyres_ok") else 0,
                        1 if request.form.get("docs_ok") else 0,
                        1 if request.form.get("lights_ok") else 0,
                        assignment["id"],
                    ),
                )

                cur.execute(
                    """
                    UPDATE vans
                    SET status = 'In uso',
                        current_km = %s
                    WHERE id = %s
                    """,
                    (
                        request.form.get("pickup_km")
                        or assignment["current_km"],
                        assignment["van_id"],
                    ),
                )

            db.commit()

            # FOTO DOPO (NON BLOCCANTI)
            pickup_fields = [
                "pickup_front",
                "pickup_rear",
                "pickup_right",
                "pickup_left",
                "pickup_inside",
            ]

            failed_photos = []

            for field_name in pickup_fields:
                try:
                    save_single_photo(
                        request.files.get(field_name),
                        assignment["id"],
                        field_name,
                    )
                except Exception:
                    failed_photos.append(PHOTO_LABELS[field_name])

            if failed_photos:
                flash(
                    "Presa in carico salvata, ma alcune foto non sono state caricate: "
                    + ", ".join(failed_photos),
                    "error",
                )
            else:
                flash("Presa in carico registrata.", "success")

            return redirect(url_for("driver_portal", token=token))

        # =========================
        # RICONSEGNA
        # =========================
        if action == "return":

            # SALVA DATI PRIMA
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
                        assignment["driver_name"],
                        assignment["id"],
                    ),
                )

                cur.execute(
                    """
                    UPDATE vans
                    SET status = 'Disponibile',
                        current_km = %s
                    WHERE id = %s
                    """,
                    (
                        request.form.get("return_km")
                        or assignment["current_km"],
                        assignment["van_id"],
                    ),
                )

            db.commit()

            # FOTO DOPO (FACOLTATIVE)
            return_fields = [
                "return_front",
                "return_rear",
                "return_right",
                "return_left",
                "return_inside",
            ]

            failed_photos = []

            for field_name in return_fields:
                try:
                    save_single_photo(
                        request.files.get(field_name),
                        assignment["id"],
                        field_name,
                    )
                except Exception:
                    failed_photos.append(PHOTO_LABELS[field_name])

            if failed_photos:
                flash(
                    "Riconsegna salvata, ma alcune foto non sono state caricate: "
                    + ", ".join(failed_photos),
                    "error",
                )
            else:
                flash("Riconsegna registrata.", "success")

            return redirect(url_for("driver_portal", token=token))

    all_photos, photos_by_stage = get_assignment_photos(assignment["id"])

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

    return render_template(
        "driver.html",
        assignment=assignment,
        photos=all_photos,
        photos_by_stage=photos_by_stage,
        photo_labels=PHOTO_LABELS,
    )

# =========================
# PDF
# =========================

@app.route("/pdf/<int:assignment_id>")
@admin_required
def genera_pdf(assignment_id: int):
    db = get_db()
    appalto_id = current_appalto_id()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                a.*,
                d.full_name AS driver_name,
                d.phone AS driver_phone,
                d.email AS driver_email,
                v.plate,
                v.model
            FROM assignments a
            JOIN drivers d ON d.id = a.driver_id
            JOIN vans v ON v.id = a.van_id
            WHERE a.id = %s
            AND a.appalto_id = %s
            """,
            (assignment_id, appalto_id),
        )
        assignment = cur.fetchone()

        cur.execute(
            """
            SELECT * FROM photos
            WHERE assignment_id = %s
            ORDER BY id ASC
            """,
            (assignment_id,),
        )
        photos = cur.fetchall()

    if not assignment:
        flash("Pratica non trovata.", "error")
        return redirect(url_for("dashboard"))

    photos_by_stage = {key: None for key in PHOTO_LABELS.keys()}

    for photo in photos:
        photos_by_stage[photo["stage"]] = photo

    buffer = io.BytesIO()

    pdf = canvas.Canvas(buffer, pagesize=A4)

    page_width, page_height = A4

    margin = 40

    def draw_title(text, y):
        pdf.setFont("Helvetica-Bold", 20)
        pdf.drawString(margin, y, text)

    def draw_section(title, y):
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(margin, y, title)

    def draw_text(label, value, y):
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(margin, y, f"{label}:")

        pdf.setFont("Helvetica", 11)
        pdf.drawString(margin + 170, y, str(value or "-"))

    def draw_photo(photo_key, x, y, width=220, height=150):
        photo = photos_by_stage.get(photo_key)

        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(x, y + height + 10, PHOTO_LABELS[photo_key])

        if not photo:
            pdf.setFont("Helvetica", 10)
            pdf.drawString(x, y + height - 20, "Foto non presente")
            return

        try:
            with urllib.request.urlopen(photo["filename"]) as response:
                image_bytes = response.read()

            img = ImageReader(io.BytesIO(image_bytes))

            pdf.drawImage(
                img,
                x,
                y,
                width=width,
                height=height,
                preserveAspectRatio=True,
                mask="auto",
            )

        except Exception:
            pdf.setFont("Helvetica", 10)
            pdf.drawString(x, y + height - 20, "Errore caricamento immagine")

    # =========================================
    # PAGINA 1 - RIEPILOGO
    # =========================================

    y = page_height - 50

    draw_title("REPORT PRESA IN CARICO FURGONE", y)

    y -= 40

    draw_text("Appalto", current_appalto_nome(), y)
    y -= 25

    draw_text("Generato il", now_dt().strftime("%d/%m/%Y %H:%M"), y)
    y -= 25

    draw_text("Data pratica", only_date(assignment["created_at"]), y)
    y -= 25

    draw_text("Autista", assignment["driver_name"], y)
    y -= 25

    draw_text("Telefono", assignment.get("driver_phone") or "-", y)
    y -= 25

    draw_text("Email", assignment.get("driver_email") or "-", y)
    y -= 25

    draw_text(
        "Mezzo",
        f"{assignment['plate']} - {assignment['model']}",
        y,
    )

    y -= 45

    draw_section("PRESA IN CARICO", y)

    y -= 30

    draw_text("Data e ora", format_date(assignment.get("pickup_at")), y)
    y -= 25

    draw_text("KM", assignment.get("pickup_km"), y)
    y -= 25

    draw_text("Carburante", assignment.get("pickup_fuel"), y)
    y -= 25

    draw_text("Firma", assignment.get("pickup_signature"), y)
    y -= 25

    draw_text(
        "Check mezzo",
        f"Carrozzeria: {'OK' if assignment.get('body_ok') else 'NO'} | "
        f"Gomme: {'OK' if assignment.get('tyres_ok') else 'NO'} | "
        f"Documenti: {'OK' if assignment.get('docs_ok') else 'NO'} | "
        f"Luci: {'OK' if assignment.get('lights_ok') else 'NO'}",
        y,
    )

    y -= 40

    draw_section("RICONSEGNA", y)

    y -= 30

    draw_text("Data e ora", format_date(assignment.get("return_at")), y)
    y -= 25

    draw_text("KM", assignment.get("return_km"), y)
    y -= 25

    draw_text("Carburante", assignment.get("return_fuel"), y)
    y -= 25

    draw_text("Firma", assignment.get("return_signature"), y)

    y -= 40

    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin, y, "Note presa in carico:")

    y -= 20

    pdf.setFont("Helvetica", 11)

    pickup_notes = assignment.get("pickup_notes") or "Nessuna"

    for line in pickup_notes.splitlines():
        pdf.drawString(margin + 15, y, f"- {line}")
        y -= 18

    y -= 20

    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin, y, "Note riconsegna:")

    y -= 20

    pdf.setFont("Helvetica", 11)

    return_notes = assignment.get("return_notes") or "Nessuna"

    for line in return_notes.splitlines():
        pdf.drawString(margin + 15, y, f"- {line}")
        y -= 18

    # =========================================
    # PAGINA 2 - FOTO PRESA IN CARICO
    # =========================================

    pdf.showPage()

    draw_title("FOTO PRESA IN CARICO", page_height - 50)

    draw_photo("pickup_front", 40, 560)
    draw_photo("pickup_rear", 300, 560)

    draw_photo("pickup_right", 40, 340)
    draw_photo("pickup_left", 300, 340)

    draw_photo("pickup_inside", 170, 120)

    # =========================================
    # PAGINA 3 - FOTO RICONSEGNA
    # =========================================

    pdf.showPage()

    draw_title("FOTO RICONSEGNA", page_height - 50)

    draw_photo("return_front", 40, 560)
    draw_photo("return_rear", 300, 560)

    draw_photo("return_right", 40, 340)
    draw_photo("return_left", 300, 340)

    draw_photo("return_inside", 170, 120)

    pdf.save()

    buffer.seek(0)

    filename = (
        f"report_"
        f"{assignment['driver_name'].replace(' ', '_')}_"
        f"{assignment['plate']}_"
        f"{assignment_id}.pdf"
    )

    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf",
    )


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return redirect(filename)


init_db()

if __name__ == "__main__":
    app.run(debug=True) 
