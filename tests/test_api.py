"""HTTP + auth integration tests. The LLM is mocked so these run offline and fast.
The point is the trust boundary: identity comes from the server-side session, never
from the request body.
"""
import pytest
from fastapi.testclient import TestClient
import app.main as main
from app import agent


@pytest.fixture
def client(monkeypatch):
    # never call the real LLM
    monkeypatch.setattr(agent, "run", lambda hist, tb: ([{"type": "final", "text": "ok"}], None))
    main.SESSIONS.clear()
    return TestClient(main.app)


def session(client, login):
    r = client.post("/session", json={"login": login})
    assert r.status_code == 200
    return r.json()["token"]


def test_chat_requires_valid_token(client):
    assert client.post("/chat", json={"token": "bogustoken123", "message": "hi"}).status_code == 401


def test_no_way_to_declare_role_in_chat(client):
    # The body has no login/role field; identity is bound to the token at /session.
    tok = session(client, "customer_northstar")
    r = client.post("/chat", json={"token": tok, "message": "hi", "login": "staff_manager"})
    assert r.status_code == 200  # extra field ignored; still a customer session


def test_customer_token_cannot_read_proactive(client):
    tok = session(client, "customer_northstar")
    assert client.get(f"/proactive?token={tok}").status_code == 403


def test_staff_token_can_read_proactive(client):
    tok = session(client, "staff_agent")
    r = client.get(f"/proactive?token={tok}")
    assert r.status_code == 200 and "items" in r.json()


def test_proactive_rejects_invalid_token(client):
    assert client.get("/proactive?token=nope").status_code == 401


def test_unknown_login_rejected(client):
    assert client.post("/session", json={"login": "customer_evilcorp"}).status_code == 400


def test_message_length_validated(client):
    tok = session(client, "customer_beacon")
    assert client.post("/chat", json={"token": tok, "message": "x" * 5000}).status_code == 422
    assert client.post("/chat", json={"token": tok, "message": ""}).status_code == 422


def test_rate_limit(client):
    tok = session(client, "customer_beacon")
    codes = [client.post("/chat", json={"token": tok, "message": "hi"}).status_code
             for _ in range(main.RATE_MAX + 3)]
    assert 429 in codes


def test_audit_endpoint_staff_only(client):
    assert client.get(f"/audit?token={session(client, 'customer_beacon')}").status_code == 403
    r = client.get(f"/audit?token={session(client, 'staff_agent')}")
    assert r.status_code == 200 and "entries" in r.json()


def test_security_headers_and_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
