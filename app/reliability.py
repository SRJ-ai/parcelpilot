"""Source-precedence resolution + deterministic policy calculators.

Money and SLA decisions are computed here, in code, so they are correct, testable,
and reproducible. The agent orchestrates and explains but does not do the arithmetic
that drives an action.

Contract-specific parameters are structured at ingestion (ACCOUNT_TERMS / SLA_*),
each annotated with its source clause. Free-text extraction of contract numbers is
the documented scale-up path; for two contracts a structured table is safer and
auditable. Anything a customer's order actually determines (timing, fault, fee) is
computed live from the workbook + the dataset snapshot, never hardcoded.
"""
from datetime import datetime
from app.config import SNAPSHOT_NOW, IST

# ---------- resolved terms (SOP defaults + agreement overrides) ----------

DEFAULT_TERMS = {
    "cancel_free_window_min": 30,          # SOP v4 §1
    "cancel_fee_inr": 250,                 # SOP v4 §1
    "cancel_fee_waived": False,
    "credit_threshold_hours": 2.0,         # SOP v4 §2
    "credit_rule": "min_500_or_10pct",     # SOP v4 §2: lower of INR 500 or 10% of fee
    "credit_fixed_inr": None,
    "credit_monthly_cap_inr": None,
    "manager_approval_above_inr": 1000,    # SOP v4 §3
    "terms_source": "Cancellation & Service Credit SOP v4 (default policy)",
}

# Overrides derived from the signed agreements (tier 4). Merged onto DEFAULT_TERMS.
ACCOUNT_TERMS = {
    "ACCT-001": {  # Northstar Enterprise Agreement
        "cancel_fee_waived": True,          # §2: cancel any pre-pickup BOOKED, no fee
        "credit_monthly_cap_inr": 5000,     # §3: monthly aggregate credits capped
        "terms_source": "Northstar Enterprise Agreement §2 (fee waiver), §3 (INR 5,000 monthly cap); SOP v4 for credit rule",
    },
    "ACCT-002": {  # LumenWorks Service Agreement
        "credit_threshold_hours": 4.0,      # §3 replaces default 2h threshold
        "credit_rule": "fixed",             # §3
        "credit_fixed_inr": 300,            # §3: fixed INR 300
        "terms_source": "LumenWorks Service Agreement §3 (4h threshold, fixed INR 300); SOP v4 otherwise",
    },
}


def resolve_terms(account_id: str) -> dict:
    terms = dict(DEFAULT_TERMS)
    terms.update(ACCOUNT_TERMS.get(account_id, {}))
    return terms


# ---------- SLA first-response targets ----------
# (target_minutes, coverage). coverage "24x7" -> wall-clock breach is exact;
# "business" -> depends on the business calendar, so we do NOT assert a breach.

SLA_DEFAULT = {  # Support Policy v3 §3
    "Enterprise": {"P1": (30, "24x7"), "P2": (120, "business"), "P3": (None, "business")},
    "Growth":     {"P1": (120, "business"), "P2": (240, "business"), "P3": (None, "business")},
    "Standard":   {"P1": (240, "business"), "P2": (None, "business"), "P3": (None, "business")},
}

SLA_ACCOUNT = {  # agreement overrides
    "ACCT-001": {  # Northstar §1
        "P1": (15, "24x7"), "P2": (60, "24x7"), "P3": (None, "business"),
        "source": "Northstar Enterprise Agreement §1",
    },
    "ACCT-002": {  # LumenWorks §1 (no weekend/after-hours)
        "P1": (120, "business"), "P2": (240, "business"), "P3": (None, "business"),
        "source": "LumenWorks Service Agreement §1",
    },
}


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt


# ---------- calculators ----------


def cancellation(order: dict, terms: dict) -> dict:
    """Fee + eligibility for a cancellation, per resolved terms. `order` is a dict row."""
    status = order["status"]
    if status == "DRAFT":
        return {"cancellable": True, "fee_inr": 0, "reason": "DRAFT: cancel with no fee (SOP v4 §1)."}
    if status == "PICKED_UP":
        return {"cancellable": False, "fee_inr": None,
                "reason": "PICKED_UP: do not cancel; use return-to-origin workflow (SOP v4 §1)."}
    if status == "DELIVERED":
        return {"cancellable": False, "fee_inr": None, "reason": "DELIVERED: cannot be cancelled (SOP v4 §1)."}
    # BOOKED, not yet picked up
    if terms["cancel_fee_waived"]:
        return {"cancellable": True, "fee_inr": 0,
                "reason": f"BOOKED, cancellable with no fee: agreement waives cancellation fee ({terms['terms_source']})."}
    booked = _parse(order["booked_at"])
    # Free window is measured to when cancellation was requested, not the snapshot.
    requested = _parse(order["cancellation_requested_at"]) if order.get("cancellation_requested_at") else SNAPSHOT_NOW
    mins = (requested - booked).total_seconds() / 60
    if mins <= terms["cancel_free_window_min"]:
        return {"cancellable": True, "fee_inr": 0,
                "reason": f"BOOKED, cancel requested {mins:.0f} min after booking (within {terms['cancel_free_window_min']}-min free window): no fee (SOP v4 §1)."}
    return {"cancellable": True, "fee_inr": terms["cancel_fee_inr"],
            "reason": f"BOOKED, {mins:.0f} min after booking (> {terms['cancel_free_window_min']}-min free window): INR {terms['cancel_fee_inr']} fee (SOP v4 §1)."}


def service_credit(order: dict, terms: dict) -> dict:
    """Failed-pickup service credit eligibility + amount, per resolved terms."""
    carrier_fault = str(order["carrier_fault"]).lower() in ("true", "1")
    customer_fault = str(order["customer_fault"]).lower() in ("true", "1")
    fee = float(order["shipment_fee_inr"])

    # Only failed/late pickups qualify; a completed pickup or delivery is not a failed pickup.
    if order["pickup_actual_at"]:
        return {"eligible": False, "amount_inr": 0,
                "reason": "Pickup was completed; failed-pickup credit does not apply."}
    if not carrier_fault or customer_fault:
        return {"eligible": False, "amount_inr": 0, "requires_verification": True,
                "reason": "Not eligible or unverified: credit requires confirmed carrier fault and no customer fault (SOP v4 §2/§3). Do not promise a credit while fault is unknown."}

    window_end = _parse(order["pickup_window_end"])
    hours_late = (SNAPSHOT_NOW - window_end).total_seconds() / 3600
    if hours_late <= terms["credit_threshold_hours"]:
        return {"eligible": False, "amount_inr": 0,
                "reason": f"Pickup is {hours_late:.1f}h past window end; below the {terms['credit_threshold_hours']}h threshold ({terms['terms_source']})."}

    if terms["credit_rule"] == "fixed":
        amount = terms["credit_fixed_inr"]
        basis = f"fixed INR {amount}"
    else:  # min_500_or_10pct
        amount = min(500, round(0.10 * fee))
        basis = f"lower of INR 500 or 10% of INR {fee:.0f} = INR {amount}"

    needs_mgr = amount > terms["manager_approval_above_inr"]
    return {
        "eligible": True, "amount_inr": amount,
        "hours_late": round(hours_late, 1),
        "requires_manager_approval": needs_mgr,
        "monthly_cap_inr": terms["credit_monthly_cap_inr"],
        "reason": f"Eligible: {hours_late:.1f}h late (> {terms['credit_threshold_hours']}h), carrier fault, no customer fault. Credit = {basis} ({terms['terms_source']})."
                  + (f" Exceeds INR {terms['manager_approval_above_inr']} -> manager approval required (SOP v4 §3)." if needs_mgr else ""),
    }


def sla_breach(account_id: str, plan: str, severity: str, created_at: str) -> dict:
    """First-response SLA status vs the dataset snapshot. Agreement overrides the plan default."""
    sev = severity.upper()
    if account_id in SLA_ACCOUNT and sev in SLA_ACCOUNT[account_id]:
        target_min, coverage = SLA_ACCOUNT[account_id][sev]
        source = SLA_ACCOUNT[account_id]["source"]
    else:
        target_min, coverage = SLA_DEFAULT.get(plan, SLA_DEFAULT["Standard"]).get(sev, (None, "business"))
        source = "Support Policy v3 §3"
    elapsed_min = (SNAPSHOT_NOW - _parse(created_at)).total_seconds() / 60
    res = {"severity": sev, "target_minutes": target_min, "coverage": coverage,
           "elapsed_minutes": round(elapsed_min), "source": source}
    if target_min is None:
        res.update({"breached": None, "reason": f"{sev} target is in business days; business-calendar check needed ({source})."})
    elif coverage == "24x7":
        breached = elapsed_min > target_min
        res.update({"breached": breached,
                    "reason": f"{sev} 24x7 target {target_min} min; elapsed {elapsed_min:.0f} min -> {'BREACHED' if breached else 'within target'} ({source})."})
    else:
        res.update({"breached": None,
                    "reason": f"{sev} target {target_min} business min; breach depends on business hours/weekend coverage — verify calendar before asserting ({source})."})
    return res
