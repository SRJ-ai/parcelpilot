"""AuthContext + role gates. Created server-side from the mocked login; never
model-controllable. The tool layer reads scope from here, so a prompt-injection or a
customer asking for another account's data cannot widen access.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthContext:
    role: str                     # "customer" | "staff"
    account_id: str | None = None # customer's own account (scope); None for staff
    staff_role: str | None = None # "agent" | "manager" (staff only)

    @property
    def is_customer(self) -> bool:
        return self.role == "customer"

    @property
    def is_manager(self) -> bool:
        return self.role == "staff" and self.staff_role == "manager"

    def scope_account(self) -> str | None:
        """Account filter for structured/document queries. None = unrestricted (staff)."""
        return self.account_id if self.is_customer else None


# Which write actions each role may execute (before the separate confirmation gate).
WRITE_PERMISSIONS = {
    "customer": {"create_escalation"},                                  # customers can only escalate
    "agent":    {"create_escalation", "update_ticket", "create_followup_task"},
    "manager":  {"create_escalation", "update_ticket", "create_followup_task", "approve_credit"},
}


def can_write(auth: AuthContext, action: str) -> bool:
    key = "customer" if auth.is_customer else (auth.staff_role or "agent")
    return action in WRITE_PERMISSIONS.get(key, set())


# Mocked logins exposed by the role switcher in the UI.
MOCK_SESSIONS = {
    "customer_northstar": AuthContext("customer", account_id="ACCT-001"),
    "customer_lumenworks": AuthContext("customer", account_id="ACCT-002"),
    "customer_beacon": AuthContext("customer", account_id="ACCT-003"),
    "staff_agent": AuthContext("staff", staff_role="agent"),
    "staff_manager": AuthContext("staff", staff_role="manager"),
}
