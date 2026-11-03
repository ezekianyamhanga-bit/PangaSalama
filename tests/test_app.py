import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from io import BytesIO
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

TEST_DATA = tempfile.TemporaryDirectory(prefix="pangasalama-tests-")
os.environ["APP_ENV"] = "test"
os.environ["ADMIN_PASSWORD"] = "testing-secret"
os.environ["SESSION_SECRET"] = "session-testing-secret"
os.environ["DATABASE_PATH"] = os.path.join(TEST_DATA.name, "pangasalama.sqlite3")
os.environ["SECURE_COOKIES"] = "false"
os.environ["PRIMARY_NOTIFICATION_EMAIL"] = ""
os.environ["BACKUP_NOTIFICATION_EMAIL"] = ""
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["EMAIL_FROM_ADDRESS"] = ""
os.environ["IMAGE_STORAGE_BACKEND"] = "local"
os.environ["LOCAL_IMAGE_STORAGE_PATH"] = os.path.join(TEST_DATA.name, "private_uploads")

import app as app_module
import storage

from app import (ADMIN_SESSION_IDLE_SECONDS, MAX_LISTING_REQUEST_BYTES,
                 SESSION_MAX_AGE_SECONDS, UploadBodyLimitMiddleware, app, init_db,
                 validate_settings)
from db_maintenance import backup_database, restore_database, verify_database


client = TestClient(app)
DATABASE = os.environ["DATABASE_PATH"]


@contextmanager
def database_connection():
    with closing(sqlite3.connect(DATABASE)) as connection:
        with connection:
            yield connection


@pytest.fixture(autouse=True)
def clear_test_database():
    with database_connection() as connection:
        for table in ("listing_photos", "listings", "requests", "notifications", "admin_audit",
                      "public_rate_limits", "admin_login_attempts", "admin_sessions"):
            connection.execute(f"DELETE FROM {table}")


def get_token(path="/"):
    response = client.get(path)
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def valid_listing(csrf, **overrides):
    data = {
        "csrf": csrf, "owner_name": "Test Owner", "owner_phone": "0712345678",
        "provider_type": "owner", "region": "Dodoma", "area": "Nzuguni",
        "property_type": "Studio", "rent": "150000", "bedrooms": "1",
        "advance_months": "3", "extra_fee": "10000", "description": "Test listing",
        "consent": "yes",
    }
    data.update(overrides)
    return data


def login_as_admin():
    token = get_token("/admin")
    response = client.post("/admin/login", data={"csrf": token, "password": "testing-secret"},
                           follow_redirects=False)
    assert response.status_code == 303
    return get_token("/admin")


def test_public_pages_and_admin_login_page():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert "Usimamizi wa PangaSalama" in client.get("/admin").text


def test_listing_requires_approval_and_remains_searchable_only_after_publish():
    token = get_token()
    result = client.post("/submit/listing", data=valid_listing(token))
    assert result.status_code == 200
    assert "Tangazo limepokelewa" in result.text
    assert "Studio — Nzuguni" not in client.get("/").text
    with database_connection() as connection:
        listing_id = connection.execute(
            "SELECT id FROM listings WHERE status='pending'").fetchone()[0]

    assert client.post("/admin/status", data={
        "csrf": get_token(), "listing_id": listing_id, "status": "published",
    }).status_code == 403
    admin_token = login_as_admin()
    assert client.post("/admin/status", data={
        "csrf": admin_token, "listing_id": listing_id, "status": "published",
    }, follow_redirects=False).status_code == 303
    assert "Studio — Nzuguni" in client.get("/").text


def test_unapproved_viewing_is_blocked():
    response = client.post("/submit/request", data={
        "csrf": get_token(), "kind": "viewing", "name": "Tester", "phone": "0712345678",
        "listing_id": "INVALID", "consent": "yes",
    })
    assert response.status_code == 404


def test_csrf_protects_public_and_admin_post_routes():
    response = client.post("/submit/listing", data=valid_listing("invalid-token"))
    assert response.status_code == 403
    with TestClient(app) as fresh_client:
        response = fresh_client.post("/submit/listing", data=valid_listing(""))
    assert response.status_code == 422

    admin_token = login_as_admin()
    response = client.post("/admin/logout", data={"csrf": "invalid-token"})
    assert response.status_code == 403
    response = client.post("/admin/status", data={
        "csrf": admin_token, "listing_id": "missing", "status": "published",
    })
    assert response.status_code == 404


def test_admin_login_is_throttled_after_five_failures():
    token = get_token("/admin")
    for _ in range(5):
        response = client.post("/admin/login", data={"csrf": token, "password": "incorrect"})
        assert response.status_code == 401
    response = client.post("/admin/login", data={"csrf": token, "password": "incorrect"})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


def test_admin_session_idle_expiry_and_logout(monkeypatch):
    current_time = [1_000_000]
    monkeypatch.setattr("app.time.time", lambda: current_time[0])
    token = login_as_admin()
    assert "Dashibodi ya msimamizi" in client.get("/admin").text

    current_time[0] += ADMIN_SESSION_IDLE_SECONDS + 1
    assert "Usimamizi wa PangaSalama" in client.get("/admin").text
    assert client.post("/admin/logout", data={"csrf": token}).status_code == 403

    token = login_as_admin()
    response = client.post("/admin/logout", data={"csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    assert "Usimamizi wa PangaSalama" in client.get("/admin").text
    middleware = app.user_middleware[0]
    assert middleware.kwargs["max_age"] == SESSION_MAX_AGE_SECONDS


def test_logout_revokes_copied_admin_cookie():
    token = login_as_admin()
    copied_cookie = client.cookies.get("session")
    assert copied_cookie

    with TestClient(app) as replay_client:
        replay_client.cookies.set("session", copied_cookie)
        assert "Dashibodi ya msimamizi" in replay_client.get("/admin").text

        assert client.post("/admin/logout", data={"csrf": token},
                           follow_redirects=False).status_code == 303

        replay_client.cookies.set("session", copied_cookie)
        assert "Usimamizi wa PangaSalama" in replay_client.get("/admin").text
        replay_client.cookies.set("session", copied_cookie)
        response = replay_client.post("/admin/status", data={
            "csrf": token, "listing_id": "PSL-TEST", "status": "published",
        })
        assert response.status_code == 403

    with database_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0


def test_admin_session_absolute_expiry(monkeypatch):
    current_time = [2_000_000]
    monkeypatch.setattr("app.time.time", lambda: current_time[0])
    login_as_admin()
    current_time[0] += SESSION_MAX_AGE_SECONDS + 1
    assert "Usimamizi wa PangaSalama" in client.get("/admin").text
    with database_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM admin_sessions WHERE expires_at > ?",
            (current_time[0],),
        ).fetchone()[0] == 0
    login_as_admin()
    with database_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 1


def test_admin_listing_changes_are_audited():
    with database_connection() as connection:
        connection.execute("""INSERT INTO listings
            (id,created_at,owner_name,owner_phone,provider_type,region,area,property_type,rent,
             bedrooms,advance_months,extra_fee,description)
             VALUES('PSL-TEST','2026-01-01T00:00:00+00:00','Owner','0712345678','owner',
             'Dodoma','Nzuguni','Studio',150000,1,3,10000,'Test')""")

    token = login_as_admin()
    assert client.post("/admin/status", data={
        "csrf": token, "listing_id": "PSL-TEST", "status": "published",
    }, follow_redirects=False).status_code == 303
    token = get_token("/admin")
    assert client.post("/admin/listing-pricing", data={
        "csrf": token, "listing_id": "PSL-TEST", "rent": "175000",
        "advance_months": "4", "extra_fee": "12000",
    }, follow_redirects=False).status_code == 303
    token = get_token("/admin")
    assert client.post("/admin/listing-verification", data={
        "csrf": token, "listing_id": "PSL-TEST", "status": "authority_documents_reviewed",
        "note": "Test review only",
    }, follow_redirects=False).status_code == 303

    with database_connection() as connection:
        rows = connection.execute(
            "SELECT actor,action,target_id,details FROM admin_audit ORDER BY id").fetchall()
    assert [row[1] for row in rows] == [
        "listing_status_changed", "listing_pricing_changed", "listing_verification_reviewed"]
    assert all(row[0] == "admin" and row[2] == "PSL-TEST" for row in rows)
    assert json.loads(rows[0][3]) == {"from": "pending", "to": "published"}
    assert json.loads(rows[1][3])["to"] == {
        "rent": 175000, "advance_months": 4, "extra_fee": 12000,
    }


def test_public_listing_and_request_forms_are_rate_limited():
    token = get_token()
    for _ in range(5):
        assert client.post("/submit/listing", data=valid_listing(token)).status_code == 200
    response = client.post("/submit/listing", data=valid_listing(token))
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0

    for _ in range(10):
        assert client.post("/submit/request", data={
            "csrf": token, "kind": "home_search", "name": "Tester",
            "phone": "0712345678", "consent": "yes",
        }).status_code == 200
    response = client.post("/submit/request", data={
        "csrf": token, "kind": "home_search", "name": "Tester",
        "phone": "0712345678", "consent": "yes",
    })
    assert response.status_code == 429


def test_listing_photos_are_private_until_listing_and_photo_are_approved():
    image = BytesIO()
    Image.new("RGB", (64, 48), "green").save(image, format="JPEG")
    image_bytes = image.getvalue()
    response = client.post("/submit/listing", data=valid_listing(get_token(), title="Nyumba ya majaribio"),
                           files=[("photos", ("room.jpg", image_bytes, "image/jpeg"))])
    assert response.status_code == 200
    with database_connection() as connection:
        listing_id, photo_id, storage_key, thumbnail_key = connection.execute(
            """SELECT l.id,p.id,p.storage_key,p.thumbnail_key FROM listings l
               JOIN listing_photos p ON p.listing_id=l.id WHERE l.title=?""",
            ("Nyumba ya majaribio",)).fetchone()

    with TestClient(app) as public_client:
        assert public_client.get(f"/media/{photo_id}").status_code == 404
    login_as_admin()
    assert client.get(f"/media/{photo_id}").status_code == 200
    token = get_token("/admin")
    assert client.post("/admin/status", data={
        "csrf": token, "listing_id": listing_id, "status": "published",
    }, follow_redirects=False).status_code == 303
    with TestClient(app) as public_client:
        assert public_client.get(f"/media/{photo_id}").status_code == 404
    token = get_token("/admin")
    assert client.post("/admin/photo-status", data={
        "csrf": token, "photo_id": photo_id, "status": "approved",
    }, follow_redirects=False).status_code == 303

    public_image = client.get(f"/media/{photo_id}")
    assert public_image.status_code == 200
    assert public_image.headers["content-type"] == "image/webp"
    assert public_image.headers["x-content-type-options"] == "nosniff"
    assert Image.open(BytesIO(public_image.content)).format == "WEBP"
    thumbnail_response = client.get(f"/media/{photo_id}?variant=thumb")
    assert thumbnail_response.status_code == 200
    with Image.open(BytesIO(thumbnail_response.content)) as thumbnail:
        assert thumbnail.size[0] <= 480 and thumbnail.size[1] <= 360
    assert client.get(f"/media/{photo_id}?variant=invalid").status_code == 422
    assert "/media/" + photo_id in client.get(f"/property/{listing_id}").text

    replacement = BytesIO()
    Image.new("RGB", (48, 48), "blue").save(replacement, format="PNG")
    token = get_token("/admin")
    assert client.post("/admin/photo-replace", data={
        "csrf": token, "photo_id": photo_id,
    }, files={"image": ("replace.png", replacement.getvalue(), "image/png")},
       follow_redirects=False).status_code == 303
    with TestClient(app) as public_client:
        assert public_client.get(f"/media/{photo_id}").status_code == 404
    with database_connection() as connection:
        assert connection.execute("SELECT status FROM listing_photos WHERE id=?",
                                  (photo_id,)).fetchone()[0] == "pending"
    assert not (Path(os.environ["LOCAL_IMAGE_STORAGE_PATH"]) / storage_key).exists()
    assert not (Path(os.environ["LOCAL_IMAGE_STORAGE_PATH"]) / thumbnail_key).exists()

    token = get_token("/admin")
    assert client.post("/admin/photo-delete", data={
        "csrf": token, "photo_id": photo_id,
    }, follow_redirects=False).status_code == 303
    assert client.get(f"/media/{photo_id}").status_code == 404


def test_photo_upload_rejects_malicious_and_oversized_content(monkeypatch):
    token = get_token()
    malicious = client.post("/submit/listing", data=valid_listing(token),
                            files=[("photos", ("bad.jpg", b"not an image", "image/jpeg"))])
    assert malicious.status_code == 422

    assert storage.MAX_IMAGE_BYTES == 5 * 1024 * 1024
    monkeypatch.setattr(storage, "MAX_IMAGE_BYTES", 100)
    oversized = client.post("/submit/listing", data=valid_listing(token),
                            files=[("photos", ("large.jpg", b"x" * 101, "image/jpeg"))])
    assert oversized.status_code == 422
    with database_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_upload_body_limit_rejects_oversized_request_before_form_parsing():
    response = client.post("/submit/listing", content=b"", headers={
        "content-length": str(MAX_LISTING_REQUEST_BYTES + 1),
    })
    assert response.status_code == 413


def test_upload_body_limit_rejects_oversized_chunked_request(monkeypatch):
    monkeypatch.setattr(app_module, "MAX_LISTING_REQUEST_BYTES", 3)
    messages = iter([
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
    ])
    responses = []

    async def receive():
        return next(messages)

    async def send(message):
        responses.append(message)

    async def downstream(scope, limited_receive, send_response):
        while (await limited_receive()).get("more_body"):
            pass

    scope = {
        "type": "http", "method": "POST", "path": "/submit/listing",
        "headers": [], "asgi": {"version": "3.0"},
    }
    asyncio.run(UploadBodyLimitMiddleware(downstream)(scope, receive, send))
    assert responses[0]["status"] == 413


def test_s3_storage_uses_private_objects_and_safe_keys(monkeypatch):
    class FakeBody:
        def read(self):
            return b"optimized-image"

        def close(self):
            pass

    class FakeS3:
        missing = False

        def put_object(self, **kwargs):
            assert kwargs["Bucket"] == "private-test-bucket"
            assert kwargs["ContentType"] == "image/webp"
            assert kwargs["CacheControl"] == "private, no-store"
            assert "ACL" not in kwargs
            self.key = kwargs["Key"]

        def get_object(self, **kwargs):
            assert kwargs["Key"] == self.key
            if self.missing:
                from botocore.exceptions import ClientError
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {"Body": FakeBody()}

        def delete_object(self, **kwargs):
            assert kwargs["Key"] == self.key

    fake = FakeS3()
    monkeypatch.setattr(storage, "_s3_client", lambda: fake)
    monkeypatch.setenv("IMAGE_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "private-test-bucket")
    key = storage.store_image(b"optimized-image")
    assert key.startswith("property-images/")
    assert storage.read_image(key) == b"optimized-image"
    storage.delete_image(key)
    with pytest.raises(ValueError):
        storage.read_image("property-images/../private.webp")
    with pytest.raises(ValueError):
        storage.delete_image("property-images/..\\private.webp")
    with pytest.raises(ValueError):
        storage.read_image("property-images/subfolder/private.webp")
    fake.missing = True
    with pytest.raises(FileNotFoundError):
        storage.read_image(key)


def test_request_tracking_token_is_private_and_admin_can_update_status():
    response = client.post("/submit/request", data={
        "csrf": get_token(), "kind": "home_search", "name": "Tester",
        "phone": "0712345678", "consent": "yes",
    })
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    reference = re.search(r"PSR-[A-F0-9]{8}", response.text).group()
    tracking_token = re.search(r"Hifadhi msimbo huu wa siri .*?: <code>([^<]+)", response.text).group(1)
    assert client.post("/track", data={
        "csrf": get_token(), "reference": reference, "tracking_token": "wrong-token",
    }).status_code == 404

    response = client.post("/track", data={
        "csrf": get_token(), "reference": reference, "tracking_token": tracking_token,
    })
    assert response.status_code == 200
    assert "new" in response.text

    admin_token = login_as_admin()
    assert client.post("/admin/request-status", data={
        "csrf": admin_token, "request_id": reference, "status": "contacted",
    }, follow_redirects=False).status_code == 303
    response = client.post("/track", data={
        "csrf": get_token(), "reference": reference, "tracking_token": tracking_token,
    })
    assert "contacted" in response.text


def test_notification_retry_requires_admin_and_records_delivery(monkeypatch):
    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert host == "mail.example.test"
            assert port == 587
            assert timeout == 12

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def starttls(self):
            pass

        def login(self, username, password):
            assert username == "test-user"
            assert password == "test-password"

        def send_message(self, email):
            assert email["To"] == "admin@example.test"

    monkeypatch.setattr("app.smtplib.SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "mail.example.test")
    monkeypatch.setenv("SMTP_USER", "test-user")
    monkeypatch.setenv("SMTP_PASSWORD", "test-password")
    monkeypatch.setenv("EMAIL_FROM_ADDRESS", "noreply@example.test")
    with database_connection() as connection:
        cursor = connection.execute("""INSERT INTO notifications
            (created_at,reference_id,recipient,status,details,subject,message,attempts)
            VALUES('2026-01-01T00:00:00+00:00','PSR-TEST','admin@example.test',
                   'failed','ConnectionError','New request','A generic request was received',0)""")
        notification_id = cursor.lastrowid
        no_recipient = connection.execute("""INSERT INTO notifications
            (created_at,reference_id,recipient,status,details,subject,message,attempts)
            VALUES('2026-01-01T00:00:00+00:00','PSR-OTHER','','unconfigured',
                   'No recipient','New request','A generic request was received',0)""").lastrowid

    csrf = get_token("/admin")
    assert client.post("/admin/notification-retry", data={
        "csrf": csrf, "notification_id": notification_id,
    }).status_code == 403
    csrf = login_as_admin()
    assert client.post("/admin/notification-retry", data={
        "csrf": csrf, "notification_id": no_recipient,
    }, follow_redirects=False).status_code == 409
    assert client.post("/admin/notification-retry", data={
        "csrf": csrf, "notification_id": notification_id,
    }, follow_redirects=False).status_code == 303
    with database_connection() as connection:
        status, attempts = connection.execute(
            "SELECT status,attempts FROM notifications WHERE id=?",
            (notification_id,)).fetchone()
    assert (status, attempts) == ("accepted", 1)


def test_production_configuration_rejects_unsafe_values():
    valid = {
        "APP_ENV": "production",
        "ADMIN_PASSWORD": "Admin-Prod-Passphrase-Example-2026!Strong",
        "SESSION_SECRET": "Session-Secret-Example-For-Prod-2026-Random-String!",
        "SECURE_COOKIES": "true",
        "DATABASE_PATH": os.path.abspath("pangasalama-production-test.sqlite3"),
        "IMAGE_STORAGE_BACKEND": "s3",
        "S3_BUCKET": "pangasalama-test-bucket",
        "S3_REGION": "af-south-1",
    }
    validate_settings(valid)
    with pytest.raises(RuntimeError, match="APP_ENV"):
        validate_settings({})
    with pytest.raises(RuntimeError, match="APP_ENV"):
        validate_settings(dict(valid, APP_ENV="staging"))
    for key, value in (
        ("ADMIN_PASSWORD", "short"),
        ("ADMIN_PASSWORD", "A" * 40),
        ("SESSION_SECRET", "short"),
        ("SESSION_SECRET", "S" * 42),
        ("SECURE_COOKIES", "false"),
        ("DATABASE_PATH", "relative.sqlite3"),
        ("IMAGE_STORAGE_BACKEND", "local"),
    ):
        unsafe = dict(valid)
        unsafe[key] = value
        with pytest.raises(RuntimeError):
            validate_settings(unsafe)
    same_secret = dict(valid, SESSION_SECRET=valid["ADMIN_PASSWORD"])
    with pytest.raises(RuntimeError):
        validate_settings(same_secret)
    weak_entropy = dict(valid, ADMIN_PASSWORD="a" * 40)
    with pytest.raises(RuntimeError):
        validate_settings(weak_entropy)


def test_application_startup_rejects_missing_or_invalid_app_env():
    for app_env in (None, "staging"):
        environment = os.environ.copy()
        if app_env is None:
            environment.pop("APP_ENV", None)
        else:
            environment["APP_ENV"] = app_env
        environment["DATABASE_PATH"] = os.path.join(
            TEST_DATA.name, f"startup-{app_env or 'missing'}.sqlite3")
        result = subprocess.run(
            [sys.executable, "-c", "import app"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "APP_ENV must be explicitly set" in result.stderr


def test_database_initialization_preserves_legacy_data(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("""CREATE TABLE listings(
            id TEXT PRIMARY KEY,created_at TEXT NOT NULL,owner_name TEXT NOT NULL,owner_phone TEXT NOT NULL,
            provider_type TEXT NOT NULL,region TEXT NOT NULL,area TEXT NOT NULL,property_type TEXT NOT NULL,
            rent INTEGER NOT NULL,bedrooms INTEGER NOT NULL,advance_months INTEGER NOT NULL,
            description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',availability_checked_at TEXT);
            CREATE TABLE requests(
            id TEXT PRIMARY KEY,created_at TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,
            phone TEXT NOT NULL,region TEXT,area TEXT,budget INTEGER,listing_id TEXT,message TEXT,
            status TEXT NOT NULL DEFAULT 'new');
            CREATE TABLE notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,reference_id TEXT NOT NULL,
            recipient TEXT NOT NULL,status TEXT NOT NULL,details TEXT);
            CREATE TABLE listing_photos(
            id TEXT PRIMARY KEY,listing_id TEXT NOT NULL,storage_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',is_cover INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL);
            INSERT INTO listings VALUES(
            'PSL-OLD','2025-01-01T00:00:00+00:00','Owner','0712345678','owner',
            'Dodoma','Nzuguni','Studio',150000,1,3,'Existing record','pending',NULL);""")
        connection.commit()

    init_db(database)
    init_db(database)
    with closing(sqlite3.connect(database)) as connection:
        listing = connection.execute(
            "SELECT id,description,extra_fee FROM listings WHERE id='PSL-OLD'").fetchone()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert listing == ("PSL-OLD", "Existing record", 0)
    assert {"admin_audit", "public_rate_limits", "admin_login_attempts",
            "admin_sessions", "listing_photos"} <= tables
    with closing(sqlite3.connect(database)) as connection:
        photo_columns = {row[1] for row in connection.execute("PRAGMA table_info(listing_photos)")}
    assert "thumbnail_key" in photo_columns


def test_database_backup_verify_and_restore_are_safe(tmp_path):
    backup = tmp_path / "baseline.sqlite3"
    assert backup_database(DATABASE, backup) == backup
    assert verify_database(backup) == backup
    with pytest.raises(FileExistsError):
        backup_database(DATABASE, backup)

    with database_connection() as connection:
        connection.execute("""INSERT INTO notifications
            (created_at,reference_id,recipient,status,details)
            VALUES('2026-01-01T00:00:00+00:00','PSR-TEST','','unconfigured','test')""")
    safety_backup = restore_database(DATABASE, backup)
    assert safety_backup is not None and safety_backup.is_file()
    with database_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    with closing(sqlite3.connect(safety_backup)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_restore_refuses_sqlite_sidecars(tmp_path):
    backup = tmp_path / "backup.sqlite3"
    backup_database(DATABASE, backup)
    sidecar = f"{DATABASE}-wal"
    with open(sidecar, "w", encoding="utf-8") as file:
        file.write("test")
    try:
        with pytest.raises(RuntimeError, match="sidecar"):
            restore_database(DATABASE, backup)
    finally:
        os.remove(sidecar)
