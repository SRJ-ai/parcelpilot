"""Problem 1: proactive attention feed for internal staff. Deterministic rules over
live data — no LLM needed. Heuristic severity is flagged as heuristic; staff confirm.
"""
import re
from app.reliability import sla_breach

SECURITY_RE = re.compile(r"api key|credential|exposure|password|secret|leak", re.I)
OUTAGE_RE = re.compile(r"all .*fail|every user|http 500|outage|cannot create|can't create|all shipment", re.I)
BULK_RE = re.compile(r"bulk upload|csv", re.I)


def _guess_severity(text: str) -> str:
    if SECURITY_RE.search(text) or OUTAGE_RE.search(text):
        return "P1"
    if BULK_RE.search(text) or re.search(r"fail|error|degraded", text, re.I):
        return "P2"
    return "P3"


def attention_feed(con) -> list[dict]:
    tickets = [dict(r) for r in con.execute("SELECT * FROM tickets WHERE status='open'").fetchall()]
    plans = {r["account_id"]: r["plan"] for r in con.execute("SELECT account_id, plan FROM accounts").fetchall()}
    items = []

    bulk_hits = []
    for t in tickets:
        text = f"{t['subject']} {t['description']}"
        sev = _guess_severity(text)

        if SECURITY_RE.search(text):
            items.append({"urgency": 100, "kind": "security",
                          "title": f"Suspected security incident — {t['ticket_id']}",
                          "detail": f"{t['account_id']}: {t['subject']}. Treat as P1; escalate immediately.",
                          "tickets": [t["ticket_id"]]})
        elif OUTAGE_RE.search(text):
            items.append({"urgency": 90, "kind": "outage",
                          "title": f"Possible P1 outage — {t['ticket_id']}",
                          "detail": f"{t['account_id']}: {t['subject']}.",
                          "tickets": [t["ticket_id"]]})

        sla = sla_breach(t["account_id"], plans.get(t["account_id"], "Standard"), sev, t["created_at"])
        if sla["breached"]:
            items.append({"urgency": 80, "kind": "sla_breach",
                          "title": f"SLA breached ({sev}) — {t['ticket_id']}",
                          "detail": f"{t['account_id']}: elapsed {sla['elapsed_minutes']} min vs {sla['target_minutes']} min target ({sla['source']}). Severity is heuristic — confirm.",
                          "tickets": [t["ticket_id"]]})

        if BULK_RE.search(text):
            bulk_hits.append(t["ticket_id"])

    if len(bulk_hits) >= 2:
        items.append({"urgency": 70, "kind": "cluster",
                      "title": f"Recurring issue: bulk upload failures ({len(bulk_hits)} tickets)",
                      "detail": f"Tickets {', '.join(bulk_hits)} match known issue KI-208 (Bulk Upload failures >~3,000 rows). Workaround: split files below 3,000 rows.",
                      "tickets": bulk_hits})

    # operational: outstanding carrier-fault pickups (service credit likely owed)
    orders = [dict(r) for r in con.execute(
        "SELECT * FROM orders WHERE carrier_fault IN ('True','1') AND pickup_actual_at IS NULL").fetchall()]
    for o in orders:
        items.append({"urgency": 60, "kind": "ops",
                      "title": f"Outstanding carrier-fault pickup — {o['order_id']}",
                      "detail": f"{o['account_id']}: pickup not completed, carrier at fault. Likely service credit owed — review.",
                      "tickets": []})

    items.sort(key=lambda x: x["urgency"], reverse=True)
    return items
