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
