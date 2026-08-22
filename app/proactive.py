"""Problem 1: proactive attention feed for internal staff. Deterministic rules over
live data — no LLM needed. Heuristic severity is flagged as heuristic; staff confirm.

Detection is data-driven, not hard-coded to one known issue: single-ticket signals
(security, outage, SLA breach) plus emergent clustering that finds ANY recurring theme
across tickets and flags when a theme spans multiple customers at once.
"""
import re
from collections import defaultdict

from app.ingest import DB_LOCK
from app.reliability import sla_breach

SECURITY_RE = re.compile(r"api key|credential|exposure|password|secret|leak", re.I)
OUTAGE_RE = re.compile(r"all .*fail|every user|http 500|outage|cannot create|can't create|all shipment", re.I)

# Known-issue enrichment: when a discovered cluster matches one of these signatures we
# attach the reference + workaround. This ENRICHES a data-driven cluster; it never gates
# whether a cluster is surfaced, so brand-new recurring themes still show up.
KNOWN_ISSUES = [
    (re.compile(r"bulk upload|large csv|\d[\d,]*\s*-?\s*row", re.I),
     "KI-208", "Bulk Upload fails above ~3,000 rows. Workaround: split files below 3,000 rows."),
    (re.compile(r"still shows booked|booked after|webhook|status not updat", re.I),
     "KI-211", "SwiftShip webhook delay can leave a picked-up order showing BOOKED. Verify before declaring a failed pickup."),
]

# Tokens with no diagnostic value for theme discovery.
_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to", "of", "for",
    "in", "on", "at", "and", "or", "but", "if", "we", "i", "you", "it", "this", "that",
    "how", "do", "does", "did", "can", "cant", "cannot", "with", "from", "our", "my",
    "still", "after", "not", "no", "has", "have", "had", "will", "would", "should",
    "when", "what", "why", "where", "which", "who", "get", "got", "us", "as", "by",
    "up", "out", "all", "any", "some", "please", "help", "issue", "issues", "problem",
}
_WORD = re.compile(r"[a-z0-9]+", re.I)


def _guess_severity(text: str) -> str:
    if SECURITY_RE.search(text) or OUTAGE_RE.search(text):
        return "P1"
    if re.search(r"fail|error|degraded|exposure|down", text, re.I):
        return "P2"
    return "P3"


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP and len(w) > 2]


def _keys(text: str) -> set[str]:
    """Candidate theme keys for one ticket: significant unigrams + adjacent bigrams.
    Bigrams give specificity ('bulk upload' beats 'bulk'); unigrams give recall."""
    toks = _tokens(text)
    keys = set(toks)
    keys.update(f"{a} {b}" for a, b in zip(toks, toks[1:]))
    return keys


def _match_known(text: str):
    for pat, ref, note in KNOWN_ISSUES:
        if pat.search(text):
            return ref, note
    return None, None


def _clusters(tickets: list[dict]) -> list[dict]:
    """Discover recurring themes across tickets, entirely from the text — no fixed theme
    list. A cluster is any key shared by >=2 tickets; overlapping keys are collapsed to
    the most specific representative so we surface one item per real theme."""
    by_key_tickets = defaultdict(set)   # key -> {ticket_id}
    key_is_bigram = {}
    for t in tickets:
        blob = f"{t['subject']} {t.get('description', '')}"
        for k in _keys(blob):
            by_key_tickets[k].add(t["ticket_id"])
            key_is_bigram[k] = " " in k

    # Keep only recurring keys (>=2 tickets), then dedupe keys that cover the same (or a
    # subset of the same) ticket set — preferring bigrams, then longer strings.
    recurring = {k: tids for k, tids in by_key_tickets.items() if len(tids) >= 2}
    chosen: dict[frozenset, str] = {}
    for k in sorted(recurring, key=lambda k: (key_is_bigram[k], len(k)), reverse=True):
        tids = frozenset(recurring[k])
        # skip if an already-chosen key's ticket set is a superset of this one
        if any(tids <= existing for existing in chosen):
            continue
        chosen[tids] = k

    idx = {t["ticket_id"]: t for t in tickets}
    out = []
    for tids, key in chosen.items():
        members = [idx[i] for i in tids]
        accounts = sorted({m["account_id"] for m in members})
        open_ids = sorted(m["ticket_id"] for m in members if m["status"] == "open")
        all_ids = sorted(m["ticket_id"] for m in members)
        blob = " ".join(f"{m['subject']} {m.get('description','')}" for m in members)
        ref, note = _match_known(blob)
        cross = len(accounts) >= 2

        # Urgency: cross-customer themes rank above single-customer recurrences; more
        # tickets and an open ticket in the cluster push it higher.
        urgency = 55 + (20 if cross else 0) + min(len(all_ids), 4) * 3 + (5 if open_ids else 0)

        if cross:
            title = f"Cross-customer issue: “{key}” across {len(accounts)} accounts"
            detail = (f"{len(all_ids)} tickets from {', '.join(accounts)} share this theme "
                      f"({', '.join(all_ids)}). Multiple customers affected — check for a systemic cause.")
            kind = "cross_customer"
        else:
            title = f"Recurring issue: “{key}” ({len(all_ids)} tickets)"
            detail = (f"Tickets {', '.join(all_ids)} ({accounts[0]}) recur on the same theme"
                      + (f"; open now: {', '.join(open_ids)}." if open_ids else " (all resolved)."))
            kind = "cluster"
        if ref:
            detail += f" Matches known issue {ref}: {note}"
        out.append({"urgency": urgency, "kind": kind, "title": title, "detail": detail,
                    "tickets": all_ids, "theme": key, "accounts": accounts,
                    "known_issue": ref})
    return out


def attention_feed(con) -> list[dict]:
    with DB_LOCK:
        all_tickets = [dict(r) for r in con.execute("SELECT * FROM tickets").fetchall()]
        plans = {r["account_id"]: r["plan"] for r in con.execute("SELECT account_id, plan FROM accounts").fetchall()}
    open_tickets = [t for t in all_tickets if t["status"] == "open"]
    items = []

    # ---- single-ticket signals (open tickets only) ----
    for t in open_tickets:
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

    # ---- emergent clustering (all tickets, so a recurrence spanning open + recently
    # closed tickets is visible; a one-off open ticket never clusters with itself) ----
    items.extend(_clusters(all_tickets))

    # ---- operational: outstanding carrier-fault pickups (service credit likely owed) ----
    with DB_LOCK:
        orders = [dict(r) for r in con.execute(
            "SELECT * FROM orders WHERE carrier_fault IN ('True','1') AND pickup_actual_at IS NULL").fetchall()]
    for o in orders:
        items.append({"urgency": 60, "kind": "ops",
                      "title": f"Outstanding carrier-fault pickup — {o['order_id']}",
                      "detail": f"{o['account_id']}: pickup not completed, carrier at fault. Likely service credit owed — review.",
                      "tickets": []})

    items.sort(key=lambda x: x["urgency"], reverse=True)
    return items
