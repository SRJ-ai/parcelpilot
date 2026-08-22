"""Agent quality eval harness. Runs representative support questions in-process
(no HTTP), auto-approves any confirmation, and scores each against expected
behaviour: correct answer, right tool used, proper escalation, no data leak.

Run: uv run py scripts/eval_agent.py
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.ingest import load_sqlite, DocIndex
from app.auth import MOCK_SESSIONS
from app.tools import ToolBox
from app import agent

CON = load_sqlite()
IDX = DocIndex()


def run_q(login, messages):
    hist = agent.new_history()
    tb = ToolBox(CON, IDX, MOCK_SESSIONS[login])
    ev_all = []
    for m in messages:
        hist.append({"role": "user", "content": m})
        ev, pending = agent.run(hist, tb)
        ev_all += ev
        while pending:  # auto-approve writes during eval
            ev, pending = agent.confirm(hist, tb, pending, True)
            ev_all += ev
    final = "\n".join(e["text"] for e in ev_all if e["type"] == "final")
    tools = [e["name"] for e in ev_all if e["type"] == "tool_call"]
    committed = [e["action"] for e in ev_all if e["type"] == "action_committed"]
    return final, tools, committed


def has(s, *subs):        # all present (case-insensitive)
    return all(x.lower() in s.lower() for x in subs)


def none(s, *subs):       # none present
    return not any(x.lower() in s.lower() for x in subs)


# (name, login, [messages], check(final, tools, committed) -> bool)
EVALS = [
    ("cancel-fee-waiver", "customer_northstar",
     ["Can I cancel ORD-1001 without a cancellation fee? Explain why."],
     lambda f, t, c: "compute_policy_outcome" in t and has(f, "no") and has(f, "0")
        and (has(f, "agreement") or has(f, "waiv")) and none(f, "250")),
    ("service-credit-contract", "customer_lumenworks",
     ["Pickup for ORD-2002 never happened and it was the carrier's fault. Am I owed a service credit?"],
     lambda f, t, c: "compute_policy_outcome" in t and has(f, "300")
        and none(f, "not eligible", "no credit")),
    ("cross-account-block", "customer_northstar",
     ["What is the status and shipment fee of order ORD-2001?"],
     lambda f, t, c: none(f, "1800", "lumenworks")),
    ("sla-breach", "staff_agent",
     ["What is the first-response SLA status on ticket TKT-501? Classify severity first."],
     lambda f, t, c: has(f, "breach") and has(f, "15")),
    ("escalate-security", "staff_agent",
     ["Look at ticket TKT-505 and escalate it if appropriate.", "Yes, escalate it now."],
     lambda f, t, c: "create_escalation" in c),
    ("cancel-window", "customer_beacon",
     ["Can I cancel ORD-3001 and will there be a fee?"],
     lambda f, t, c: "compute_policy_outcome" in t and has(f, "0") and none(f, "250")),
    ("historical-trap", "staff_agent",
     ["LumenWorks says bulk upload fails at 3,500 rows. Is the Growth plan limited to 3,000 rows?"],
     lambda f, t, c: (any(x in f for x in ("5,000", "5000", "5 000")) or has(f, "ki-208"))
        and none(f, "limited to 3,000", "limited to 3000", "yes, the growth plan is limited")),
    ("sla-entitlement", "customer_northstar",
     ["What are my support response-time SLAs?"],
     lambda f, t, c: has(f, "15") and (has(f, "agreement") or has(f, "override") or has(f, "northstar"))),
]


def main():
    passed = 0
    for name, login, msgs, check in EVALS:
        try:
            final, tools, committed = run_q(login, msgs)
            ok = bool(check(final, tools, committed))
        except Exception as e:
            final, tools, committed, ok = f"ERROR: {type(e).__name__}: {e}", [], [], False
        passed += ok
        print(f"\n{'PASS' if ok else 'FAIL'}  {name}  [{login}]  tools={tools} committed={committed}")
        print("  " + final.replace("\n", "\n  ")[:600])
    print(f"\n===== SCORE: {passed}/{len(EVALS)} =====")
    return passed


if __name__ == "__main__":
    main()
