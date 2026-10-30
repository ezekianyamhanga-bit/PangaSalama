import os
os.environ["ADMIN_PASSWORD"] = "testing-secret"
os.environ["SESSION_SECRET"] = "session-testing-secret"
os.environ["DATABASE_PATH"] = "/tmp/pangasalama-test.sqlite3"
os.environ["SECURE_COOKIES"] = "false"
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)

def get_token():
    import re
    response = client.get("/")
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)

def test_public_and_admin_protection():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert "Usimamizi wa PangaSalama" in client.get("/admin").text

def test_listing_requires_approval():
    import sqlite3
    token = get_token()
    result = client.post("/submit/listing", data={"csrf": token,"owner_name":"Test Owner","owner_phone":"0712345678","provider_type":"owner","region":"Dodoma","area":"Nzuguni","property_type":"Studio","rent":"150000","bedrooms":"1","advance_months":"3","extra_fee":"10000","description":"Test listing","consent":"yes"})
    assert result.status_code == 200
    assert "Tangazo limepokelewa" in result.text
    assert "Studio — Nzuguni" not in client.get("/").text
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM listings WHERE status='pending'").fetchone()[0] >= 1

def test_unapproved_viewing_blocked():
    token = get_token()
    response = client.post("/submit/request",data={"csrf":token,"kind":"viewing","name":"Tester","phone":"0712345678","listing_id":"INVALID","consent":"yes"})
    assert response.status_code == 404
