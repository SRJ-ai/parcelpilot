"""Proactive attention feed — the emergent clustering must be data-driven: it should
discover ANY recurring theme and cross-customer impact from the text, not fire only on
a hardcoded known issue. We build a tiny in-memory dataset so the assertions don't move
when the real workbook changes.
"""
import sqlite3
import pytest

from app import proactive


def _con(tickets, accounts=None):
    """Minimal in-memory DB with the columns attention_feed touches."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE accounts (account_id TEXT, plan TEXT, account_name TEXT)")
    con.execute("CREATE TABLE tickets (ticket_id TEXT, account_id TEXT, status TEXT, "
                "subject TEXT, description TEXT, created_at TEXT)")
    con.execute("CREATE TABLE orders (order_id TEXT, account_id TEXT, carrier_fault TEXT, "
                "pickup_actual_at TEXT)")
    accts = accounts or [("ACCT-001", "Enterprise", "Northstar Logistics"),
                         ("ACCT-002", "Growth", "LumenWorks"),
                         ("ACCT-003", "Standard", "Beacon Retail")]
    con.executemany("INSERT INTO accounts VALUES (?,?,?)", accts)
    con.executemany(
        "INSERT INTO tickets VALUES (?,?,?,?,?,?)",
        [(t["ticket_id"], t["account_id"], t.get("status", "open"), t["subject"],
          t.get("description", ""), t.get("created_at", "2026-05-20T09:00:00")) for t in tickets])
    con.commit()
    return con


def _kinds(items):
    return [it["kind"] for it in items]


def test_discovers_a_novel_recurring_theme_not_in_known_issues():
    # A theme that matches NO known-issue signature must still cluster from the text alone.
    con = _con([
        {"ticket_id": "T1", "account_id": "ACCT-001", "subject": "Label printer misalignment on thermal rolls"},
        {"ticket_id": "T2", "account_id": "ACCT-001", "subject": "Printer misalignment again on thermal labels"},
    ])
    items = proactive.attention_feed(con)
    clusters = [it for it in items if it["kind"] in ("cluster", "cross_customer")]
    assert clusters, "should discover a recurring theme with no hardcoded rule"
    c = clusters[0]
    assert set(c["tickets"]) == {"T1", "T2"}
    assert c["known_issue"] is None  # proven data-driven, not a known-issue match


def test_cross_customer_theme_is_flagged_and_outranks_single_customer():
    con = _con([
        {"ticket_id": "T1", "account_id": "ACCT-001", "subject": "Tracking webhook not firing for deliveries"},
        {"ticket_id": "T2", "account_id": "ACCT-002", "subject": "Tracking webhook silent, no delivery updates"},
        {"ticket_id": "T3", "account_id": "ACCT-003", "subject": "Tracking webhook missing again today"},
    ])
    items = proactive.attention_feed(con)
    cross = [it for it in items if it["kind"] == "cross_customer"]
    assert cross, "a theme spanning 3 accounts must be flagged cross-customer"
    c = cross[0]
    assert len(c["accounts"]) == 3
    # cross-customer must outrank a single-customer recurrence of the same size
    single = _con([
        {"ticket_id": "A", "account_id": "ACCT-001", "subject": "Tracking webhook not firing"},
        {"ticket_id": "B", "account_id": "ACCT-001", "subject": "Tracking webhook silent today"},
    ])
    single_cluster = [it for it in proactive.attention_feed(single) if it["kind"] == "cluster"][0]
    assert c["urgency"] > single_cluster["urgency"]


def test_no_false_cluster_from_unrelated_tickets():
    con = _con([
        {"ticket_id": "T1", "account_id": "ACCT-001", "subject": "How do I change my billing contact"},
        {"ticket_id": "T2", "account_id": "ACCT-002", "subject": "Requesting an invoice copy for March"},
    ])
    items = proactive.attention_feed(con)
    assert not [it for it in items if it["kind"] in ("cluster", "cross_customer")]


def test_account_name_is_not_treated_as_a_theme():
    # Two Northstar tickets on different topics must NOT cluster on the word 'Northstar'.
    con = _con([
        {"ticket_id": "T1", "account_id": "ACCT-001", "subject": "Northstar wants to update billing address"},
        {"ticket_id": "T2", "account_id": "ACCT-001", "subject": "Northstar asks about invoice schedule"},
    ])
    items = proactive.attention_feed(con)
    assert not [it for it in items if it["kind"] in ("cluster", "cross_customer")]


def test_cluster_spans_open_and_closed_tickets():
    # A recurrence where the earlier ticket is already closed must still surface (the old
    # open-only rule missed exactly this — the KI-208 bulk-upload pair).
    con = _con([
        {"ticket_id": "OLD", "account_id": "ACCT-002", "status": "closed",
         "subject": "Bulk upload fails for large CSV"},
        {"ticket_id": "NEW", "account_id": "ACCT-002", "status": "open",
         "subject": "Bulk upload fails for 4,200-row CSV"},
    ])
    items = proactive.attention_feed(con)
    clusters = [it for it in items if it["kind"] == "cluster"]
    assert clusters and set(clusters[0]["tickets"]) == {"OLD", "NEW"}
    assert clusters[0]["known_issue"] == "KI-208"  # enriched with the known-issue ref


def test_security_ticket_still_top_ranked():
    con = _con([
        {"ticket_id": "T1", "account_id": "ACCT-004", "subject": "Possible API key exposure in logs"},
    ], accounts=[("ACCT-004", "Growth", "Acme")])
    items = proactive.attention_feed(con)
    assert items and items[0]["kind"] == "security"
