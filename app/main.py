"""FastAPI app: JWT auth, chat, confirmation, proactive feed, audit, admin metrics, UI.

Identity comes from a signed JWT issued at POST /login (bcrypt-verified credentials).
Every later request carries the token; the AuthContext is rebuilt from the *verified*
claims, never from the body — a client cannot choose its own role or account. Server-side
conversation state is keyed by the token's jti.
"""
import os
import time
import secrets
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.ingest import load_sqlite, DocIndex
from app import auth as authmod
from app.tools import ToolBox
from app.state import open_state
from app import agent, proactive, obs, docs as docmod

load_dotenv()

app = FastAPI(title="ParcelPilot AI Support")
CON = load_sqlite()
IDX = DocIndex()
STATE = open_state(CON)
USERS = authmod.seed_users()
STATIC = Path(__file__).resolve().parent / "static"

SESSIONS: dict[str, dict] = {}          # jti -> {history, pending, hits, last}
SESSION_TTL = authmod.JWT_TTL
RATE_MAX = int(os.getenv("RATE_MAX_PER_MIN", "30"))      # per-user (jti) chat rate
RATE_WINDOW = 60
IP_RATE_MAX = int(os.getenv("IP_RATE_MAX_PER_MIN", "90"))  # per-IP rate (all endpoints)
_ip_hits: dict[str, list] = {}


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "?")


def _ip_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _ip_hits.get(ip, []) if now - t < RATE_WINDOW]
    if len(hits) >= IP_RATE_MAX:
        _ip_hits[ip] = hits
        return False
    hits.append(now)
    _ip_hits[ip] = hits
    return True


def _session_for(jti: str) -> dict:
    s = SESSIONS.get(jti)
    if s is None:
        s = {"history": agent.new_history(), "pending": None, "hits": [], "last": time.time()}
        SESSIONS[jti] = s
    s["last"] = time.time()
    return s


def _auth(token: str):
    """Return (AuthContext, session) or None. Identity is from the verified JWT."""
    claims = authmod.verify_token(token or "")
    if not claims:
        return None
    return authmod.context_from_claims(claims), _session_for(claims["jti"])


def _rate_ok(s) -> bool:
    now = time.time()
    s["hits"] = [t for t in s["hits"] if now - t < RATE_WINDOW]
    if len(s["hits"]) >= RATE_MAX:
        return False
    s["hits"].append(now)
    return True


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class ChatIn(BaseModel):
    token: str = Field(min_length=8, max_length=4000)
    conversation_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class ConfirmIn(BaseModel):
    token: str = Field(min_length=8, max_length=4000)
    conversation_id: str = Field(min_length=8, max_length=64)
    approved: bool


class TicketIn(BaseModel):
    token: str = Field(min_length=8, max_length=4000)
    subject: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)


# per-conversation working state (history + pending confirmation), rebuilt from the DB
CONV: dict[str, dict] = {}


def _conv_state(conv_id: str) -> dict:
    c = CONV.get(conv_id)
    if c is None:
        history = agent.new_history()
        for m in STATE.get_messages(conv_id):
            if m["role"] in ("user", "assistant") and m.get("content"):
                history.append({"role": m["role"], "content": m["content"]})
        c = {"history": history, "pending": None}
        CONV[conv_id] = c
    return c


@app.middleware("http")
async def gate(request: Request, call_next):
    rid = obs.new_request_id()
    ip = _client_ip(request)
    if request.method == "POST" and not _ip_ok(ip):
        obs.incr("ratelimit.ip")
        return JSONResponse({"error": "too many requests from your address; slow down"}, status_code=429)
    start = time.time()
    resp = await call_next(request)
    ms = (time.time() - start) * 1000
    path = request.url.path
    if path not in ("/health",) and not path.startswith("/static"):
        obs.observe_latency(ms)
        obs.event("http", method=request.method, path=path, status=resp.status_code, ms=round(ms))
    resp.headers["X-Request-ID"] = rid
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if path.startswith("/document/"):
        # PDFs are shown in the in-app viewer (same-origin iframe): allow self-framing.
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'self'"
        )
    else:
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
        )
    return resp


@app.get("/")
def index():
    # never cache the shell (inline JS lives here): users must not get a stale build
    # that calls removed endpoints like the old /session.
    return FileResponse(STATIC / "index.html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/health")
def health():
    return {"status": "ok", "provider": os.getenv("LLM_PROVIDER", "groq"), "sessions": len(SESSIONS)}


@app.get("/accounts")
def accounts():
    """Demo login directory (email + label). The password is the shared demo value."""
    return {"accounts": [{"email": e, "label": u["label"]} for e, u in USERS.items()],
            "domain": authmod.DOMAIN}


@app.post("/login")
def login(inp: LoginIn):
    user = USERS.get(inp.email.strip().lower())
    if not user or not authmod.verify_password(user, inp.password):
        obs.incr("login.fail")
        return JSONResponse({"error": "invalid email or password"}, status_code=401)
    token, jti = authmod.issue_token(inp.email.strip().lower(), user)
    _session_for(jti)
    obs.incr("login.ok")
    ctx = authmod.context_from_user(inp.email, user)
    return {"token": token, "label": user["label"], "is_staff": not ctx.is_customer,
            "is_admin": ctx.is_admin}


def _final_meta(events):
    """Extract the assistant's final text + metadata for persistence."""
    for e in events:
        if e["type"] == "final":
            return e["text"], {k: e[k] for k in ("trust", "sources", "tokens", "abstained", "escalation_ref") if k in e}
    return "", {}


def _own_conversation(ctx, conv_id: str) -> bool:
    owner = STATE.conversation_owner(conv_id)
    return owner is not None and owner == ctx.email


@app.post("/conversations")
def new_conversation(token: str):
    a = _auth(token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    cid = secrets.token_urlsafe(12)
    STATE.create_conversation(cid, a[0].email, "New chat")
    return {"id": cid, "title": "New chat"}


@app.get("/conversations")
def list_conversations(token: str):
    a = _auth(token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    return {"conversations": STATE.list_conversations(a[0].email)}


@app.get("/conversations/{conv_id}")
def get_conversation(conv_id: str, token: str):
    a = _auth(token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    if not _own_conversation(a[0], conv_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"messages": STATE.get_messages(conv_id)}


@app.post("/chat")
def chat(inp: ChatIn):
    a = _auth(inp.token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    ctx, s = a
    if not _own_conversation(ctx, inp.conversation_id):
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    c = _conv_state(inp.conversation_id)
    if c["pending"]:
        return JSONResponse({"error": "confirm or cancel the pending action first"}, status_code=409)
    if not _rate_ok(s):
        return JSONResponse({"error": "rate limit exceeded; slow down"}, status_code=429)
    first = not any(m["role"] == "user" for m in c["history"])
    c["history"].append({"role": "user", "content": inp.message})
    STATE.add_message(inp.conversation_id, "user", inp.message)
    if first:
        STATE.set_title(inp.conversation_id, inp.message[:60])
    try:
        events, pending = agent.run(c["history"], ToolBox(CON, IDX, ctx, STATE))
    except Exception as e:
        obs.error("llm_failure", err=f"{type(e).__name__}: {e}")
        return JSONResponse({"error": f"The assistant is temporarily unavailable ({type(e).__name__}). Please retry."},
                            status_code=502)
    c["pending"] = pending
    if pending is None:
        text, meta = _final_meta(events)
        if text:
            STATE.add_message(inp.conversation_id, "assistant", text, meta)
    return {"events": events, "awaiting_confirmation": pending is not None,
            "title_updated": first}


@app.post("/confirm")
def confirm(inp: ConfirmIn):
    a = _auth(inp.token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    ctx, _ = a
    if not _own_conversation(ctx, inp.conversation_id):
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    c = _conv_state(inp.conversation_id)
    if not c["pending"]:
        return JSONResponse({"error": "no pending action"}, status_code=400)
    pending = c["pending"]
    c["pending"] = None
    try:
        events, new_pending = agent.confirm(c["history"], ToolBox(CON, IDX, ctx, STATE), pending, inp.approved)
    except Exception as e:
        obs.error("llm_failure", err=f"{type(e).__name__}: {e}")
        return JSONResponse({"error": f"The assistant is temporarily unavailable ({type(e).__name__}). Please retry."},
                            status_code=502)
    c["pending"] = new_pending
    if new_pending is None:
        text, meta = _final_meta(events)
        if text:
            STATE.add_message(inp.conversation_id, "assistant", text, meta)
    return {"events": events, "awaiting_confirmation": new_pending is not None}


@app.get("/proactive")
def proactive_feed(token: str):
    a = _auth(token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    if a[0].is_customer:
        return JSONResponse({"error": "staff only"}, status_code=403)
    return {"items": proactive.attention_feed(CON)}


@app.get("/audit")
def audit_trail(token: str, limit: int = 50):
    a = _auth(token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    if a[0].is_customer:
        return JSONResponse({"error": "staff only"}, status_code=403)
    return {"entries": STATE.recent_audit(max(1, min(limit, 200)))}


@app.get("/metrics")
def metrics(token: str):
    a = _auth(token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    if not a[0].is_admin:
        return JSONResponse({"error": "admin only"}, status_code=403)
    snap = obs.metrics_snapshot()
    snap["active_sessions"] = len(SESSIONS)
    return snap


@app.post("/ticket")
def raise_ticket(inp: TicketIn):
    a = _auth(inp.token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    ctx = a[0]
    ref = STATE.raise_ticket(ctx.account_id or "-", ctx.email, inp.subject, inp.description)
    obs.event("ticket_raised", ref=ref, account=ctx.account_id)
    ToolBox(CON, IDX, ctx, STATE).audit("ticket_raised", ref=ref, subject=inp.subject[:60])
    return {"ref": ref}


@app.get("/tickets")
def tickets(token: str):
    a = _auth(token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    ctx = a[0]
    # staff see every raised ticket; a customer sees only their own
    return {"tickets": STATE.list_tickets(None if not ctx.is_customer else ctx.account_id)}


@app.get("/documents")
def list_docs(token: str):
    a = _auth(token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    return {"docs": [{"id": d["id"], "title": d["title"], "type": d["doc_type"], "status": d["status"]}
                     for d in docmod.visible_docs(a[0])]}


@app.get("/document/{doc_id}")
def get_doc(doc_id: str, token: str):
    a = _auth(token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    d = docmod.resolve(a[0], doc_id)
    if d is None:
        return JSONResponse({"error": "not found or not permitted for your account"}, status_code=404)
    return FileResponse(docmod.path_for(d), media_type=docmod.media_type(d["file"]),
                        headers={"Content-Disposition": f'inline; filename="{d["file"]}"'})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
