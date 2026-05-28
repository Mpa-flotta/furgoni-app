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
from supabase import create_client

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
    send_from_directory,
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

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "furgoni-foto")

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)


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
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(value)


def only_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
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
    base_name = Path(safe_name).stem or "foto"

    optimized_file = optimize_image(file_obj)

    filename = (
        f"{assignment_id}_"
        f"{stage}_"
        f"{secrets.token_hex(4)}_"
        f"{base_name}.jpg"
    )

    file_bytes = optimized_file.read()

    supabase.storage.from_(SUPABASE_BUCKET).upload(
        path=filename,
        file=file_bytes,
        file_options={
            "content-type": "image/jpeg"
        }
    )

    image_url = (
        f"{SUPABASE_URL}/storage/v1/object/public/"
        f"{SUPABASE_BUCKET}/{filename}"
    )

    with db.cursor() as cur:

        cur.execute(
            """
            INSERT INTO photos (
                assignment_id,
                stage,
                filename,
                uploaded_at
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                assignment_id,
                stage,
                image_url,
                now_iso(),
            ),
        )

    db.commit()
    

def get_assignment_photos(assignment_id: int):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM photos
            WHERE assignment_id = %s
            ORDER BY id ASC
            """,
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

            cur.execute("""
                ALTER TABLE vans
                DROP CONSTRAINT IF EXISTS vans_plate_key;
            """)

            cur.execute("""
                DROP INDEX IF EXISTS idx_vans_plate_appalto_unique;
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
                    """
                    INSERT INTO appalti (nome)
                    VALUES (%s)
                    ON CONFLICT (nome) DO NOTHING
                    """,
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
            """
            SELECT *
            FROM drivers
            WHERE appalto_id = %s
            ORDER BY full_name
            """,
            (appalto_id,),
        )
        drivers = cur.fetchall()

        cur.execute(
            """
            SELECT *
            FROM vans
            WHERE appalto_id = %s
            ORDER BY plate
            """,
            (appalto_id,),
        )
        vans = cur.fetchall()

        cur.execute(
            """
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
            """,
            (appalto_id,),
        )
        assignments = cur.fetchall()

        assignment_ids = [a["id"] for a in assignments]

        photos_by_assignment = {}

        if assignment_ids:
            cur.execute(
                """
                SELECT *
                FROM photos
                WHERE assignment_id = ANY(%s)
                ORDER BY assignment_id, id ASC
                """,
                (assignment_ids,),
            )

            all_photos = cur.fetchall()

            for photo in all_photos:
                photos_by_assignment.setdefault(
                    photo["assignment_id"],
                    []
                ).append(photo)

        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM assignments
            WHERE appalto_id = %s
              AND status IN ('Assegnato', 'Preso in carico')
            """,
            (appalto_id,),
        )
        active_count = cur.fetchone()["count"]

        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM assignments
            WHERE appalto_id = %s
              AND status = 'Riconsegnato'
            """,
            (appalto_id,),
        )
        completed_count = cur.fetchone()["count"]

    grouped_assignments = {}
    daily_counts = {}

    for a in assignments:
        day_key = only_date(a["created_at"])
        grouped_assignments.setdefault(day_key, []).append(a)

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
        "photos_by_assignment": photos_by_assignment,
        "photo_labels": PHOTO_LABELS,
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
            cur.execute(
                """
                SELECT au.*, a.nome AS appalto_nome
                FROM admin_users au
                LEFT JOIN appalti a ON a.id = au.appalto_id
                WHERE au.username = %s
                """,
                (username,),
            )
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

    sort_mode = request.args.get("sort", "date")
    filter_date_raw = request.args.get("filter_date", "").strip()

    if sort_mode == "alpha":
        for day in data["grouped_assignments"]:
            data["grouped_assignments"][day] = sorted(
                data["grouped_assignments"][day],
                key=lambda x: x["driver_name"].split()[-1].lower()
            )

    if filter_date_raw:
        try:
            dt = datetime.strptime(filter_date_raw, "%Y-%m-%d")
            filter_date = dt.strftime("%d/%m/%Y")

            data["grouped_assignments"] = {
                day: items
                for day, items in data["grouped_assignments"].items()
                if day == filter_date
            }
        except ValueError:
            pass

    data["sort_mode"] = sort_mode
    data["filter_date_raw"] = filter_date_raw

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
                    cur.execute(
                        """
                        DELETE FROM admin_users
                        WHERE id = %s
                        """,
                        (int(user_id),),
                    )
                db.commit()
                flash("Admin eliminato correttamente.", "success")
            except Exception:
                db.rollback()
                flash("Errore durante l'eliminazione dell'admin.", "error")

            return redirect(url_for("manage_admin"))

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                au.id,
                au.username,
                au.appalto_id,
                a.nome AS appalto_nome
            FROM admin_users au
            LEFT JOIN appalti a ON a.id = au.appalto_id
            ORDER BY au.username
            """
        )
        admins = cur.fetchall()

        cur.execute(
            """
            SELECT id, nome
            FROM appalti
            ORDER BY nome
            """
        )
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
                """
                DELETE FROM drivers
                WHERE id = %s
                  AND appalto_id = %s
                """,
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
    except Exception:
        db.rollback()
        flash("Errore durante la creazione del furgone.", "error")

    return redirect(url_for("dashboard"))


@app.post("/vans/delete/<int:van_id>")
@admin_required
def delete_van(van_id: int):
    db = get_db()
    appalto_id = current_appalto_id()

    try:
        with db.cursor() as cur:
            cur.execute(
                """
                DELETE FROM vans
                WHERE id = %s
                  AND appalto_id = %s
                """,
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
            cur.execute(
                "SELECT * FROM drivers WHERE pin = %s",
                (pin,),
            )
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
                            INSERT INTO assignments (
                                driver_id,
                                van_id,
                                token,
                                created_at,
                                status,
                                appalto_id
                            )
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
                            """
                            UPDATE vans
                            SET status = 'Assegnato'
                            WHERE id = %s
                            """,
                            (van_id,),
                        )

                        db.commit()

                        return redirect(
                            url_for(
                                "driver_portal",
                                token=new_assignment["token"],
                            )
                        )

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


@app.post("/driver/change-pin")
def driver_change_pin():
    current_pin = request.form.get("current_pin", "").strip()
    new_pin = request.form.get("new_pin", "").strip()
    confirm_pin = request.form.get("confirm_pin", "").strip()

    if not current_pin or not new_pin or not confirm_pin:
        flash("Compila tutti i campi per cambiare PIN.", "error")
        return redirect(url_for("driver_select"))

    if len(new_pin) < 4:
        flash("Il nuovo PIN deve avere almeno 4 caratteri.", "error")
        return redirect(url_for("driver_select"))

    if new_pin != confirm_pin:
        flash("Il nuovo PIN e la conferma non coincidono.", "error")
        return redirect(url_for("driver_select"))

    db = get_db()

    with db.cursor() as cur:

        cur.execute(
            """
            SELECT *
            FROM drivers
            WHERE pin = %s
            """,
            (current_pin,),
        )

        driver = cur.fetchone()

        if not driver:
            flash("PIN attuale non valido.", "error")
            return redirect(url_for("driver_select"))

        cur.execute(
        """
       SELECT id
       FROM drivers
       WHERE pin = %s
       AND id != %s
       """,
       (new_pin, driver["id"]),
       )

        existing = cur.fetchone()

        if existing:
            flash(
                "Questo PIN è già usato da un altro autista.",
                "error",
            )
            return redirect(url_for("driver_select"))

        cur.execute(
            """
            UPDATE drivers
            SET pin = %s
            WHERE id = %s
            """,
            (new_pin, driver["id"]),
        )

    db.commit()

    flash("PIN aggiornato correttamente.", "success")

    return redirect(url_for("driver_select"))


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

                if not already_present and (
                    not new_file or not new_file.filename
                ):
                    missing_files.append(PHOTO_LABELS[field_name])

            if missing_files:
                flash(
                    "Mancano foto obbligatorie: "
                    + ", ".join(missing_files),
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

            return redirect(
                url_for(
                    "driver_portal",
                    token=token,
                )
            )

        # =========================
        # RICONSEGNA
        # =========================

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

            return redirect(
                url_for(
                    "driver_portal",
                    token=token,
                )
            )

    all_photos, photos_by_stage = get_assignment_photos(
        assignment["id"]
    )

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
            SELECT *
            FROM photos
            WHERE assignment_id = %s
            ORDER BY id ASC
            """,
            (assignment_id,),
        )

        photos = cur.fetchall()

    if not assignment:
        flash("Pratica non trovata.", "error")
        return redirect(url_for("dashboard"))

    photos_by_stage = {}

    for photo in photos:
        photos_by_stage[photo["stage"]] = photo

    buffer = io.BytesIO()

    pdf = canvas.Canvas(buffer, pagesize=A4)

    page_width, page_height = A4

    margin = 40
    y = page_height - 40

    # =========================
    # HELPERS
    # =========================

    def safe(value):
        return "" if value is None else str(value)

    def line(text="", size=11, bold=False, gap=18):

        nonlocal y

        if y < 60:
            pdf.showPage()
            y = page_height - 40

        font = "Helvetica-Bold" if bold else "Helvetica"

        pdf.setFont(font, size)
        pdf.drawString(margin, y, text)

        y -= gap

    def section(title):

        nonlocal y

        y -= 10

        pdf.setFillColorRGB(0.1, 0.25, 0.55)

        pdf.rect(margin, y, 515, 22, fill=1)

        pdf.setFillColorRGB(1, 1, 1)

        pdf.setFont("Helvetica-Bold", 12)

        pdf.drawString(margin + 10, y + 6, title)

        pdf.setFillColorRGB(0, 0, 0)

        y -= 30

    def draw_photo_grid(photo_keys):

        nonlocal y

        start_x = margin
        col_gap = 20

        img_width = 220
        img_height = 140

        x_positions = [
            start_x,
            start_x + img_width + col_gap,
        ]

        current_col = 0

        for stage_key in photo_keys:

            photo = photos_by_stage.get(stage_key)

            if not photo:
                continue

            try:

                with urllib.request.urlopen(photo["filename"]) as response:
                    image_bytes = response.read()

                img = ImageReader(io.BytesIO(image_bytes))

                x = x_positions[current_col]
                draw_y = y - img_height

                pdf.drawImage(
                    img,
                    x,
                    draw_y,
                    width=img_width,
                    height=img_height,
                    preserveAspectRatio=True,
                    mask="auto",
                )

                pdf.setFont("Helvetica", 9)

                pdf.drawString(
                    x,
                    draw_y - 12,
                    PHOTO_LABELS.get(stage_key, stage_key),
                )

                current_col += 1

                if current_col > 1:
                    current_col = 0
                    y -= 190

                    if y < 220:
                        pdf.showPage()
                        y = page_height - 40

            except Exception:
                pass

        if current_col != 0:
            y -= 190

    # =========================
    # LOGO
    # =========================

    logo_path = os.path.join("static", "logo2.png")

    if os.path.exists(logo_path):

        try:
            pdf.drawImage(
                logo_path,
                margin,
                y - 50,
                width=180,
                height=50,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            pass

    y -= 70

    # =========================
    # HEADER
    # =========================

    pdf.setFont("Helvetica-Bold", 18)

    pdf.drawString(
        margin,
        y,
        "REPORT PRESA IN CARICO / RICONSEGNA MEZZO"
    )

    y -= 30

    pdf.setFont("Helvetica", 11)

    pdf.drawString(
        margin,
        y,
        f"Appalto: {current_appalto_nome() or ''}"
    )

    y -= 18

    pdf.drawString(
        margin,
        y,
        f"Generato il: {now_dt().strftime('%d/%m/%Y %H:%M')}"
    )

    y -= 30

    # =========================
    # DATI AUTISTA
    # =========================

    section("DATI AUTISTA")

    line(f"Nome: {safe(assignment['driver_name'])}")
    line(f"Telefono: {safe(assignment.get('driver_phone'))}")
    line(f"Email: {safe(assignment.get('driver_email'))}")

    # =========================
    # DATI MEZZO
    # =========================

    section("DATI MEZZO")

    line(f"Targa: {safe(assignment['plate'])}")
    line(f"Modello: {safe(assignment['model'])}")
    line(f"Data pratica: {only_date(assignment['created_at'])}")

    # =========================
    # PRESA IN CARICO
    # =========================

    section("PRESA IN CARICO")

    line(f"Data presa in carico: {safe(format_date(assignment.get('pickup_at')))}")
    line(f"KM presa in carico: {safe(assignment.get('pickup_km'))}")
    line(f"Carburante: {safe(assignment.get('pickup_fuel'))}")
    line(f"Firma: {safe(assignment.get('pickup_signature'))}")

    y -= 10

    checks = [
        ("Carrozzeria OK", assignment.get("body_ok")),
        ("Gomme OK", assignment.get("tyres_ok")),
        ("Documenti presenti", assignment.get("docs_ok")),
        ("Luci OK", assignment.get("lights_ok")),
    ]

    for label, value in checks:

        symbol = "OK" if value else "NO"

        line(f"{label}: {symbol}")

    line("")

    line("Note presa in carico:", bold=True)

    pickup_notes = assignment.get("pickup_notes") or "Nessuna"

    for txt in pickup_notes.splitlines():
        line(f"- {txt}")

    # =========================
    # RICONSEGNA
    # =========================

    section("RICONSEGNA")

    line(f"Data riconsegna: {safe(format_date(assignment.get('return_at')))}")
    line(f"KM riconsegna: {safe(assignment.get('return_km'))}")
    line(f"Carburante: {safe(assignment.get('return_fuel'))}")
    line(f"Firma: {safe(assignment.get('return_signature'))}")

    line("")

    line("Note riconsegna:", bold=True)

    return_notes = assignment.get("return_notes") or "Nessuna"

    for txt in return_notes.splitlines():
        line(f"- {txt}")

    # =========================
    # FOTO PRESA
    # =========================

    pdf.showPage()

    y = page_height - 40

    section("FOTO PRESA IN CARICO")

    draw_photo_grid([
        "pickup_front",
        "pickup_rear",
        "pickup_right",
        "pickup_left",
        "pickup_inside",
    ])

    # =========================
    # FOTO RICONSEGNA
    # =========================

    pdf.showPage()

    y = page_height - 40

    section("FOTO RICONSEGNA")

    draw_photo_grid([
        "return_front",
        "return_rear",
        "return_right",
        "return_left",
        "return_inside",
    ])

    # =========================
    # FOOTER
    # =========================

    y -= 20

    line(
        "FAR RIFERIMENTO A QUANTO DISCIPLINATO DALL'ART. 32 DEL CCNL",
        size=10,
        bold=True,
    )

    line(
        "IN VIGORE DAL 01/01/2025 OLTRE CHE AL MANUALE DELL'AUTISTA.",
        size=10,
    )

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
    filepath = os.path.join(UPLOAD_FOLDER, filename)

    if not os.path.exists(filepath):
        return "Foto non trovata", 404

    return send_from_directory(UPLOAD_FOLDER, filename)


# =========================
# START APP
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
