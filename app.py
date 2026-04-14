from __future__ import annotations

import io
import os
import secrets
import urllib.request
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

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

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL non configurata")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-questa-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

cloudinary.config(secure=True)

DEFAULT_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin12345")
DEFAULT_ADMIN_PASSWORD_HASH = generate_password_hash(DEFAULT_ADMIN_PASSWORD)

MAX_IMAGE_SIZE = (1600, 1600)
JPEG_QUALITY = 72

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


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def get_db():
    if "db" not in g:
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


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
                    email TEXT,
                    pin TEXT
                );
            """)

            cur.execute("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS pin TEXT;")

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

        db.commit()


def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped_view


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
            ORDER BY a.created_at DESC, a.id DESC
        """)
        assignments = cur.fetchall()

        cur.execute("""
            SELECT COUNT(*) AS count
            FROM assignments
            WHERE status IN ('Assegnato', 'Preso in carico')
        """)
        active_count = cur.fetchone()["count"]

        cur.execute("""
            SELECT COUNT(*) AS count
            FROM assignments
            WHERE status = 'Riconsegnato'
        """)
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

            if action == "select_van":
                van_id = request.form.get("van_id")

                if van_id:
                    cur.execute("SELECT * FROM vans WHERE id = %s", (van_id,))
                    van = cur.fetchone()

                    if not van or van["status"] != "Disponibile":
                        flash("Furgone non disponibile.", "error")
                    else:
                        token = secrets.token_urlsafe(16)

                        cur.execute(
                            """
                            INSERT INTO assignments (driver_id, van_id, token, created_at, status)
                            VALUES (%s, %s, %s, %s, %s)
                            RETURNING token
                            """,
                            (driver["id"], van_id, token, now_iso(), "Assegnato"),
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
                ORDER BY plate
                """
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
                AND a.status != 'Riconsegnato'
                ORDER BY a.id DESC
                """,
                (driver["id"],),
            )
            assignments = cur.fetchall()

    return render_template(
        "driver_select.html",
        driver=driver,
        assignments=assignments,
        available_vans=available_vans,
    )


@app.post("/drivers/create")
@admin_required
def create_driver():
    full_name = request.form.get("full_name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    pin = request.form.get("pin", "").strip()

    if not full_name or not pin:
        flash("Nome e PIN sono obbligatori.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO drivers (full_name, phone, email, pin) VALUES (%s, %s, %s, %s)",
            (full_name, phone, email, pin),
        )
    db.commit()

    flash("Autista creato correttamente.", "success")
    return redirect(url_for("dashboard"))


@app.post("/drivers/delete/<int:driver_id>")
@admin_required
def delete_driver(driver_id: int):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM drivers WHERE id = %s", (driver_id,))
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


@app.post("/vans/delete/<int:van_id>")
@admin_required
def delete_van(van_id: int):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM vans WHERE id = %s", (van_id,))
        db.commit()
        flash("Furgone eliminato correttamente.", "success")
    except Exception:
        db.rollback()
        flash("Impossibile eliminare il furgone. Potrebbe avere pratiche collegate.", "error")
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

    _, photos_by_stage = get_assignment_photos(assignment["id"])

    if request.method == "POST":
        action = request.form.get("action")

        if action == "pickup":
            required_pickup = [
                "pickup_front",
                "pickup_rear",
                "pickup_right",
                "pickup_left",
                "pickup_inside",
            ]

            missing_files = []
            for field_name in required_pickup:
                already_present = photos_by_stage.get(field_name) is not None
                new_file = request.files.get(field_name)
                if not already_present and (not new_file or not new_file.filename):
                    missing_files.append(PHOTO_LABELS[field_name])

            if missing_files:
                flash("Mancano foto obbligatorie: " + ", ".join(missing_files), "error")
                all_photos, photos_by_stage = get_assignment_photos(assignment["id"])
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

            for field_name in required_pickup:
                save_single_photo(request.files.get(field_name), assignment["id"], field_name)

            flash("Presa in carico registrata.", "success")
            return redirect(url_for("driver_portal", token=token))

        if action == "return":
            return_fields = [
                "return_front",
                "return_rear",
                "return_right",
                "return_left",
                "return_inside",
            ]

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

            for field_name in return_fields:
                save_single_photo(request.files.get(field_name), assignment["id"], field_name)

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


@app.route("/pdf/<int:assignment_id>")
@admin_required
def genera_pdf(assignment_id: int):
    db = get_db()

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
            """,
            (assignment_id,),
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
    _, page_height = A4
    margin = 40
    y = page_height - margin

    def write_line(text: str, size: int = 11, step: int = 18, bold: bool = False):
        nonlocal y
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        pdf.drawString(margin, y, text)
        y -= step

    def new_page():
        nonlocal y
        pdf.showPage()
        y = page_height - margin

    def safe_text(value):
        return "" if value is None else str(value)

    def draw_photo_block(stage_key: str):
        nonlocal y

        label = PHOTO_LABELS[stage_key]
        photo = photos_by_stage.get(stage_key)

        if y < 220:
            new_page()

        write_line(label, size=11, step=16, bold=True)

        if not photo:
            write_line("Foto non presente.")
            y -= 8
            return

        try:
            with urllib.request.urlopen(photo["filename"]) as response:
                image_bytes = response.read()

            img = ImageReader(io.BytesIO(image_bytes))
            img_width, img_height = img.getSize()

            max_width = 160
            max_height = 110
            scale = min(max_width / img_width, max_height / img_height)
            draw_width = img_width * scale
            draw_height = img_height * scale

            img_y = y - draw_height
            pdf.drawImage(
                img,
                margin,
                img_y,
                width=draw_width,
                height=draw_height,
                preserveAspectRatio=True,
                mask="auto",
            )
            y = img_y - 18

        except Exception as e:
            write_line(f"Immagine non caricabile: {str(e)}")
            y -= 8

    pdf.setTitle(f"report_{assignment_id}.pdf")

    write_line("REPORT GIORNALIERO PRESA IN CARICO MEZZO", size=16, step=28, bold=True)
    write_line(f"Generato il: {datetime.now().strftime('%d/%m/%Y %H:%M')}", size=10, step=22)

    write_line(f"Data pratica: {only_date(assignment['created_at'])}")
    write_line(f"Data e ora creazione: {format_date(assignment['created_at'])}")
    write_line(f"Autista: {assignment['driver_name']}")
    write_line(f"Telefono autista: {safe_text(assignment.get('driver_phone'))}")
    write_line(f"Email autista: {safe_text(assignment.get('driver_email'))}")
    write_line(f"Mezzo: {assignment['plate']} - {assignment['model']}")
    y -= 8

    write_line("PRESA IN CARICO", size=13, step=20, bold=True)
    write_line(f"Data e ora presa in carico: {format_date(assignment.get('pickup_at'))}")
    write_line(f"KM presa in carico: {safe_text(assignment.get('pickup_km'))}")
    write_line(f"Carburante presa in carico: {safe_text(assignment.get('pickup_fuel'))}")
    write_line(f"Firma presa in carico: {safe_text(assignment.get('pickup_signature'))}")
    write_line(f"Carrozzeria OK: {'Si' if assignment.get('body_ok') else 'No'}")
    write_line(f"Gomme OK: {'Si' if assignment.get('tyres_ok') else 'No'}")
    write_line(f"Documenti presenti: {'Si' if assignment.get('docs_ok') else 'No'}")
    write_line(f"Luci OK: {'Si' if assignment.get('lights_ok') else 'No'}")

    pickup_notes = assignment.get("pickup_notes") or ""
    write_line("Note presa in carico:")
    if pickup_notes:
        for line in pickup_notes.splitlines():
            write_line(f"- {line}")
    else:
        write_line("- Nessuna")

    y -= 6
    draw_photo_block("pickup_front")
    draw_photo_block("pickup_rear")
    draw_photo_block("pickup_right")
    draw_photo_block("pickup_left")
    draw_photo_block("pickup_inside")

    if y < 220:
        new_page()

    write_line("RICONSEGNA", size=13, step=20, bold=True)
    write_line(f"Data e ora riconsegna: {format_date(assignment.get('return_at'))}")
    write_line(f"KM riconsegna: {safe_text(assignment.get('return_km'))}")
    write_line(f"Carburante riconsegna: {safe_text(assignment.get('return_fuel'))}")
    write_line(f"Firma riconsegna: {safe_text(assignment.get('return_signature'))}")

    return_notes = assignment.get("return_notes") or ""
    write_line("Note riconsegna:")
    if return_notes:
        for line in return_notes.splitlines():
            write_line(f"- {line}")
    else:
        write_line("- Nessuna")

    y -= 6
    draw_photo_block("return_front")
    draw_photo_block("return_rear")
    draw_photo_block("return_right")
    draw_photo_block("return_left")
    draw_photo_block("return_inside")

    pdf.save()
    buffer.seek(0)

    filename = f"report_{assignment['driver_name'].replace(' ', '_')}_{assignment['plate']}_{assignment_id}.pdf"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf"
    )


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return redirect(filename)


init_db()

if __name__ == "__main__":
    app.run(debug=True)

