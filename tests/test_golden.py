"""Golden cases = the grading rubric. These lock the deliberate traps in the data
pack before any LLM is wired in. Pure logic over live workbook rows + snapshot time.
"""
import pytest
from app.ingest import load_sqlite, DocIndex
from app.reliability import (
    resolve_terms, cancellation, service_credit, sla_breach, DEFAULT_TERMS,
)


@pytest.fixture(scope="module")
def con():
    return load_sqlite()


@pytest.fixture(scope="module")
def idx():
    return DocIndex()


def order(con, oid):
    return dict(con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone())


def ticket(con, tid):
    return dict(con.execute("SELECT * FROM tickets WHERE ticket_id=?", (tid,)).fetchone())


def account(con, aid):
    return dict(con.execute("SELECT * FROM accounts WHERE account_id=?", (aid,)).fetchone())


# ---------- cancellation (agreement overrides SOP) ----------

def test_ord1001_northstar_cancel_no_fee(con):
    # Example Q1: BOOKED, 2h after booking. SOP would charge INR 250; agreement waives it.
    r = cancellation(order(con, "ORD-1001"), resolve_terms("ACCT-001"))
    assert r["cancellable"] is True and r["fee_inr"] == 0
    assert "waives" in r["reason"]


def test_ord2001_lumenworks_cancel_fee_applies(con):
    # LumenWorks has no waiver: requested 75 min after booking -> INR 250.
    r = cancellation(order(con, "ORD-2001"), resolve_terms("ACCT-002"))
    assert r["cancellable"] is True and r["fee_inr"] == 250


def test_ord3001_beacon_cancel_within_window(con):
    # Requested 15 min after booking -> within 30-min free window -> no fee.
    r = cancellation(order(con, "ORD-3001"), resolve_terms("ACCT-003"))
    assert r["fee_inr"] == 0


def test_ord1002_picked_up_not_cancellable(con):
    r = cancellation(order(con, "ORD-1002"), resolve_terms("ACCT-001"))
    assert r["cancellable"] is False and "return-to-origin" in r["reason"]


def test_ord4001_delivered_not_cancellable(con):
    r = cancellation(order(con, "ORD-4001"), resolve_terms("ACCT-004"))
    assert r["cancellable"] is False


# ---------- service credit (agreement overrides SOP threshold + amount) ----------

def test_ord2002_lumenworks_credit_fixed_300(con):
    # Carrier fault, window end 06:30, snapshot 11:00 = 4.5h > 4h contract threshold -> INR 300.
    r = service_credit(order(con, "ORD-2002"), resolve_terms("ACCT-002"))
    assert r["eligible"] is True and r["amount_inr"] == 300
    assert r["requires_manager_approval"] is False


def test_default_credit_rule_min_500_or_10pct():
    # Synthetic order under default SOP terms: fee 4200 -> 10% = 420 < 500 -> 420.
    o = {"pickup_actual_at": None, "carrier_fault": "True", "customer_fault": "False",
         "shipment_fee_inr": "4200", "pickup_window_end": "2026-08-16 06:30"}
    r = service_credit(o, DEFAULT_TERMS)
    assert r["eligible"] is True and r["amount_inr"] == 420


def test_no_credit_when_fault_unknown(con):
    # ORD-1001: no carrier fault recorded -> do not promise a credit.
    r = service_credit(order(con, "ORD-1001"), resolve_terms("ACCT-001"))
    assert r["eligible"] is False and r.get("requires_verification") is True


# ---------- SLA breach vs snapshot (agreement overrides plan default) ----------

def test_tkt501_northstar_p1_breached(con):
    # Northstar P1 = 15 min 24x7; created 10:30, snapshot 11:00 = 30 min -> breached.
    t = ticket(con, "TKT-501")
    r = sla_breach("ACCT-001", "Enterprise", "P1", t["created_at"])
    assert r["breached"] is True and r["target_minutes"] == 15


def test_tkt505_axis_security_p1_breached(con):
    # ACCT-004 no agreement -> Enterprise default P1 = 30 min 24x7; created 08:30 -> breached.
    t = ticket(con, "TKT-505")
    r = sla_breach("ACCT-004", "Enterprise", "P1", t["created_at"])
    assert r["breached"] is True and r["target_minutes"] == 30


# ---------- retrieval reliability + access scope ----------

def test_deprecated_policy_excluded_by_default(idx):
    hits = idx.search("first response targets enterprise plan", account_id=None, top_k=6)
    assert all(h["status"] != "deprecated" for h in hits)


def test_customer_cannot_retrieve_other_customers_agreement(idx):
    # Northstar (ACCT-001) searching LumenWorks terms must not get the LumenWorks agreement.
    hits = idx.search("LumenWorks fixed INR 300 failed pickup credit", account_id="ACCT-001", top_k=6)
    assert all(h["owner_account_id"] in (None, "ACCT-001") for h in hits)


def test_staff_can_retrieve_any_agreement(idx):
    hits = idx.search("LumenWorks fixed INR 300 failed pickup credit", account_id=None, top_k=6)
    assert any(h["owner_account_id"] == "ACCT-002" for h in hits)
