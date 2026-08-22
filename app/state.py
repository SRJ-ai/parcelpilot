"""Pluggable state store for the two things that must outlive a restart: the action
store (escalations/tickets/tasks) and the append-only audit log.

- `DATABASE_URL` set  -> Postgres / Supabase (durable, shared across workers).
- unset               -> SQLite (the reference connection's tables; zero-setup dev).

Reference data (accounts/orders/tickets) never comes here — it is re-seeded from the
workbook each boot. Only mutable state lives in the store. Two real backends behind one
interface, the same shape as the LLM adapter.
"""
import os
import json
import threading
from datetime import datetime, timezone

from app.ingest import DB_LOCK


def _now():
    return datetime.now(timezone.utc).isoformat()


def open_state(sqlite_con):
    dsn = os.getenv("DATABASE_URL")
    return PostgresState(dsn) if dsn else SqliteState(sqlite_con)


class SqliteState:
    """Uses the reference SQLite connection's `actions` / `audit_log` tables."""

    def __init__(self, con):
        self.con = con

    def record_action(self, kind, payload) -> str:
        with DB_LOCK:
            cur = self.con.execute(
                "INSERT INTO actions (kind, payload, created_at) VALUES (?,?,?)",
                (kind, json.dumps(payload), _now()))
            self.con.commit()
            return f"ACT-{cur.lastrowid:04d}"

    def write_audit(self, request_id, role, event, detail):
        with DB_LOCK:
            self.con.execute(
                "INSERT INTO audit_log (ts, request_id, role, event, detail) VALUES (?,?,?,?,?)",
                (_now(), request_id, role, event, json.dumps(detail)))
            self.con.commit()

    def recent_audit(self, limit):
        with DB_LOCK:
            rows = self.con.execute(
                "SELECT ts, request_id, role, event, detail FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- conversations ----
    def create_conversation(self, conv_id, user, title):
        with DB_LOCK:
            self.con.execute("INSERT INTO conversations (id, user_email, title, created_at, updated_at) "
                             "VALUES (?,?,?,?,?)", (conv_id, user, title, _now(), _now()))
            self.con.commit()

    def list_conversations(self, user):
        with DB_LOCK:
            rows = self.con.execute("SELECT id, title, updated_at FROM conversations WHERE user_email=? "
                                    "ORDER BY updated_at DESC LIMIT 100", (user,)).fetchall()
        return [dict(r) for r in rows]

    def conversation_owner(self, conv_id):
        with DB_LOCK:
            r = self.con.execute("SELECT user_email FROM conversations WHERE id=?", (conv_id,)).fetchone()
        return r["user_email"] if r else None

    def add_message(self, conv_id, role, content, meta=None):
        with DB_LOCK:
            self.con.execute("INSERT INTO messages (conversation_id, role, content, meta, created_at) "
                             "VALUES (?,?,?,?,?)", (conv_id, role, content, json.dumps(meta) if meta else None, _now()))
            self.con.execute("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), conv_id))
            self.con.commit()

    def get_messages(self, conv_id):
        with DB_LOCK:
            rows = self.con.execute("SELECT role, content, meta, created_at FROM messages "
                                    "WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
        return [dict(r) for r in rows]

    def set_title(self, conv_id, title):
        with DB_LOCK:
            self.con.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
            self.con.commit()

    # ---- customer-raised tickets ----
    def raise_ticket(self, account_id, email, subject, description):
        with DB_LOCK:
            cur = self.con.execute("INSERT INTO raised_tickets (account_id, email, subject, description, status, created_at) "
                                   "VALUES (?,?,?,?, 'open', ?)", (account_id, email, subject, description, _now()))
            self.con.commit()
            return f"RT-{cur.lastrowid:04d}"

    def list_tickets(self, account_id=None):
        with DB_LOCK:
            if account_id:
                rows = self.con.execute("SELECT id, account_id, email, subject, description, status, created_at "
                                        "FROM raised_tickets WHERE account_id=? ORDER BY id DESC LIMIT 100", (account_id,)).fetchall()
            else:
                rows = self.con.execute("SELECT id, account_id, email, subject, description, status, created_at "
                                        "FROM raised_tickets ORDER BY id DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]


class PostgresState:
    """Supabase / Postgres backend. Connect-per-write (low volume: actions + audit),
    serialized by a lock. ponytail: add a pool only if write throughput ever demands it."""

    def __init__(self, dsn):
        self.dsn = dsn
        self._lock = threading.Lock()
        self._exec("CREATE TABLE IF NOT EXISTS actions (id SERIAL PRIMARY KEY, kind TEXT, "
                   "payload TEXT, created_at TIMESTAMPTZ DEFAULT now())")
        self._exec("CREATE TABLE IF NOT EXISTS audit_log (id SERIAL PRIMARY KEY, "
                   "ts TIMESTAMPTZ DEFAULT now(), request_id TEXT, role TEXT, event TEXT, detail TEXT)")
        self._exec("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, user_email TEXT, "
                   "title TEXT, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now())")
        self._exec("CREATE TABLE IF NOT EXISTS messages (id SERIAL PRIMARY KEY, conversation_id TEXT, "
                   "role TEXT, content TEXT, meta TEXT, created_at TIMESTAMPTZ DEFAULT now())")
        self._exec("CREATE TABLE IF NOT EXISTS raised_tickets (id SERIAL PRIMARY KEY, account_id TEXT, "
                   "email TEXT, subject TEXT, description TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMPTZ DEFAULT now())")

    def _conn(self):
        import psycopg  # lazy: app runs without psycopg when DATABASE_URL is unset
        return psycopg.connect(self.dsn)

    def _exec(self, sql, params=None, fetch=None):
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(sql, params or ())
                out = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
                cols = [d[0] for d in cur.description] if (fetch and cur.description) else None
                conn.commit()
                return out, cols
            finally:
                conn.close()

    def record_action(self, kind, payload) -> str:
        (row, _) = self._exec("INSERT INTO actions (kind, payload) VALUES (%s,%s) RETURNING id",
                              (kind, json.dumps(payload)), fetch="one")
        return f"ACT-{row[0]:04d}"

    def write_audit(self, request_id, role, event, detail):
        self._exec("INSERT INTO audit_log (request_id, role, event, detail) VALUES (%s,%s,%s,%s)",
                   (request_id, role, event, json.dumps(detail)))

    def recent_audit(self, limit):
        rows, cols = self._exec(
            "SELECT ts, request_id, role, event, detail FROM audit_log ORDER BY id DESC LIMIT %s",
            (limit,), fetch="all")
        return [{c: str(v) for c, v in zip(cols, row)} for row in rows]

    def _rows(self, sql, params):
        rows, cols = self._exec(sql, params, fetch="all")
        return [{c: (str(v) if v is not None else None) for c, v in zip(cols, row)} for row in rows]

    def create_conversation(self, conv_id, user, title):
        self._exec("INSERT INTO conversations (id, user_email, title) VALUES (%s,%s,%s)", (conv_id, user, title))

    def list_conversations(self, user):
        return self._rows("SELECT id, title, updated_at FROM conversations WHERE user_email=%s "
                          "ORDER BY updated_at DESC LIMIT 100", (user,))

    def conversation_owner(self, conv_id):
        (row, _) = self._exec("SELECT user_email FROM conversations WHERE id=%s", (conv_id,), fetch="one")
        return row[0] if row else None

    def add_message(self, conv_id, role, content, meta=None):
        self._exec("INSERT INTO messages (conversation_id, role, content, meta) VALUES (%s,%s,%s,%s)",
                   (conv_id, role, content, json.dumps(meta) if meta else None))
        self._exec("UPDATE conversations SET updated_at=now() WHERE id=%s", (conv_id,))

    def get_messages(self, conv_id):
        return self._rows("SELECT role, content, meta, created_at FROM messages "
                          "WHERE conversation_id=%s ORDER BY id", (conv_id,))

    def set_title(self, conv_id, title):
        self._exec("UPDATE conversations SET title=%s WHERE id=%s", (title, conv_id))

    def raise_ticket(self, account_id, email, subject, description):
        (row, _) = self._exec("INSERT INTO raised_tickets (account_id, email, subject, description, status) "
                              "VALUES (%s,%s,%s,%s,'open') RETURNING id", (account_id, email, subject, description), fetch="one")
        return f"RT-{row[0]:04d}"

    def list_tickets(self, account_id=None):
        if account_id:
            return self._rows("SELECT id, account_id, email, subject, description, status, created_at "
                              "FROM raised_tickets WHERE account_id=%s ORDER BY id DESC LIMIT 100", (account_id,))
        return self._rows("SELECT id, account_id, email, subject, description, status, created_at "
                          "FROM raised_tickets ORDER BY id DESC LIMIT 100", ())
