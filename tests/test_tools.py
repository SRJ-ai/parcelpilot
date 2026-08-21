"""Access control enforced in the tool layer, independent of model instructions."""
import pytest
from app.ingest import load_sqlite, DocIndex
from app.auth import AuthContext
from app.tools import ToolBox


@pytest.fixture(scope="module")
def deps():
    return load_sqlite(), DocIndex()


def box(deps, auth):
    con, idx = deps
    return ToolBox(con, idx, auth)


def test_customer_lookup_scoped_to_own_account(deps):
    tb = box(deps, AuthContext("customer", account_id="ACCT-001"))
    r = tb.lookup_data("orders")
    assert r["count"] > 0
    assert all(row["account_id"] == "ACCT-001" for row in r["rows"])


def test_customer_cannot_filter_into_other_account(deps):
    tb = box(deps, AuthContext("customer", account_id="ACCT-001"))
    r = tb.lookup_data("orders", {"account_id": "ACCT-002"})
    assert all(row["account_id"] == "ACCT-001" for row in r["rows"])


def test_customer_cannot_read_other_account_order_via_compute(deps):
    tb = box(deps, AuthContext("customer", account_id="ACCT-001"))
    r = tb.compute_policy_outcome("cancellation", order_id="ORD-2001")  # LumenWorks order
    assert "error" in r


def test_staff_sees_all_accounts(deps):
    tb = box(deps, AuthContext("staff", staff_role="agent"))
    r = tb.lookup_data("orders")
    assert len({row["account_id"] for row in r["rows"]}) > 1


def test_customer_can_escalate_but_not_update_ticket(deps):
    tb = box(deps, AuthContext("customer", account_id="ACCT-001"))
    assert tb.preview_write("create_escalation", {"reason": "x", "ticket_id": "TKT-501"})["allowed"] is True
    assert tb.preview_write("update_ticket", {"ticket_id": "TKT-501", "changes": {}})["allowed"] is False


def test_write_preview_does_not_commit(deps):
    con, _ = deps
    before = con.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    tb = box(deps, AuthContext("staff", staff_role="agent"))
    tb.preview_write("create_escalation", {"reason": "y", "ticket_id": "TKT-501"})
    after = con.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    assert before == after  # nothing written until commit


def test_commit_writes_action(deps):
    con, _ = deps
    tb = box(deps, AuthContext("staff", staff_role="agent"))
    r = tb.commit_write("create_escalation", {"reason": "z", "ticket_id": "TKT-501", "severity": "P1"})
    assert r["committed"] is True and r["ref"].startswith("ACT-")
    assert con.execute("SELECT COUNT(*) FROM actions").fetchone()[0] >= 1
