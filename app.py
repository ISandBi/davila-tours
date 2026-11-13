import hmac
import io
import os
import re
import secrets
import sqlite3
import tempfile
from calendar import Calendar
from datetime import date, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash


DESTINATIONS = ("Oaxaca", "Guadalajara", "Veracruz")
RESERVATION_STATES = {
    "pendiente": ("Pendiente", "clock"),
    "apartado": ("Apartado", "info"),
    "pagado": ("Pagado", "check"),
    "cancelado": ("Cancelado", "close"),
}
ALLOWED_TRANSITIONS = {
    "pendiente": {"pendiente", "apartado", "pagado", "cancelado"},
    "apartado": {"apartado", "pagado", "cancelado"},
    "pagado": {"pagado"},
    "cancelado": {"cancelado"},
}
EMAIL_LOCAL_PATTERN = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
PHONE_PATTERN = re.compile(r"^\d{10}$")
PROFILE_PHOTO_MAX_BYTES = 5 * 1024 * 1024
PROFILE_PHOTO_MAX_PIXELS = 25_000_000
PROFILE_PHOTO_NAME_PATTERN = re.compile(r"^[a-f0-9]{32}\.jpg$")


SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_domains (
    id INTEGER PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('cliente', 'chofer', 'administrador')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    profile_photo TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS destinations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS buses (
    id INTEGER PRIMARY KEY,
    plate TEXT NOT NULL UNIQUE,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    status TEXT NOT NULL CHECK (status IN ('Disponible', 'Mantenimiento'))
);
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY,
    destination_id INTEGER NOT NULL REFERENCES destinations(id),
    departure_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    bus_id INTEGER REFERENCES buses(id),
    driver_user_id INTEGER REFERENCES users(id),
    UNIQUE (departure_date)
);
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY,
    folio TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    passenger_count INTEGER NOT NULL CHECK (passenger_count > 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    status TEXT NOT NULL CHECK (status IN ('pendiente', 'apartado', 'pagado', 'cancelado')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reservation_seats (
    id INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    seat_number INTEGER NOT NULL CHECK (seat_number > 0),
    UNIQUE (trip_id, seat_number),
    UNIQUE (reservation_id, seat_number)
);
CREATE INDEX IF NOT EXISTS idx_reservations_trip_status
    ON reservations (trip_id, status);
CREATE INDEX IF NOT EXISTS idx_reservations_user
    ON reservations (user_id, created_at DESC);
"""


def get_db():
    if "db" not in g:
        connection = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.db = connection
    return g.db


def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def seat_rows_for_capacity(capacity):
    rows = [[None, None, 3, 4]]
    rows.extend(
        [1 + 4 * row, 2 + 4 * row, 7 + 4 * row, 8 + 4 * row]
        for row in range(9)
    )
    rows.append([37, 38, None, None])
    rows.extend(
        [seat, seat + 1, seat + 2, seat + 3]
        for seat in range(41, capacity + 1, 4)
    )
    return [
        [seat if seat is None or seat <= capacity else None for seat in row]
        for row in rows
    ]


def image_contains_face(image_data):
    image = cv2.imdecode(
        np.frombuffer(image_data, dtype=np.uint8),
        cv2.IMREAD_GRAYSCALE,
    )
    if image is None:
        raise ValueError("No se pudo procesar la fotografía.")
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    with tempfile.NamedTemporaryFile(
        prefix="davila-haar-",
        suffix=".xml",
        delete=False,
    ) as cascade_file:
        cascade_file.write(cascade_path.read_bytes())
        temporary_cascade = Path(cascade_file.name)
    try:
        detector = cv2.CascadeClassifier(str(temporary_cascade))
    finally:
        temporary_cascade.unlink()
    if detector.empty():
        raise RuntimeError("No está disponible el detector local de rostros.")
    faces = detector.detectMultiScale(
        image,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(80, 80),
    )
    return len(faces) > 0


def normalize_profile_photo(upload):
    if upload is None or not upload.filename:
        raise ValueError("Selecciona una fotografía para continuar.")
    image_data = upload.stream.read(PROFILE_PHOTO_MAX_BYTES + 1)
    if len(image_data) > PROFILE_PHOTO_MAX_BYTES:
        raise ValueError("La fotografía no puede superar 5 MB.")
    try:
        with Image.open(io.BytesIO(image_data)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Usa una fotografía JPG, PNG o WEBP.")
            width, height = source.size
            if width < 80 or height < 80:
                raise ValueError("La fotografía debe medir al menos 80 × 80 píxeles.")
            if width * height > PROFILE_PHOTO_MAX_PIXELS:
                raise ValueError("La fotografía tiene una resolución demasiado grande.")
            source.verify()
        with Image.open(io.BytesIO(image_data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            image.thumbnail((1024, 1024))
            normalized = io.BytesIO()
            image.convert("RGB").save(normalized, format="JPEG", quality=88, optimize=True)
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as error:
        raise ValueError("El archivo no contiene una fotografía válida.") from error
    normalized_data = normalized.getvalue()
    if not image_contains_face(normalized_data):
        raise ValueError(
            "La fotografía debe mostrar un rostro de forma clara. "
            "Esta validación no confirma la identidad de la persona."
        )
    return normalized_data


def initialize_database():
    db = get_db()
    db.executescript(SCHEMA)
    users_table = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        ("table", "users"),
    ).fetchone()
    if users_table and "microbusero" in users_table["sql"]:
        db.commit()
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE users_rebuild (
                    id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL CHECK (
                        role IN ('cliente', 'chofer', 'administrador')
                    ),
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO users_rebuild
                    (id, full_name, email, password_hash, phone, role, active, created_at)
                SELECT id, full_name, email, password_hash, phone,
                       CASE role WHEN 'microbusero' THEN 'chofer' ELSE role END,
                       active, created_at
                FROM users;
                DROP TABLE users;
                ALTER TABLE users_rebuild RENAME TO users;
                COMMIT;
                """
            )
        except sqlite3.Error:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")

    user_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if "profile_photo" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN profile_photo TEXT")

    reservation_table = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        ("table", "reservations"),
    ).fetchone()
    if reservation_table and "BETWEEN 1 AND 20" in reservation_table["sql"]:
        db.commit()
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE reservations_rebuild (
                    id INTEGER PRIMARY KEY,
                    folio TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    trip_id INTEGER NOT NULL REFERENCES trips(id),
                    passenger_count INTEGER NOT NULL CHECK (passenger_count > 0),
                    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                    status TEXT NOT NULL CHECK (
                        status IN ('pendiente', 'apartado', 'pagado', 'cancelado')
                    ),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO reservations_rebuild
                    (id, folio, user_id, trip_id, passenger_count, amount_cents, status, created_at)
                SELECT id, folio, user_id, trip_id, passenger_count, amount_cents, status, created_at
                FROM reservations;
                DROP TABLE reservations;
                ALTER TABLE reservations_rebuild RENAME TO reservations;
                CREATE INDEX idx_reservations_trip_status
                    ON reservations (trip_id, status);
                CREATE INDEX idx_reservations_user
                    ON reservations (user_id, created_at DESC);
                COMMIT;
                """
            )
        finally:
            db.execute("PRAGMA foreign_keys = ON")
    db.execute(
        "INSERT OR IGNORE INTO app_settings (setting_key, setting_value) VALUES (?, ?)",
        ("email_separator", "@"),
    )
    db.executemany(
        "INSERT OR IGNORE INTO email_domains (domain) VALUES (?)",
        (("gmail.com",), ("hotmail.com",), ("outlook.com",)),
    )
    db.executemany(
        "INSERT OR IGNORE INTO destinations (name) VALUES (?)",
        ((name,) for name in DESTINATIONS),
    )

    if db.execute("SELECT COUNT(*) FROM buses").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO buses (plate, capacity, status) VALUES (?, ?, ?)",
            [
                ("DT-101-A", 45, "Disponible"),
                ("DT-202-B", 40, "Disponible"),
                ("DT-303-C", 38, "Disponible"),
                ("DT-404-D", 42, "Disponible"),
                ("DT-505-E", 36, "Mantenimiento"),
            ],
        )

    demo_password = current_app.config["DEMO_PASSWORD"]
    demo_users = [
        (
            "Administrador Dávila",
            current_app.config["DEMO_ADMIN_EMAIL"],
            "administrador",
        ),
    ]
    demo_users.extend(
        (f"Chofer {index}", f"chofer{index}@gmail.com", "chofer")
        for index in range(1, 7)
    )
    for name, email, role in demo_users:
        db.execute(
            """
            INSERT OR IGNORE INTO users (full_name, email, password_hash, role)
            VALUES (?, ?, ?, ?)
            """,
            (name, email, generate_password_hash(demo_password), role),
        )

    if db.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 0:
        destinations = {
            row["name"]: row["id"]
            for row in db.execute("SELECT id, name FROM destinations")
        }
        next_thursday = date.today() + timedelta(
            days=(3 - date.today().weekday()) % 7
        )
        trips = []
        for week in range(12):
            departure = next_thursday + timedelta(weeks=week)
            destination = DESTINATIONS[week % len(DESTINATIONS)]
            trips.append(
                (
                    destinations[destination],
                    departure.isoformat(),
                    (departure + timedelta(days=3)).isoformat(),
                )
            )
        db.executemany(
            """
            INSERT INTO trips (destination_id, departure_date, return_date)
            VALUES (?, ?, ?)
            """,
            trips,
        )
    max_capacity = db.execute(
        "SELECT COALESCE(MAX(capacity), 0) FROM buses WHERE status = ?",
        ("Disponible",),
    ).fetchone()[0]
    legacy_reservations = db.execute(
        """
        SELECT r.id, r.trip_id, r.passenger_count, COALESCE(b.capacity, ?) AS capacity
        FROM reservations r
        JOIN trips t ON t.id = r.trip_id
        LEFT JOIN buses b ON b.id = t.bus_id
        WHERE r.status != 'cancelado'
          AND NOT EXISTS (
              SELECT 1 FROM reservation_seats rs WHERE rs.reservation_id = r.id
          )
        ORDER BY r.trip_id, r.id
        """,
        (max_capacity,),
    ).fetchall()
    occupied_seats_by_trip = {}
    for reservation in legacy_reservations:
        trip_id = reservation["trip_id"]
        if trip_id not in occupied_seats_by_trip:
            occupied_seats_by_trip[trip_id] = {
                row["seat_number"]
                for row in db.execute(
                    "SELECT seat_number FROM reservation_seats WHERE trip_id = ?",
                    (trip_id,),
                )
            }
        occupied = occupied_seats_by_trip[trip_id]
        available = [
            seat
            for seat in range(1, reservation["capacity"] + 1)
            if seat not in occupied
        ]
        if len(available) >= reservation["passenger_count"]:
            db.executemany(
                """
                INSERT INTO reservation_seats (reservation_id, trip_id, seat_number)
                VALUES (?, ?, ?)
                """,
                (
                    (reservation["id"], reservation["trip_id"], seat)
                    for seat in available[: reservation["passenger_count"]]
                ),
            )
            occupied.update(available[: reservation["passenger_count"]])
    db.commit()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-this-secret"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower()
        == "true",
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
        PROFILE_PHOTO_FOLDER=os.environ.get(
            "PROFILE_PHOTO_FOLDER", str(Path(app.instance_path) / "profile_photos")
        ),
        DATABASE=os.environ.get(
            "DATABASE_PATH", str(Path(app.instance_path) / "davila_tours.sqlite3")
        ),
        DEMO_PASSWORD=os.environ.get("DEMO_PASSWORD", "Davilatours2026!"),
        DEMO_ADMIN_EMAIL=os.environ.get("DEMO_ADMIN_EMAIL", "admin@gmail.com"),
        QA_ACCESS_TOKEN=os.environ.get("QA_ACCESS_TOKEN", ""),
        RESERVATION_PRICE_CENTS=int(os.environ.get("RESERVATION_PRICE_CENTS", "0")),
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["PROFILE_PHOTO_FOLDER"]).mkdir(parents=True, exist_ok=True)

    app.teardown_appcontext(close_db)
    with app.app_context():
        initialize_database()

    @app.before_request
    def protect_forms():
        if request.method == "POST":
            submitted = request.form.get("csrf_token", "")
            expected = session.get("_csrf_token", "")
            if not expected or not hmac.compare_digest(submitted, expected):
                abort(400, description="La sesión del formulario venció. Recarga e inténtalo de nuevo.")

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self' data:; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'self'",
        )
        return response

    @app.context_processor
    def inject_template_helpers():
        def csrf_token():
            if "_csrf_token" not in session:
                session["_csrf_token"] = secrets.token_urlsafe(32)
            return session["_csrf_token"]

        return {
            "csrf_token": csrf_token,
            "reservation_states": RESERVATION_STATES,
            "allowed_transitions": ALLOWED_TRANSITIONS,
            "current_user": session.get("user"),
        }

    @app.template_filter("mxn")
    def format_mxn(cents):
        if not cents:
            return "Por confirmar"
        return f"${cents / 100:,.2f} MXN"

    def authenticated_role(*roles):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                user = session.get("user")
                if not user:
                    flash("Inicia sesión para continuar.", "info")
                    return redirect(url_for("login", next=request.path))
                if user["role"] not in roles:
                    abort(403)
                return view(*args, **kwargs)

            return wrapped

        return decorator

    def safe_next_url():
        candidate = request.form.get("next", "").strip()
        parsed = urlsplit(candidate)
        if (
            candidate.startswith("/")
            and not candidate.startswith("//")
            and not parsed.netloc
            and not parsed.scheme
        ):
            return candidate
        return ""

    def email_from_form():
        local_part = request.form.get("email_local", "").strip()
        domain_id = request.form.get("email_domain", "").strip()
        if not EMAIL_LOCAL_PATTERN.fullmatch(local_part) or local_part.startswith(".") or local_part.endswith(".") or ".." in local_part:
            raise ValueError("Escribe la parte del correo anterior al arroba.")
        db = get_db()
        separator = db.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = ?",
            ("email_separator",),
        ).fetchone()
        domain = db.execute(
            "SELECT domain FROM email_domains WHERE id = ?",
            (domain_id,),
        ).fetchone()
        if separator is None or domain is None:
            raise ValueError("Selecciona un dominio de correo válido.")
        return f"{local_part.lower()}{separator['setting_value']}{domain['domain']}"

    def render_auth(template, **extra):
        domains = get_db().execute(
            "SELECT id, domain FROM email_domains ORDER BY domain"
        ).fetchall()
        separator = get_db().execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = ?",
            ("email_separator",),
        ).fetchone()["setting_value"]
        return render_template(
            template, email_domains=domains, email_separator=separator, **extra
        )

    def get_trip_rows(where="", parameters=()):
        db = get_db()
        max_capacity = db.execute(
            "SELECT COALESCE(MAX(capacity), 0) FROM buses WHERE status = ?",
            ("Disponible",),
        ).fetchone()[0]
        query = f"""
            SELECT t.id, t.departure_date, t.return_date, d.name AS destination,
                   t.bus_id, t.driver_user_id, b.plate, b.capacity AS bus_capacity,
                   b.status AS bus_status, u.full_name AS driver_name,
                   COALESCE(b.capacity, ?) AS capacity,
                   COALESCE((
                       SELECT SUM(r.passenger_count) FROM reservations r
                       WHERE r.trip_id = t.id AND r.status != 'cancelado'
                   ), 0) AS reserved
            FROM trips t
            JOIN destinations d ON d.id = t.destination_id
            LEFT JOIN buses b ON b.id = t.bus_id
            LEFT JOIN users u ON u.id = t.driver_user_id
            {where}
            ORDER BY t.departure_date
        """
        return db.execute(query, (max_capacity, *parameters)).fetchall()

    @app.get("/")
    def home():
        trips = get_trip_rows("WHERE t.departure_date >= ?", (date.today().isoformat(),))
        return render_template("home.html", trips=trips[:6])

    @app.route("/qa", methods=["GET", "POST"])
    def qa_access():
        qa_token = app.config["QA_ACCESS_TOKEN"]
        if not qa_token:
            abort(503, description="El acceso QA no está configurado.")

        if request.method == "POST":
            if request.form.get("action") == "select_profile":
                if not session.get("qa_access"):
                    abort(403)
                role = request.form.get("role", "")
                if role not in {"cliente", "chofer", "administrador"}:
                    abort(400, description="Selecciona un perfil válido.")
                user = get_db().execute(
                    """
                    SELECT id, full_name, email, role FROM users
                    WHERE role = ? AND active = 1 ORDER BY id LIMIT 1
                    """,
                    (role,),
                ).fetchone()
                if user is None and role == "cliente":
                    get_db().execute(
                        """
                        INSERT INTO users (full_name, email, password_hash, role)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            "Pasajero QA",
                            "qa-passenger@davila-tours.invalid",
                            generate_password_hash(secrets.token_urlsafe(32)),
                            role,
                        ),
                    )
                    get_db().commit()
                    user = get_db().execute(
                        """
                        SELECT id, full_name, email, role FROM users
                        WHERE email = ? AND active = 1
                        """,
                        ("qa-passenger@davila-tours.invalid",),
                    ).fetchone()
                if user is None:
                    flash("No hay una cuenta activa para ese perfil.", "error")
                    return render_template("qa_access.html", qa_access=True)
                session.clear()
                session["qa_access"] = True
                session["user"] = dict(user)
                return redirect(url_for("test_hub"))

            submitted_token = request.form.get("token", "")
            if len(submitted_token) > 512 or not hmac.compare_digest(
                submitted_token, qa_token
            ):
                flash("El token no es válido.", "error")
                return render_template("qa_access.html", qa_access=False), 401
            session.clear()
            session["qa_access"] = True

        return render_template(
            "qa_access.html", qa_access=bool(session.get("qa_access"))
        )

    @app.get("/pruebas")
    @authenticated_role("cliente", "chofer", "administrador")
    def test_hub():
        dashboard_endpoints = {
            "cliente": "client_dashboard",
            "chofer": "driver_dashboard",
            "administrador": "admin_dashboard",
        }
        return render_template(
            "test_hub.html",
            dashboard_url=url_for(dashboard_endpoints[session["user"]["role"]]),
        )

    @app.route("/registro", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            password = request.form.get("password", "")
            phone = request.form.get("phone", "").strip()
            try:
                email = email_from_form()
            except ValueError as error:
                flash(str(error), "error")
            else:
                if not full_name or len(full_name) > 100:
                    flash("Escribe tu nombre completo (máximo 100 caracteres).", "error")
                elif len(password) < 8 or len(password) > 128:
                    flash("La contraseña debe tener entre 8 y 128 caracteres.", "error")
                elif phone and not PHONE_PATTERN.fullmatch(phone):
                    flash("El teléfono debe contener 10 dígitos.", "error")
                else:
                    try:
                        get_db().execute(
                            """
                            INSERT INTO users (full_name, email, password_hash, phone, role)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                full_name,
                                email,
                                generate_password_hash(password),
                                phone,
                                "cliente",
                            ),
                        )
                        get_db().commit()
                    except sqlite3.IntegrityError:
                        flash("Ya existe una cuenta con ese correo.", "error")
                    else:
                        session.clear()
                        user = get_db().execute(
                            "SELECT id, full_name, email, role FROM users WHERE email = ?",
                            (email,),
                        ).fetchone()
                        session["user"] = dict(user)
                        flash("Tu cuenta quedó lista. Ya puedes reservar tu viaje.", "success")
                        next_url = safe_next_url()
                        if next_url:
                            return redirect(next_url)
                        return redirect(url_for("client_dashboard"))
            return render_auth("register.html")
        return render_auth("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            try:
                email = email_from_form()
            except ValueError as error:
                flash(str(error), "error")
            else:
                user = get_db().execute(
                    """
                    SELECT id, full_name, email, password_hash, role
                    FROM users WHERE email = ? AND active = 1
                    """,
                    (email,),
                ).fetchone()
                if user and check_password_hash(
                    user["password_hash"], request.form.get("password", "")
                ):
                    session.clear()
                    session["user"] = {
                        "id": user["id"],
                        "full_name": user["full_name"],
                        "email": user["email"],
                        "role": user["role"],
                    }
                    flash(f"Bienvenido, {user['full_name']}.", "success")
                    endpoints = {
                        "cliente": "client_dashboard",
                        "chofer": "driver_dashboard",
                        "administrador": "admin_dashboard",
                    }
                    next_url = safe_next_url()
                    if user["role"] == "cliente" and next_url:
                        return redirect(next_url)
                    return redirect(url_for(endpoints[user["role"]]))
                flash("Correo o contraseña incorrectos.", "error")
            return render_auth("login.html")
        return render_auth("login.html")

    @app.get("/salir")
    def logout():
        session.clear()
        flash("Has cerrado sesión.", "info")
        return redirect(url_for("home"))

    @app.route("/perfil", methods=["GET", "POST"])
    @authenticated_role("cliente", "chofer", "administrador")
    def profile():
        db = get_db()
        user_id = session["user"]["id"]
        if request.method == "POST":
            existing_user = db.execute(
                "SELECT profile_photo FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if existing_user is None:
                abort(404)
            try:
                photo_data = normalize_profile_photo(request.files.get("profile_photo"))
            except ValueError as error:
                flash(str(error), "error")
                return redirect(url_for("profile"))
            photo_name = f"{secrets.token_hex(16)}.jpg"
            photo_path = Path(app.config["PROFILE_PHOTO_FOLDER"]) / photo_name
            photo_path.write_bytes(photo_data)
            previous_photo = existing_user["profile_photo"]
            db.execute(
                "UPDATE users SET profile_photo = ? WHERE id = ?",
                (photo_name, user_id),
            )
            db.commit()
            if previous_photo and PROFILE_PHOTO_NAME_PATTERN.fullmatch(previous_photo):
                previous_path = Path(app.config["PROFILE_PHOTO_FOLDER"]) / previous_photo
                if previous_path.is_file():
                    previous_path.unlink()
            flash("Tu fotografía de perfil se actualizó.", "success")
            return redirect(url_for("profile"))

        user = db.execute(
            "SELECT full_name, email, role, profile_photo FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if user is None:
            abort(404)
        return render_template("profile.html", profile=user)

    @app.get("/perfil/foto")
    @authenticated_role("cliente", "chofer", "administrador")
    def profile_photo():
        user = get_db().execute(
            "SELECT profile_photo FROM users WHERE id = ?",
            (session["user"]["id"],),
        ).fetchone()
        if (
            user is None
            or not user["profile_photo"]
            or not PROFILE_PHOTO_NAME_PATTERN.fullmatch(user["profile_photo"])
        ):
            abort(404)
        return send_from_directory(
            app.config["PROFILE_PHOTO_FOLDER"],
            user["profile_photo"],
            mimetype="image/jpeg",
            max_age=0,
        )

    @app.get("/reservar")
    def booking():
        destination = request.args.get("destino", "").strip()
        if destination not in DESTINATIONS:
            destination = ""
        where = (
            "WHERE t.departure_date >= ? "
            "AND strftime('%w', t.departure_date) = '4' "
            "AND t.return_date = date(t.departure_date, '+3 days')"
        )
        parameters = [date.today().isoformat()]
        if destination:
            where += " AND d.name = ?"
            parameters.append(destination)
        trips = get_trip_rows(where, parameters)
        selected_date = request.args.get("salida", "")
        selected_trip = next(
            (trip for trip in trips if trip["departure_date"] == selected_date),
            None,
        )
        month_value = request.args.get("mes", "")
        if not month_value and selected_trip:
            month_value = selected_trip["departure_date"][:7]
        try:
            month_start = date.fromisoformat(f"{month_value}-01")
        except ValueError:
            month_start = date.today().replace(day=1)
        if not re.fullmatch(r"\d{4}-\d{2}", month_value):
            month_start = date.today().replace(day=1)
        trips_by_date = {trip["departure_date"]: trip for trip in trips}
        occupied_seats = set()
        if selected_trip:
            occupied_seats = {
                row["seat_number"]
                for row in get_db().execute(
                    """
                    SELECT rs.seat_number
                    FROM reservation_seats rs
                    JOIN reservations r ON r.id = rs.reservation_id
                    WHERE rs.trip_id = ? AND r.status != 'cancelado'
                    """,
                    (selected_trip["id"],),
                )
            }
        previous_month = (month_start.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        next_month = (
            (month_start.replace(day=28) + timedelta(days=4))
            .replace(day=1)
            .strftime("%Y-%m")
        )
        month_trips = any(
            trip["departure_date"].startswith(month_start.strftime("%Y-%m"))
            for trip in trips
        )
        return render_template(
            "booking.html",
            trips=trips,
            destinations=DESTINATIONS,
            destination_filter=destination,
            month_start=month_start,
            calendar_weeks=Calendar(firstweekday=0).monthdatescalendar(
                month_start.year, month_start.month
            ),
            trips_by_date=trips_by_date,
            selected_trip=selected_trip,
            occupied_seats=occupied_seats,
            previous_month=previous_month,
            next_month=next_month,
            month_trips=month_trips,
            seat_rows=(
                seat_rows_for_capacity(selected_trip["capacity"])
                if selected_trip
                else []
            ),
        )

    @app.post("/reservar")
    @authenticated_role("cliente")
    def create_reservation():
        try:
            trip_id = int(request.form.get("trip_id", ""))
            passenger_count = int(request.form.get("passenger_count", ""))
            requested_seats = [
                int(value) for value in request.form.getlist("seat_numbers")
            ]
        except ValueError:
            flash("Selecciona una salida, asientos y cantidad de pasajeros válidos.", "error")
            return redirect(url_for("booking"))
        if passenger_count < 1:
            flash("La cantidad de pasajeros debe ser mayor que cero.", "error")
            return redirect(url_for("booking"))
        if requested_seats and (
            len(requested_seats) != passenger_count
            or len(set(requested_seats)) != len(requested_seats)
        ):
            flash("Selecciona exactamente un asiento por pasajero.", "error")
            return redirect(url_for("booking"))

        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        trip = get_trip_rows(
            """
            WHERE t.id = ? AND t.departure_date >= ?
              AND strftime('%w', t.departure_date) = '4'
              AND t.return_date = date(t.departure_date, '+3 days')
            """,
            (trip_id, date.today().isoformat()),
        )
        if not trip:
            db.rollback()
            flash("La salida seleccionada ya no está disponible.", "error")
            return redirect(url_for("booking"))
        trip = trip[0]
        if trip["reserved"] + passenger_count > trip["capacity"]:
            db.rollback()
            flash("No hay lugares suficientes para ese grupo en la salida seleccionada.", "error")
            return redirect(url_for("booking"))
        occupied_seats = {
            row["seat_number"]
            for row in db.execute(
                """
                SELECT rs.seat_number
                FROM reservation_seats rs
                JOIN reservations r ON r.id = rs.reservation_id
                WHERE rs.trip_id = ? AND r.status != 'cancelado'
                """,
                (trip_id,),
            )
        }
        available_seats = [
            seat
            for seat in range(1, trip["capacity"] + 1)
            if seat not in occupied_seats
        ]
        if not requested_seats:
            requested_seats = available_seats[:passenger_count]
        if (
            len(requested_seats) != passenger_count
            or any(seat not in available_seats for seat in requested_seats)
        ):
            db.rollback()
            flash("Uno o más asientos ya no están disponibles. Selecciónalos de nuevo.", "error")
            return redirect(
                url_for(
                    "booking",
                    destino=trip["destination"],
                    salida=trip["departure_date"],
                )
            )

        folio = ""
        for _attempt in range(3):
            candidate = f"DT-{secrets.token_hex(4).upper()}"
            if not db.execute(
                "SELECT 1 FROM reservations WHERE folio = ?", (candidate,)
            ).fetchone():
                folio = candidate
                break
        if not folio:
            db.rollback()
            flash("No fue posible generar un folio único. Inténtalo de nuevo.", "error")
            return redirect(url_for("booking"))
        cursor = db.execute(
            """
            INSERT INTO reservations
                (folio, user_id, trip_id, passenger_count, amount_cents, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                folio,
                session["user"]["id"],
                trip_id,
                passenger_count,
                current_app.config["RESERVATION_PRICE_CENTS"] * passenger_count,
                "pendiente",
            ),
        )
        db.executemany(
            """
            INSERT INTO reservation_seats (reservation_id, trip_id, seat_number)
            VALUES (?, ?, ?)
            """,
            ((cursor.lastrowid, trip_id, seat) for seat in requested_seats),
        )
        db.commit()
        return redirect(url_for("reservation_detail", folio=folio))

    @app.get("/cliente")
    @authenticated_role("cliente")
    def client_dashboard():
        reservations = get_db().execute(
            """
            SELECT r.*, t.departure_date, t.return_date, d.name AS destination
                   , (SELECT GROUP_CONCAT(seat_number, ', ')
                      FROM (SELECT seat_number FROM reservation_seats
                            WHERE reservation_id = r.id ORDER BY seat_number)) AS seats
            FROM reservations r
            JOIN trips t ON t.id = r.trip_id
            JOIN destinations d ON d.id = t.destination_id
            WHERE r.user_id = ?
            ORDER BY r.created_at DESC, r.id DESC
            """,
            (session["user"]["id"],),
        ).fetchall()
        return render_template("client.html", reservations=reservations)

    @app.get("/cliente/reservas/<folio>")
    @authenticated_role("cliente")
    def reservation_detail(folio):
        reservation = get_db().execute(
            """
            SELECT r.*, t.departure_date, t.return_date, d.name AS destination
                   , (SELECT GROUP_CONCAT(seat_number, ', ')
                      FROM (SELECT seat_number FROM reservation_seats
                            WHERE reservation_id = r.id ORDER BY seat_number)) AS seats
            FROM reservations r
            JOIN trips t ON t.id = r.trip_id
            JOIN destinations d ON d.id = t.destination_id
            WHERE r.folio = ? AND r.user_id = ?
            """,
            (folio, session["user"]["id"]),
        ).fetchone()
        if reservation is None:
            abort(404)
        return render_template("reservation_detail.html", reservation=reservation)

    @app.post("/cliente/reservas/<folio>/cancelar")
    @authenticated_role("cliente")
    def cancel_reservation(folio):
        db = get_db()
        reservation = db.execute(
            """
            SELECT id FROM reservations
            WHERE folio = ? AND user_id = ? AND status IN ('pendiente', 'apartado')
            """,
            (folio, session["user"]["id"]),
        ).fetchone()
        if reservation is not None:
            db.execute(
                "DELETE FROM reservation_seats WHERE reservation_id = ?",
                (reservation["id"],),
            )
        cursor = db.execute(
            """
            UPDATE reservations SET status = 'cancelado'
            WHERE folio = ? AND user_id = ? AND status IN ('pendiente', 'apartado')
            """,
            (folio, session["user"]["id"]),
        )
        db.commit()
        if cursor.rowcount:
            flash("La reserva fue cancelada y los lugares quedaron disponibles.", "success")
        else:
            flash("Esta reserva no se puede cancelar desde tu perfil.", "error")
        return redirect(url_for("reservation_detail", folio=folio))

    @app.get("/chofer")
    @authenticated_role("chofer")
    def driver_dashboard():
        trips = get_trip_rows(
            "WHERE t.driver_user_id = ? AND t.departure_date >= ?",
            (session["user"]["id"], date.today().isoformat()),
        )
        manifests = {}
        if trips:
            trip_ids = [trip["id"] for trip in trips]
            placeholders = ",".join("?" for _ in trip_ids)
            rows = get_db().execute(
                f"""
                SELECT r.trip_id, r.folio, r.passenger_count, r.status,
                       u.full_name AS passenger_name
                FROM reservations r
                JOIN users u ON u.id = r.user_id
                WHERE r.trip_id IN ({placeholders}) AND r.status != 'cancelado'
                ORDER BY r.trip_id, r.id
                """,
                trip_ids,
            ).fetchall()
            for row in rows:
                manifests.setdefault(row["trip_id"], []).append(row)
        return render_template("driver.html", trips=trips, manifests=manifests)

    @app.get("/microbusero")
    @authenticated_role("chofer")
    def legacy_driver_dashboard():
        return redirect(url_for("driver_dashboard"), code=301)

    @app.get("/administrador")
    @authenticated_role("administrador")
    def admin_dashboard():
        db = get_db()
        status_filter = request.args.get("estado", "").strip()
        destination_filter = request.args.get("destino", "").strip()
        conditions = []
        values = []
        if status_filter in RESERVATION_STATES:
            conditions.append("r.status = ?")
            values.append(status_filter)
        if destination_filter in DESTINATIONS:
            conditions.append("d.name = ?")
            values.append(destination_filter)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        reservations = db.execute(
            f"""
            SELECT r.*, u.full_name AS passenger_name, u.email,
                   t.departure_date, t.return_date, d.name AS destination
            FROM reservations r
            JOIN users u ON u.id = r.user_id
            JOIN trips t ON t.id = r.trip_id
            JOIN destinations d ON d.id = t.destination_id
            {where}
            ORDER BY r.created_at DESC, r.id DESC
            """,
            values,
        ).fetchall()
        trips = get_trip_rows("WHERE t.departure_date >= ?", (date.today().isoformat(),))
        buses = db.execute("SELECT * FROM buses ORDER BY plate").fetchall()
        drivers = db.execute(
            "SELECT id, full_name FROM users WHERE role = ? AND active = 1 ORDER BY full_name",
            ("chofer",),
        ).fetchall()
        totals = {
            "reservations": db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],
            "pending": db.execute(
                "SELECT COUNT(*) FROM reservations WHERE status = ?", ("pendiente",)
            ).fetchone()[0],
            "passengers": db.execute(
                "SELECT COALESCE(SUM(passenger_count), 0) FROM reservations "
                "WHERE status != 'cancelado'"
            ).fetchone()[0],
            "buses": db.execute("SELECT COUNT(*) FROM buses").fetchone()[0],
        }
        return render_template(
            "admin.html",
            reservations=reservations,
            trips=trips,
            buses=buses,
            drivers=drivers,
            totals=totals,
            destinations=DESTINATIONS,
            status_filter=status_filter,
            destination_filter=destination_filter,
        )

    @app.post("/administrador/reservas/<int:reservation_id>/estado")
    @authenticated_role("administrador")
    def update_reservation_status(reservation_id):
        new_status = request.form.get("status", "")
        db = get_db()
        reservation = db.execute(
            "SELECT status FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        if (
            reservation is None
            or new_status not in RESERVATION_STATES
            or new_status not in ALLOWED_TRANSITIONS[reservation["status"]]
        ):
            flash("El cambio de estatus solicitado no es válido.", "error")
        else:
            db.execute(
                "UPDATE reservations SET status = ? WHERE id = ?",
                (new_status, reservation_id),
            )
            if new_status == "cancelado":
                db.execute(
                    "DELETE FROM reservation_seats WHERE reservation_id = ?",
                    (reservation_id,),
                )
            db.commit()
            flash("El estatus de la reserva se actualizó.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/administrador/salidas/<int:trip_id>/asignar")
    @authenticated_role("administrador")
    def assign_trip(trip_id):
        bus_value = request.form.get("bus_id", "").strip()
        driver_value = request.form.get("driver_user_id", "").strip()
        try:
            bus_id = int(bus_value) if bus_value else None
            driver_id = int(driver_value) if driver_value else None
        except ValueError:
            flash("La unidad o el chofer seleccionado no es válido.", "error")
            return redirect(url_for("admin_dashboard"))
        db = get_db()
        trip = db.execute(
            "SELECT id, departure_date FROM trips WHERE id = ?", (trip_id,)
        ).fetchone()
        if trip is None:
            abort(404)
        if bus_id is not None:
            bus = db.execute(
                "SELECT capacity, status FROM buses WHERE id = ?", (bus_id,)
            ).fetchone()
            if bus is None or bus["status"] != "Disponible":
                flash("Solo se pueden asignar unidades disponibles.", "error")
                return redirect(url_for("admin_dashboard"))
            reserved = db.execute(
                """
                SELECT COALESCE(SUM(passenger_count), 0) FROM reservations
                WHERE trip_id = ? AND status != 'cancelado'
                """,
                (trip_id,),
            ).fetchone()[0]
            if reserved > bus["capacity"]:
                flash("La unidad no tiene capacidad suficiente para las reservas actuales.", "error")
                return redirect(url_for("admin_dashboard"))
            seat_out_of_range = db.execute(
                """
                SELECT 1
                FROM reservation_seats rs
                JOIN reservations r ON r.id = rs.reservation_id
                WHERE rs.trip_id = ? AND r.status != 'cancelado'
                  AND rs.seat_number > ?
                LIMIT 1
                """,
                (trip_id, bus["capacity"]),
            ).fetchone()
            if seat_out_of_range:
                flash("La unidad no incluye todos los asientos ya seleccionados para esta salida.", "error")
                return redirect(url_for("admin_dashboard"))
            conflict = db.execute(
                "SELECT id FROM trips WHERE departure_date = ? AND bus_id = ? AND id != ?",
                (trip["departure_date"], bus_id, trip_id),
            ).fetchone()
            if conflict:
                flash("Esa unidad ya está asignada a otra salida en esa fecha.", "error")
                return redirect(url_for("admin_dashboard"))
        if driver_id is not None:
            driver = db.execute(
                "SELECT id FROM users WHERE id = ? AND role = ? AND active = 1",
                (driver_id, "chofer"),
            ).fetchone()
            if driver is None:
                flash("Selecciona un chofer activo válido.", "error")
                return redirect(url_for("admin_dashboard"))
            conflict = db.execute(
                """
                SELECT id FROM trips
                WHERE departure_date = ? AND driver_user_id = ? AND id != ?
                """,
                (trip["departure_date"], driver_id, trip_id),
            ).fetchone()
            if conflict:
                flash("Ese chofer ya está asignado a otra salida en esa fecha.", "error")
                return redirect(url_for("admin_dashboard"))
        db.execute(
            "UPDATE trips SET bus_id = ?, driver_user_id = ? WHERE id = ?",
            (bus_id, driver_id, trip_id),
        )
        db.commit()
        flash("La asignación de la salida quedó actualizada.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/administrador/flota")
    @authenticated_role("administrador")
    def manage_fleet():
        db = get_db()
        action = request.form.get("action", "")
        plate = request.form.get("plate", "").strip().upper()
        status = request.form.get("status", "")
        try:
            capacity = int(request.form.get("capacity", ""))
        except ValueError:
            capacity = 0
        if (
            not plate
            or len(plate) > 20
            or not 1 <= capacity <= 100
            or status not in {"Disponible", "Mantenimiento"}
        ):
            flash("Verifica placas, capacidad y estatus de mantenimiento.", "error")
            return redirect(url_for("admin_dashboard"))
        try:
            if action == "add":
                db.execute(
                    "INSERT INTO buses (plate, capacity, status) VALUES (?, ?, ?)",
                    (plate, capacity, status),
                )
            elif action == "update":
                bus_id = int(request.form.get("bus_id", ""))
                current_bus = db.execute(
                    "SELECT id FROM buses WHERE id = ?", (bus_id,)
                ).fetchone()
                if current_bus is None:
                    flash("No se encontró la unidad que intentas actualizar.", "error")
                    return redirect(url_for("admin_dashboard"))
                if status == "Mantenimiento":
                    assigned = db.execute(
                        """
                        SELECT id FROM trips
                        WHERE bus_id = ? AND departure_date >= ?
                        LIMIT 1
                        """,
                        (bus_id, date.today().isoformat()),
                    ).fetchone()
                    if assigned:
                        flash(
                            "Desasigna la unidad de sus próximas salidas antes de enviarla a mantenimiento.",
                            "error",
                        )
                        return redirect(url_for("admin_dashboard"))
                over_capacity = db.execute(
                    """
                    SELECT t.id
                    FROM trips t
                    JOIN reservations r ON r.trip_id = t.id AND r.status != 'cancelado'
                    WHERE t.bus_id = ? AND t.departure_date >= ?
                    GROUP BY t.id
                    HAVING SUM(r.passenger_count) > ?
                    LIMIT 1
                    """,
                    (bus_id, date.today().isoformat(), capacity),
                ).fetchone()
                if over_capacity:
                    flash(
                        "La capacidad no puede ser menor que las reservas ya asignadas a esta unidad.",
                        "error",
                    )
                    return redirect(url_for("admin_dashboard"))
                seat_out_of_range = db.execute(
                    """
                    SELECT 1
                    FROM trips t
                    JOIN reservation_seats rs ON rs.trip_id = t.id
                    JOIN reservations r ON r.id = rs.reservation_id
                    WHERE t.bus_id = ? AND t.departure_date >= ?
                      AND r.status != 'cancelado' AND rs.seat_number > ?
                    LIMIT 1
                    """,
                    (bus_id, date.today().isoformat(), capacity),
                ).fetchone()
                if seat_out_of_range:
                    flash(
                        "La capacidad no puede excluir asientos elegidos en próximas salidas.",
                        "error",
                    )
                    return redirect(url_for("admin_dashboard"))
                cursor = db.execute(
                    "UPDATE buses SET plate = ?, capacity = ?, status = ? WHERE id = ?",
                    (plate, capacity, status, bus_id),
                )
                if cursor.rowcount == 0:
                    flash("No se encontró la unidad que intentas actualizar.", "error")
                    return redirect(url_for("admin_dashboard"))
            else:
                flash("La operación de flota no es válida.", "error")
                return redirect(url_for("admin_dashboard"))
            db.commit()
        except (sqlite3.IntegrityError, ValueError):
            db.rollback()
            flash("No se guardó la unidad. Revisa que la placa no esté duplicada.", "error")
        else:
            flash("La flota se actualizó.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("error.html", code=403, message="No tienes permiso para ver esta sección."), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="No encontramos la página solicitada."), 404

    @app.errorhandler(400)
    def bad_request(error):
        message = getattr(error, "description", "La solicitud no es válida.")
        return render_template("error.html", code=400, message=message), 400

    return app


app = create_app()


if __name__ == "__main__":
    app.run()
