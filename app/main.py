"""FastAPI app: JWT auth, chat, confirmation, proactive feed, audit, admin metrics, UI.

Identity comes from a signed JWT issued at POST /login (bcrypt-verified credentials).
Every later request carries the token; the AuthContext is rebuilt from the *verified*
claims, never from the body — a client cannot choose its own role or account. Server-side
conversation state is keyed by the token's jti.
"""
import os
import time
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
from app import agent, proactive, obs

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
    message: str = Field(min_length=1, max_length=4000)


class ConfirmIn(BaseModel):
    token: str = Field(min_length=8, max_length=4000)
    approved: bool


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
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
    )
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


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


@app.post("/chat")
def chat(inp: ChatIn):
    a = _auth(inp.token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    ctx, s = a
    if s["pending"]:
        return JSONResponse({"error": "confirm or cancel the pending action first"}, status_code=409)
    if not _rate_ok(s):
        return JSONResponse({"error": "rate limit exceeded; slow down"}, status_code=429)
    s["history"].append({"role": "user", "content": inp.message})
    try:
        events, pending = agent.run(s["history"], ToolBox(CON, IDX, ctx, STATE))
    except Exception as e:
        obs.error("llm_failure", err=f"{type(e).__name__}: {e}")
        return JSONResponse({"error": f"The assistant is temporarily unavailable ({type(e).__name__}). Please retry."},
                            status_code=502)
    s["pending"] = pending
    return {"events": events, "awaiting_confirmation": pending is not None}


@app.post("/confirm")
def confirm(inp: ConfirmIn):
    a = _auth(inp.token)
    if a is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    ctx, s = a
    if not s["pending"]:
        return JSONResponse({"error": "no pending action"}, status_code=400)
    pending = s["pending"]
    s["pending"] = None
    try:
        events, new_pending = agent.confirm(s["history"], ToolBox(CON, IDX, ctx, STATE), pending, inp.approved)
    except Exception as e:
        obs.error("llm_failure", err=f"{type(e).__name__}: {e}")
        return JSONResponse({"error": f"The assistant is temporarily unavailable ({type(e).__name__}). Please retry."},
                            status_code=502)
    s["pending"] = new_pending
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


app.mount("/static", StaticFiles(directory=STATIC), name="static")
