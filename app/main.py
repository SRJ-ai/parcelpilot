"""FastAPI app: session auth, chat, confirmation, proactive feed, single-page UI.

Identity is established once at POST /session (the mock authentication boundary) and
returned as an opaque bearer token. Every later request carries only that token; the
AuthContext is looked up server-side and is never taken from the request body. A client
therefore cannot pick its own role or account — closing the impersonation hole where a
customer could simply send {"login": "staff_agent"}.

State (sessions, history, mock action store) is in-process. ponytail: fine for a single
worker; a shared store (Redis) is the scale-out path for multiple workers.
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
from app.auth import MOCK_SESSIONS
from app.tools import ToolBox
from app.state import open_state
from app import agent, proactive, obs

load_dotenv()

app = FastAPI(title="ParcelPilot AI Support")
CON = load_sqlite()
IDX = DocIndex()
STATE = open_state(CON)  # Postgres/Supabase when DATABASE_URL is set, else SQLite
STATIC = Path(__file__).resolve().parent / "static"

SESSIONS: dict[str, dict] = {}      # token -> {auth, history, pending, created, last, hits[]}
SESSION_TTL = 60 * 60               # 1h idle expiry
MAX_SESSIONS = 500
RATE_MAX = 30                       # messages per RATE_WINDOW per token
RATE_WINDOW = 60

CONTEXT_LABELS = {
    "customer_northstar": "Northstar Logistics · customer",
    "customer_lumenworks": "LumenWorks · customer",
    "customer_beacon": "Beacon Retail · customer",
    "staff_agent": "Support agent · internal",
    "staff_manager": "Support manager · internal",
}


def _prune():
    now = time.time()
    for tok in [t for t, s in SESSIONS.items() if now - s["last"] > SESSION_TTL]:
        SESSIONS.pop(tok, None)
    if len(SESSIONS) > MAX_SESSIONS:  # evict oldest
        for tok in sorted(SESSIONS, key=lambda t: SESSIONS[t]["last"])[: len(SESSIONS) - MAX_SESSIONS]:
            SESSIONS.pop(tok, None)


def _auth_session(token: str):
    s = SESSIONS.get(token or "")
    if s is None:
        return None
    if time.time() - s["last"] > SESSION_TTL:
        SESSIONS.pop(token, None)
        return None
    s["last"] = time.time()
    return s


def _rate_ok(s) -> bool:
    now = time.time()
    s["hits"] = [t for t in s["hits"] if now - t < RATE_WINDOW]
    if len(s["hits"]) >= RATE_MAX:
        return False
    s["hits"].append(now)
    return True


class SessionIn(BaseModel):
    login: str = Field(min_length=1, max_length=64)


class ChatIn(BaseModel):
    token: str = Field(min_length=8, max_length=128)
    message: str = Field(min_length=1, max_length=4000)


class ConfirmIn(BaseModel):
    token: str = Field(min_length=8, max_length=128)
    approved: bool


@app.middleware("http")
async def security_and_logging(request: Request, call_next):
    rid = obs.new_request_id()
    start = time.time()
    resp = await call_next(request)
    if request.url.path not in ("/health",) and not request.url.path.startswith("/static"):
        obs.event("http", method=request.method, path=request.url.path,
                  status=resp.status_code, ms=round((time.time() - start) * 1000))
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


@app.get("/logins")
def logins():
    return {"logins": list(MOCK_SESSIONS.keys()), "provider": os.getenv("LLM_PROVIDER", "groq")}


@app.post("/session")
def create_session(inp: SessionIn):
    if inp.login not in MOCK_SESSIONS:
        return JSONResponse({"error": "unknown login"}, status_code=400)
    _prune()
    token = secrets.token_urlsafe(24)
    now = time.time()
    SESSIONS[token] = {"auth": MOCK_SESSIONS[inp.login], "history": agent.new_history(),
                       "pending": None, "created": now, "last": now, "hits": [],
                       "context": CONTEXT_LABELS.get(inp.login, inp.login),
                       "is_staff": not MOCK_SESSIONS[inp.login].is_customer}
    return {"token": token, "context": SESSIONS[token]["context"], "is_staff": SESSIONS[token]["is_staff"]}


@app.post("/chat")
def chat(inp: ChatIn):
    s = _auth_session(inp.token)
    if s is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    if s["pending"]:
        return JSONResponse({"error": "confirm or cancel the pending action first"}, status_code=409)
    if not _rate_ok(s):
        return JSONResponse({"error": "rate limit exceeded; slow down"}, status_code=429)
    s["history"].append({"role": "user", "content": inp.message})
    try:
        events, pending = agent.run(s["history"], ToolBox(CON, IDX, s["auth"], STATE))
    except Exception as e:
        obs.error("llm_failure", err=f"{type(e).__name__}: {e}")
        return JSONResponse({"error": f"The assistant is temporarily unavailable ({type(e).__name__}). Please retry."},
                            status_code=502)
    s["pending"] = pending
    return {"events": events, "awaiting_confirmation": pending is not None}


@app.post("/confirm")
def confirm(inp: ConfirmIn):
    s = _auth_session(inp.token)
    if s is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    if not s["pending"]:
        return JSONResponse({"error": "no pending action"}, status_code=400)
    pending = s["pending"]
    s["pending"] = None
    try:
        events, new_pending = agent.confirm(s["history"], ToolBox(CON, IDX, s["auth"], STATE), pending, inp.approved)
    except Exception as e:
        obs.error("llm_failure", err=f"{type(e).__name__}: {e}")
        return JSONResponse({"error": f"The assistant is temporarily unavailable ({type(e).__name__}). Please retry."},
                            status_code=502)
    s["pending"] = new_pending
    return {"events": events, "awaiting_confirmation": new_pending is not None}


@app.get("/proactive")
def proactive_feed(token: str):
    s = _auth_session(token)
    if s is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    if s["auth"].is_customer:
        return JSONResponse({"error": "staff only"}, status_code=403)
    return {"items": proactive.attention_feed(CON)}


@app.get("/audit")
def audit_trail(token: str, limit: int = 50):
    s = _auth_session(token)
    if s is None:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    if s["auth"].is_customer:
        return JSONResponse({"error": "staff only"}, status_code=403)
    return {"entries": STATE.recent_audit(max(1, min(limit, 200)))}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
