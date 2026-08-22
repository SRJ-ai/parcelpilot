"""Ingest the data pack: workbook -> in-memory SQLite, PDFs -> section chunks + BM25.

Loading the whole pack at startup is fine: the data is static and tiny. SQLite is
used so access scoping is a clean WHERE clause and calculations are auditable.
"""
import os
import re
import sqlite3
import threading
import openpyxl
from pypdf import PdfReader
from rank_bm25 import BM25Okapi

from app.config import DATA_DIR, DOCUMENTS, WORKBOOK

# One in-memory SQLite connection is shared across request threads (check_same_thread
# is off), and SQLite forbids concurrent use of a single connection. Serialize every
# access through this lock. ponytail: global DB lock, split per-table if throughput
# ever matters — at this data size it never will.
DB_LOCK = threading.RLock()

# ---------- structured data ----------

SHEET_TABLES = ("accounts", "orders", "tickets")


def _norm(v):
    # openpyxl gives datetime objects; store ISO strings so SQLite comparisons are lexical-safe.
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ")
    return v


def load_sqlite(path: str | None = None) -> sqlite3.Connection:
    """Reference tables (accounts/orders/tickets) are always re-seeded fresh from the
    workbook. The state tables (actions, audit_log) persist when STATE_DB points at a
    file — so escalations and the audit trail survive restarts. Default `:memory:` keeps
    tests and ephemeral runs clean. ponytail: disk SQLite is the right size here; Postgres
    is the multi-worker scale path, not a day-one need."""
    path = path or os.getenv("STATE_DB", ":memory:")
    wb = openpyxl.load_workbook(DATA_DIR / WORKBOOK, data_only=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    for name in SHEET_TABLES:
        ws = wb[name]
        rows = list(ws.iter_rows(values_only=True))
        header = [str(h) for h in rows[0]]
        cols = ", ".join(f'"{h}"' for h in header)
        col_defs = ", ".join(f'"{h}" TEXT' for h in header)
        con.execute(f"DROP TABLE IF EXISTS {name}")  # reference data is authoritative from the workbook
        con.execute(f"CREATE TABLE {name} ({col_defs})")
        ph = ", ".join("?" for _ in header)
        con.executemany(
            f"INSERT INTO {name} ({cols}) VALUES ({ph})",
            [[_norm(v) for v in r] for r in rows[1:]],
        )
    # persistent state: mock action store + append-only audit trail
    con.execute("CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "kind TEXT, payload TEXT, created_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT, request_id TEXT, role TEXT, event TEXT, detail TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, user_email TEXT, "
                "title TEXT, created_at TEXT, updated_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "conversation_id TEXT, role TEXT, content TEXT, meta TEXT, created_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS raised_tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "account_id TEXT, email TEXT, subject TEXT, description TEXT, status TEXT, created_at TEXT)")
    con.commit()
    return con


# ---------- documents ----------

SECTION_RE = re.compile(r"(?m)^\s*(\d+)\.\s+\S")


def _pdf_text(path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def _chunk(text: str) -> list[str]:
    """Split on numbered section headings ("1. ", "2. "...). One chunk per section;
    any preamble before section 1 is its own chunk."""
    starts = [m.start() for m in SECTION_RE.finditer(text)]
    if not starts:
        return [text.strip()] if text.strip() else []
    bounds = ([0] if starts[0] > 0 else []) + starts + [len(text)]
    chunks = []
    for a, b in zip(bounds, bounds[1:]):
        s = text[a:b].strip()
        if s:
            chunks.append(s)
    return chunks


class DocIndex:
    """BM25 over section chunks, each carrying authority metadata. Retrieval filters
    by status (deprecated excluded by default) and owner_account_id (agreement scope)."""

    def __init__(self):
        self.chunks: list[dict] = []
        for doc in DOCUMENTS:
            text = _pdf_text(DATA_DIR / doc["file"])
            for i, body in enumerate(_chunk(text)):
                self.chunks.append(
                    {
                        "doc_file": doc["file"],
                        "title": doc["title"],
                        "doc_type": doc["doc_type"],
                        "status": doc["status"],
                        "authority_tier": doc["authority_tier"],
                        "effective": doc["effective"],
                        "owner_account_id": doc["owner_account_id"],
                        "section": i,
                        "text": body,
                    }
                )
        self._bm25 = BM25Okapi([self._tok(c["text"]) for c in self.chunks])

    @staticmethod
    def _tok(s: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", s.lower())

    def search(
        self,
        query: str,
        account_id: str | None,
        include_deprecated: bool = False,
        top_k: int = 5,
    ) -> list[dict]:
        scores = self._bm25.get_scores(self._tok(query))
        ranked = sorted(range(len(self.chunks)), key=lambda i: scores[i], reverse=True)
        out = []
        for i in ranked:
            c = self.chunks[i]
            if c["status"] == "deprecated" and not include_deprecated:
                continue
            # agreement visibility: only its owner (customers) or staff (account_id=None means staff/all)
            if c["owner_account_id"] and account_id is not None and c["owner_account_id"] != account_id:
                continue
            if scores[i] <= 0:
                continue
            out.append({**c, "score": round(float(scores[i]), 3)})
            if len(out) >= top_k:
                break
        return out
