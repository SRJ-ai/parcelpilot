"""HTTP + auth integration tests. LLM mocked; identity comes from a signed JWT."""
import pytest
from fastapi.testclient import TestClient
import app.main as main
from app import agent, auth as authmod

D = authmod.DOMAIN
PW = "Password123"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(agent, "run", lambda hist, tb: ([{"type": "final", "text": "ok"}], None))
    main.SESSIONS.clear()
    main._ip_hits.clear()
    return TestClient(main.app)


def login(client, local):
    r = client.post("/login", json={"email": f"{local}@{D}", "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_login_wrong_password_rejected(client):
    assert client.post("/login", json={"email": f"northstar@{D}", "password": "wrong"}).status_code == 401


def test_login_unknown_email_rejected(client):
    assert client.post("/login", json={"email": f"nobody@{D}", "password": PW}).status_code == 401


def test_chat_requires_valid_token(client):
    assert client.post("/chat", json={"token": "not-a-real-jwt", "message": "hi"}).status_code == 401


def test_tampered_token_rejected(client):
    tok = login(client, "northstar")
    assert client.post("/chat", json={"token": tok + "x", "message": "hi"}).status_code == 401


def test_identity_from_token_not_body(client):
    # No role/account field is even accepted; a customer token stays a customer.
    tok = login(client, "northstar")
    assert client.post("/chat", json={"token": tok, "message": "hi", "role": "admin"}).status_code == 200
    assert client.get(f"/proactive?token={tok}").status_code == 403  # still a customer


def test_staff_can_read_proactive(client):
    assert client.get(f"/proactive?token={login(client, 'agent')}").status_code == 200


def test_admin_only_metrics(client):
    assert client.get(f"/metrics?token={login(client, 'agent')}").status_code == 403
    r = client.get(f"/metrics?token={login(client, 'admin')}")
    assert r.status_code == 200 and "counters" in r.json()


def test_audit_staff_only(client):
    assert client.get(f"/audit?token={login(client, 'beacon')}").status_code == 403
    assert client.get(f"/audit?token={login(client, 'manager')}").status_code == 200


def test_message_length_validated(client):
    tok = login(client, "beacon")
    assert client.post("/chat", json={"token": tok, "message": "x" * 5000}).status_code == 422
    assert client.post("/chat", json={"token": tok, "message": ""}).status_code == 422


def test_user_rate_limit(client):
    tok = login(client, "beacon")
    codes = [client.post("/chat", json={"token": tok, "message": "hi"}).status_code
             for _ in range(main.RATE_MAX + 3)]
    assert 429 in codes


def test_health_and_security_headers(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
