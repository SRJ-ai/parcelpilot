"""Persistent state + audit trail."""
from app.ingest import load_sqlite
from app.auth import AuthContext
from app.tools import ToolBox


def _staff(con):
    return ToolBox(con, None, AuthContext("staff", staff_role="agent"))


def test_action_writes_audit_row():
    con = load_sqlite()  # :memory:
    _staff(con).commit_write("create_escalation", {"ticket_id": "TKT-501", "severity": "P1", "reason": "x"})
    row = con.execute("SELECT event, role FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["event"] == "action_committed" and row["role"] == "staff"


def test_state_persists_across_reopen(tmp_path):
    db = str(tmp_path / "state.db")
    con = load_sqlite(db)
    _staff(con).commit_write("create_escalation", {"ticket_id": "TKT-501", "severity": "P1", "reason": "y"})
    con.close()

    con2 = load_sqlite(db)  # reopen same file
    assert con2.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1        # action persisted
    assert con2.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] >= 1      # audit persisted
    assert con2.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 4       # reference re-seeded fresh
