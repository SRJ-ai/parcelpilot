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


def conv(client, token):
    r = client.post(f"/conversations?token={token}")
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_login_wrong_password_rejected(client):
    assert client.post("/login", json={"email": f"northstar@{D}", "password": "wrong"}).status_code == 401


def test_login_unknown_email_rejected(client):
    assert client.post("/login", json={"email": f"nobody@{D}", "password": PW}).status_code == 401


def test_chat_requires_valid_token(client):
    assert client.post("/chat", json={"token": "not-a-real-jwt", "conversation_id": "aaaaaaaa", "message": "hi"}).status_code == 401


def test_tampered_token_rejected(client):
    tok = login(client, "northstar")
    assert client.post("/chat", json={"token": tok + "x", "conversation_id": "aaaaaaaa", "message": "hi"}).status_code == 401


def test_identity_from_token_not_body(client):
    # No role/account field is even accepted; a customer token stays a customer.
    tok = login(client, "northstar")
    cid = conv(client, tok)
    assert client.post("/chat", json={"token": tok, "conversation_id": cid, "message": "hi", "role": "admin"}).status_code == 200
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
    cid = conv(client, tok)
    codes = [client.post("/chat", json={"token": tok, "conversation_id": cid, "message": "hi"}).status_code
             for _ in range(main.RATE_MAX + 3)]
    assert 429 in codes


def test_raise_ticket_and_staff_sees_it(client):
    ctok = login(client, "northstar")
    r = client.post("/ticket", json={"token": ctok, "subject": "Late pickup", "description": "ORD-1001 pickup late"})
    assert r.status_code == 200 and r.json()["ref"].startswith("RT-")
    # staff sees the raised ticket; customer sees only their own
    staff = client.get(f"/tickets?token={login(client, 'agent')}").json()["tickets"]
    assert any(t["subject"] == "Late pickup" for t in staff)


def test_customer_doc_scope(client):
    # Northstar sees its own agreement (05) + general docs, not LumenWorks' (06)
    docs = client.get(f"/documents?token={login(client, 'northstar')}").json()["docs"]
    ids = {d["id"] for d in docs}
    assert "05" in ids and "06" not in ids and "data" not in ids
    # staff see everything incl. the workbook
    sdocs = {d["id"] for d in client.get(f"/documents?token={login(client, 'agent')}").json()["docs"]}
    assert {"05", "06", "data"} <= sdocs


def test_health_and_security_headers(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_document_framable_in_app_but_shell_denied(client):
    # The PDF viewer embeds /document in a same-origin iframe: it must allow self-framing.
    tok = login(client, "northstar")
    doc = client.get(f"/document/05?token={tok}")
    assert doc.status_code == 200
    assert doc.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in doc.headers.get("Content-Security-Policy", "")
    # every other surface stays clickjacking-proof
    shell = client.get("/")
    assert shell.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors" not in shell.headers.get("Content-Security-Policy", "")
