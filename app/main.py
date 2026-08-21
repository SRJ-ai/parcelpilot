"""FastAPI app: chat + confirmation + proactive feed, single-page UI.

Conversation state (history + any pending confirmation) is kept per session in memory.
The AuthContext is derived server-side from the chosen mock login and is never taken
from the client message body, so it cannot be widened by the model or the user text.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.ingest import load_sqlite, DocIndex
from app.auth import MOCK_SESSIONS
from app.tools import ToolBox
from app import agent, proactive

load_dotenv()

app = FastAPI(title="ParcelPilot AI Support")
CON = load_sqlite()
IDX = DocIndex()
SESSIONS: dict[str, dict] = {}   # session_id -> {"history", "pending", "login"}
STATIC = Path(__file__).resolve().parent / "static"


def _session(session_id: str, login: str) -> dict:
    s = SESSIONS.get(session_id)
    if s is None or s["login"] != login:
        s = {"history": agent.new_history(), "pending": None, "login": login}
        SESSIONS[session_id] = s
    return s


def _toolbox(login: str) -> ToolBox:
    return ToolBox(CON, IDX, MOCK_SESSIONS[login])


class ChatIn(BaseModel):
    session_id: str
    login: str
    message: str


class ConfirmIn(BaseModel):
    session_id: str
    login: str
    approved: bool


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/logins")
def logins():
    return {"logins": list(MOCK_SESSIONS.keys()),
            "provider": os.getenv("LLM_PROVIDER", "groq")}


@app.post("/chat")
def chat(inp: ChatIn):
    if inp.login not in MOCK_SESSIONS:
        return JSONResponse({"error": "unknown login"}, status_code=400)
    s = _session(inp.session_id, inp.login)
    if s["pending"]:
        return JSONResponse({"error": "confirm or cancel the pending action first"}, status_code=409)
    s["history"].append({"role": "user", "content": inp.message})
    try:
        events, pending = agent.run(s["history"], _toolbox(inp.login))
    except Exception as e:  # surface provider/key errors to the UI instead of 500
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    s["pending"] = pending
    return {"events": events, "awaiting_confirmation": pending is not None}


@app.post("/confirm")
def confirm(inp: ConfirmIn):
    s = SESSIONS.get(inp.session_id)
    if not s or not s["pending"]:
        return JSONResponse({"error": "no pending action"}, status_code=400)
    pending = s["pending"]
    s["pending"] = None
    try:
        events, new_pending = agent.confirm(s["history"], _toolbox(inp.login), pending, inp.approved)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    s["pending"] = new_pending
    return {"events": events, "awaiting_confirmation": new_pending is not None}


@app.get("/proactive")
def proactive_feed(login: str):
    if MOCK_SESSIONS.get(login, None) is None or MOCK_SESSIONS[login].is_customer:
        return JSONResponse({"error": "staff only"}, status_code=403)
    return {"items": proactive.attention_feed(CON)}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
