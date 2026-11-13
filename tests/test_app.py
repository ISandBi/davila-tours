import io
import re
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from app import create_app, initialize_database


class DavilaToursTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp_dir.name) / "test.sqlite3")
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": self.database,
                "PROFILE_PHOTO_FOLDER": str(Path(self.temp_dir.name) / "profile_photos"),
                "DEMO_PASSWORD": "TestPassword2026!",
                "DEMO_ADMIN_EMAIL": "admin@gmail.com",
                "QA_ACCESS_TOKEN": "test-qa-token",
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf_token(self, page):
        response = self.client.get(page)
        self.assertEqual(response.status_code, 200)
        match = re.search(
            rb'name="csrf_token" value="([^"]+)"', response.data
        )
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def email_domain_id(self, domain):
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT id FROM email_domains WHERE domain = ?", (domain,)
            ).fetchone()
        finally:
            connection.close()
        return str(row[0])

    def login_as(self, local_part, role="cliente"):
        token = self.csrf_token("/login")
        response = self.client.post(
            "/login",
            data={
                "csrf_token": token,
                "email_local": local_part,
                "email_domain": self.email_domain_id("gmail.com"),
                "password": "TestPassword2026!",
            },
        )
        self.assertEqual(response.status_code, 302)
        return response

    def register_client(self, local_part):
        response = self.client.post(
            "/registro",
            data={
                "csrf_token": self.csrf_token("/registro"),
                "full_name": "Pasajero de Prueba",
                "email_local": local_part,
                "email_domain": self.email_domain_id("gmail.com"),
                "phone": "5512345678",
                "password": "TestPassword2026!",
            },
        )
        self.assertEqual(response.status_code, 302)
        return response

    def test_email_domains_are_loaded_from_database(self):
        response = self.client.get("/registro")
        self.assertEqual(response.status_code, 200)
        for domain in (b"gmail.com", b"hotmail.com", b"outlook.com"):
            self.assertIn(domain, response.data)
        self.assertIn(b"@</span>", response.data)

    def test_site_branding_uses_logo_for_header_and_favicon(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        logo_url = "/static/img/logo.png"
        self.assertIn(b'rel="icon" type="image/png"', response.data)
        self.assertIn(logo_url.encode(), response.data)
        self.assertIn(b'class="brand-logo"', response.data)

        logo_response = self.client.get(logo_url)
        self.assertEqual(logo_response.status_code, 200)
        self.assertEqual(logo_response.mimetype, "image/png")
        logo_response.close()

    def test_homepage_shows_company_sections_and_requested_images(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Nuestra misi", response.data)
        self.assertIn(b"Nuestra visi", response.data)
        self.assertIn(b"equipo profesional", response.data)
        self.assertIn(b"class=\"page-loader\"", response.data)
        loader_markup = re.search(
            rb'<div class="loader-scene".*?</div>\s*</div>',
            response.data,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(loader_markup)
        self.assertNotIn(b"<img", loader_markup.group(0))
        self.assertIn(b"loader-bus-wheel", loader_markup.group(0))
        self.assertIn(b"D\xc3\x81VILA TOURS", loader_markup.group(0))
        self.assertIn(b"TURISMO Y VIAJES ESPECIALES", loader_markup.group(0))
        self.assertIn(b"loader-bus-stripe-green", loader_markup.group(0))
        for filename in (
            "Equipo-Davila-Tours.jpeg",
            "Instalaciones-Davila-Tours.jpeg",
            "Davila-Tours-Autobus-Imagen1.jpeg",
        ):
            image_response = self.client.get(f"/static/img/{filename}")
            self.assertEqual(image_response.status_code, 200)
            self.assertEqual(image_response.mimetype, "image/jpeg")
            image_response.close()

    def test_client_registration_and_booking_create_pending_folio(self):
        token = self.csrf_token("/registro")
        response = self.client.post(
            "/registro",
            data={
                "csrf_token": token,
                "full_name": "Pasajero de Prueba",
                "email_local": "pasajero",
                "email_domain": self.email_domain_id("gmail.com"),
                "phone": "5512345678",
                "password": "TestPassword2026!",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/cliente")

        connection = sqlite3.connect(self.database)
        try:
            trip_id, departure_date, destination = connection.execute(
                """
                SELECT t.id, t.departure_date, d.name
                FROM trips t JOIN destinations d ON d.id = t.destination_id
                ORDER BY t.departure_date LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
        booking_url = f"/reservar?destino={destination}&salida={departure_date}"
        booking_token = self.csrf_token(booking_url)
        response = self.client.post(
            "/reservar",
            data={
                "csrf_token": booking_token,
                "trip_id": str(trip_id),
                "passenger_count": "2",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertRegex(response.headers["Location"], r"/cliente/reservas/DT-[A-F0-9]{8}$")
        detail = self.client.get(response.headers["Location"])
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Pendiente", detail.data)
        self.assertIn(b"Por confirmar", detail.data)
        self.assertIn(b"2", detail.data)

        booking_token = self.csrf_token(booking_url)
        response = self.client.post(
            "/reservar",
            data={
                "csrf_token": booking_token,
                "trip_id": str(trip_id),
                "passenger_count": "21",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"<strong>21</strong>", self.client.get(response.headers["Location"]).data)

    def test_public_calendar_filters_destination_and_shows_only_thursday_round_trips(self):
        response = self.client.get("/reservar?destino=Oaxaca")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Calendario de salidas".encode(), response.data)
        self.assertIn(b"Oaxaca", response.data)
        self.assertIn(b"jueves y regreso en domingo", response.data)

        connection = sqlite3.connect(self.database)
        try:
            trips = connection.execute(
                """
                SELECT t.departure_date, t.return_date
                FROM trips t JOIN destinations d ON d.id = t.destination_id
                WHERE d.name = ?
                """,
                ("Oaxaca",),
            ).fetchall()
        finally:
            connection.close()
        self.assertTrue(trips)
        for departure, return_date in trips:
            self.assertEqual(date.fromisoformat(departure).weekday(), 3)
            self.assertEqual(
                date.fromisoformat(return_date) - date.fromisoformat(departure),
                timedelta(days=3),
            )

    def test_seat_selection_is_saved_and_canceled_seats_are_released(self):
        self.register_client("seatpassenger")
        connection = sqlite3.connect(self.database)
        try:
            trip_id, departure_date, destination = connection.execute(
                """
                SELECT t.id, t.departure_date, d.name
                FROM trips t JOIN destinations d ON d.id = t.destination_id
                ORDER BY t.departure_date LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()

        booking_url = f"/reservar?destino={destination}&salida={departure_date}"
        token = self.csrf_token(booking_url)
        booking_page = self.client.get(booking_url)
        self.assertIn(b"CHOFER", booking_page.data)
        self.assertIn(b"Asiento 1", booking_page.data)
        self.assertIn(b'class="bus-front"', booking_page.data)
        self.assertIn(b'class="bus-windshield"', booking_page.data)
        self.assertIn(b'class="seat-visual"', booking_page.data)
        self.assertIn(b'class="seat-back"', booking_page.data)
        self.assertIn(b'class="seat-cushion"', booking_page.data)
        self.assertIn(b'class="driver-person"', booking_page.data)
        response = self.client.post(
            "/reservar",
            data={
                "csrf_token": token,
                "trip_id": str(trip_id),
                "passenger_count": "1",
                "seat_numbers": "4",
            },
        )
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(response.headers["Location"])
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Pendiente", detail.data)
        self.assertIn(b"<strong>4</strong>", detail.data)
        folio = response.headers["Location"].rsplit("/", 1)[-1]

        connection = sqlite3.connect(self.database)
        try:
            saved_seat = connection.execute(
                """
                SELECT rs.seat_number FROM reservation_seats rs
                JOIN reservations r ON r.id = rs.reservation_id
                WHERE r.folio = ?
                """,
                (folio,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(saved_seat, (4,))

        unavailable_page = self.client.get(booking_url)
        self.assertIn(
            b'value="4" aria-label="Asiento 4" disabled',
            unavailable_page.data,
        )
        self.assertIn(b'class="seat-person"', unavailable_page.data)
        conflict = self.client.post(
            "/reservar",
            data={
                "csrf_token": token,
                "trip_id": str(trip_id),
                "passenger_count": "1",
                "seat_numbers": "4",
            },
            follow_redirects=True,
        )
        self.assertIn(b"asientos ya no est", conflict.data)

        cancel_token = self.csrf_token(response.headers["Location"])
        canceled = self.client.post(
            f"/cliente/reservas/{folio}/cancelar",
            data={"csrf_token": cancel_token},
        )
        self.assertEqual(canceled.status_code, 302)
        available_page = self.client.get(booking_url)
        self.assertRegex(
            available_page.data,
            rb'value="4" aria-label="Asiento 4"\s*>',
        )

    def test_existing_reservations_receive_seats_when_database_initializes(self):
        self.register_client("legacyseat")
        connection = sqlite3.connect(self.database)
        try:
            user_id = connection.execute(
                "SELECT id FROM users WHERE email = ?",
                ("legacyseat@gmail.com",),
            ).fetchone()[0]
            trip_id = connection.execute(
                "SELECT id FROM trips ORDER BY departure_date LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO reservations
                    (folio, user_id, trip_id, passenger_count, amount_cents, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("DT-LEGACY01", user_id, trip_id, 2, 0, "pendiente"),
            )
            connection.commit()
        finally:
            connection.close()

        with self.app.app_context():
            initialize_database()

        connection = sqlite3.connect(self.database)
        try:
            seats = connection.execute(
                """
                SELECT seat_number FROM reservation_seats
                WHERE reservation_id = (
                    SELECT id FROM reservations WHERE folio = ?
                )
                ORDER BY seat_number
                """,
                ("DT-LEGACY01",),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(seats, [(1,), (2,)])

    def test_role_endpoints_reject_other_profiles(self):
        with self.client.session_transaction() as session:
            session["user"] = {
                "id": 1,
                "full_name": "Pasajero",
                "email": "pasajero@gmail.com",
                "role": "cliente",
            }
        response = self.client.get("/administrador")
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/chofer")
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/microbusero")
        self.assertEqual(response.status_code, 403)
        hub_response = self.client.get("/pruebas")
        self.assertEqual(hub_response.status_code, 200)
        self.assertIn(b"Centro de pruebas", hub_response.data)
        self.assertIn(b"/cliente", hub_response.data)
        self.assertNotIn(b"/administrador", hub_response.data)
        self.assertNotIn(b">Pruebas</a>", hub_response.data)

    def test_test_hub_requires_login(self):
        response = self.client.get("/pruebas")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/pruebas")

    def test_qa_access_requires_token_and_allows_profile_selection(self):
        response = self.client.get("/qa")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Token de acceso", response.data)

        response = self.client.post(
            "/qa",
            data={
                "csrf_token": self.csrf_token("/qa"),
                "action": "select_profile",
                "role": "administrador",
            },
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            "/qa",
            data={"csrf_token": self.csrf_token("/qa"), "token": "wrong-token"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"El token no es v\xc3\xa1lido", response.data)

        response = self.client.post(
            "/qa",
            data={"csrf_token": self.csrf_token("/qa"), "token": "test-qa-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Selecciona un perfil", response.data)

        response = self.client.post(
            "/qa",
            data={
                "csrf_token": self.csrf_token("/qa"),
                "action": "select_profile",
                "role": "cliente",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/pruebas")
        with self.client.session_transaction() as session:
            self.assertEqual(session["user"]["role"], "cliente")
            self.assertTrue(session["qa_access"])
        self.assertEqual(self.client.get("/cliente").status_code, 200)
        self.assertEqual(self.client.get("/administrador").status_code, 403)

        profiles = (("chofer", "/chofer"), ("administrador", "/administrador"))
        for role, endpoint in profiles:
            response = self.client.post(
                "/qa",
                data={
                    "csrf_token": self.csrf_token("/qa"),
                    "action": "select_profile",
                    "role": role,
                },
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.client.get(endpoint).status_code, 200)

    def test_qa_access_is_disabled_without_configured_token(self):
        unconfigured_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": str(Path(self.temp_dir.name) / "unconfigured.sqlite3"),
                "QA_ACCESS_TOKEN": "",
            }
        )
        response = unconfigured_app.test_client().get("/qa")
        self.assertEqual(response.status_code, 503)

    def test_chofer_dashboard_and_legacy_route_keep_role_protection(self):
        with self.client.session_transaction() as session:
            session["user"] = {
                "id": 2,
                "full_name": "Chofer de Prueba",
                "email": "chofer1@gmail.com",
                "role": "chofer",
            }
        response = self.client.get("/chofer")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Hola, Chofer de Prueba".encode(), response.data)
        legacy_response = self.client.get("/microbusero")
        self.assertEqual(legacy_response.status_code, 301)
        self.assertEqual(legacy_response.headers["Location"], "/chofer")

    def test_profile_is_available_for_passengers_and_drivers(self):
        self.register_client("profilepassenger")
        passenger_profile = self.client.get("/perfil")
        self.assertEqual(passenger_profile.status_code, 200)
        self.assertIn(b"Fotograf\xc3\xada de perfil", passenger_profile.data)

        driver_client = self.app.test_client()
        token = re.search(rb'name="csrf_token" value="([^"]+)"', driver_client.get("/login").data)
        self.assertIsNotNone(token)
        driver_client.post(
            "/login",
            data={
                "csrf_token": token.group(1).decode(),
                "email_local": "chofer1",
                "email_domain": self.email_domain_id("gmail.com"),
                "password": "TestPassword2026!",
            },
        )
        driver_profile = driver_client.get("/perfil")
        self.assertEqual(driver_profile.status_code, 200)
        self.assertIn(b"Chofer 1", driver_profile.data)

    def test_profile_photo_is_validated_normalized_and_served_to_its_owner(self):
        self.register_client("photopassenger")
        image = io.BytesIO()
        Image.new("RGB", (256, 256), "#B8C8D0").save(image, format="PNG")
        image.seek(0)
        token = self.csrf_token("/perfil")
        with patch("app.image_contains_face", return_value=True):
            response = self.client.post(
                "/perfil",
                data={
                    "csrf_token": token,
                    "profile_photo": (image, "profile.png"),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Tu fotograf\xc3\xada de perfil se actualiz\xc3\xb3", response.data)
        photo_response = self.client.get("/perfil/foto")
        self.assertEqual(photo_response.status_code, 200)
        self.assertEqual(photo_response.mimetype, "image/jpeg")
        self.assertEqual(photo_response.data[:2], b"\xff\xd8")
        photo_response.close()

        connection = sqlite3.connect(self.database)
        try:
            photo_name = connection.execute(
                "SELECT profile_photo FROM users WHERE email = ?",
                ("photopassenger@gmail.com",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertRegex(photo_name, r"^[a-f0-9]{32}\.jpg$")
        self.assertTrue(
            (Path(self.app.config["PROFILE_PHOTO_FOLDER"]) / photo_name).is_file()
        )
        self.assertEqual(self.app.test_client().get("/perfil/foto").status_code, 302)

    def test_profile_photo_rejects_image_without_face(self):
        self.register_client("nofacepassenger")
        image = io.BytesIO()
        Image.new("RGB", (256, 256), "white").save(image, format="JPEG")
        image.seek(0)
        response = self.client.post(
            "/perfil",
            data={
                "csrf_token": self.csrf_token("/perfil"),
                "profile_photo": (image, "landscape.jpg"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"debe mostrar un rostro", response.data)
        connection = sqlite3.connect(self.database)
        try:
            photo_name = connection.execute(
                "SELECT profile_photo FROM users WHERE email = ?",
                ("nofacepassenger@gmail.com",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIsNone(photo_name)
        self.assertEqual(
            list(Path(self.app.config["PROFILE_PHOTO_FOLDER"]).iterdir()), []
        )

    def test_legacy_driver_role_is_migrated_without_losing_trip_assignments(self):
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            CREATE TABLE users_legacy (
                id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL CHECK (
                    role IN ('cliente', 'microbusero', 'administrador')
                ),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO users_legacy
                (id, full_name, email, password_hash, phone, role, active, created_at)
            SELECT id, full_name, email, password_hash, phone,
                   CASE role WHEN 'chofer' THEN 'microbusero' ELSE role END,
                   active, created_at
            FROM users;
            DROP TABLE users;
            ALTER TABLE users_legacy RENAME TO users;
            """
        )
        driver_id = connection.execute(
            "SELECT id FROM users WHERE role = 'microbusero' LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "UPDATE trips SET driver_user_id = ? WHERE id = "
            "(SELECT id FROM trips ORDER BY id LIMIT 1)",
            (driver_id,),
        )
        connection.commit()
        connection.close()

        migrated_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": self.database,
                "DEMO_PASSWORD": "TestPassword2026!",
                "DEMO_ADMIN_EMAIL": "admin@gmail.com",
            }
        )
        with migrated_app.app_context():
            connection = sqlite3.connect(self.database)
            try:
                role = connection.execute(
                    "SELECT role FROM users WHERE id = ?", (driver_id,)
                ).fetchone()[0]
                assigned_driver = connection.execute(
                    "SELECT driver_user_id FROM trips "
                    "WHERE driver_user_id = ? LIMIT 1",
                    (driver_id,),
                ).fetchone()[0]
                foreign_key_errors = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(users)")
                }
            finally:
                connection.close()
        self.assertEqual(role, "chofer")
        self.assertEqual(assigned_driver, driver_id)
        self.assertEqual(foreign_key_errors, [])
        self.assertIn("profile_photo", columns)

    def test_administrator_dashboard_renders_and_has_separate_endpoint(self):
        with self.client.session_transaction() as session:
            session["user"] = {
                "id": 1,
                "full_name": "Administrador Dávila",
                "email": "admin@gmail.com",
                "role": "administrador",
            }
        response = self.client.get("/administrador")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Panel de administraci", response.data)
        self.assertIn("Gestión de flota".encode(), response.data)

    def test_administrator_can_assign_a_unit_and_driver(self):
        connection = sqlite3.connect(self.database)
        try:
            trip_id = connection.execute(
                "SELECT id FROM trips ORDER BY departure_date LIMIT 1"
            ).fetchone()[0]
            bus_id = connection.execute(
                "SELECT id FROM buses WHERE status = 'Disponible' LIMIT 1"
            ).fetchone()[0]
            driver_id = connection.execute(
                "SELECT id FROM users WHERE role = 'chofer' LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()
        with self.client.session_transaction() as session:
            session["user"] = {
                "id": 1,
                "full_name": "Administrador Dávila",
                "email": "admin@gmail.com",
                "role": "administrador",
            }
        token = self.csrf_token("/administrador")
        response = self.client.post(
            f"/administrador/salidas/{trip_id}/asignar",
            data={
                "csrf_token": token,
                "bus_id": str(bus_id),
                "driver_user_id": str(driver_id),
            },
        )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.database)
        try:
            assignment = connection.execute(
                "SELECT bus_id, driver_user_id FROM trips WHERE id = ?", (trip_id,)
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(assignment, (bus_id, driver_id))

    def test_sql_injection_attempt_is_not_accepted_as_email(self):
        token = self.csrf_token("/login")
        response = self.client.post(
            "/login",
            data={
                "csrf_token": token,
                "email_local": "x' OR 1=1--",
                "email_domain": self.email_domain_id("gmail.com"),
                "password": "anything",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"parte del correo", response.data)
        self.assertNotIn(b"Panel de administraci", response.data)


if __name__ == "__main__":
    unittest.main()
