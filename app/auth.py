"""Real authentication: email + password login (bcrypt-hashed) issuing signed JWTs.

The JWT carries the identity (role + account scope) as signed claims, so /chat, /confirm,
etc. derive the AuthContext from the *verified* token — never from the request body. This
replaces the earlier mock role-picker: a client can no longer choose its own role.

Credentials live in a catalog seeded into the state store (Postgres/Supabase when
configured, else SQLite) so they persist; the catalog below is the seed for first boot.
"""
import os
import time
import uuid
from dataclasses import dataclass

import bcrypt
import jwt

JWT_SECRET = os.getenv("JWT_SECRET", "dev-insecure-change-me-in-prod")
JWT_ALG = "HS256"
JWT_TTL = int(os.getenv("JWT_TTL_SECONDS", str(8 * 3600)))
DOMAIN = os.getenv("AUTH_EMAIL_DOMAIN", "parcelpilot.duckdns.org")


@dataclass(frozen=True)
class AuthContext:
    role: str                      # "customer" | "staff"
    account_id: str | None = None  # customer's own account (scope); None for staff
    staff_role: str | None = None  # "agent" | "manager" | "admin" (staff only)
    email: str | None = None

    @property
    def is_customer(self) -> bool:
        return self.role == "customer"

    @property
    def is_manager(self) -> bool:
        return self.role == "staff" and self.staff_role in ("manager", "admin")

    @property
    def is_admin(self) -> bool:
        return self.role == "staff" and self.staff_role == "admin"

    def scope_account(self) -> str | None:
        return self.account_id if self.is_customer else None


# Which write actions each role may execute (before the separate confirmation gate).
WRITE_PERMISSIONS = {
    "customer": {"create_escalation"},
    "agent":    {"create_escalation", "update_ticket", "create_followup_task"},
    "manager":  {"create_escalation", "update_ticket", "create_followup_task", "approve_credit"},
    "admin":    {"create_escalation", "update_ticket", "create_followup_task", "approve_credit"},
}


def can_write(auth: AuthContext, action: str) -> bool:
    key = "customer" if auth.is_customer else (auth.staff_role or "agent")
    return action in WRITE_PERMISSIONS.get(key, set())


# --- credential catalog (seed). Password is the same demo value for every account. ---
_DEMO_PW = os.getenv("DEMO_PASSWORD", "Password123")

# email-localpart -> (role, account_id, staff_role, display)
_CATALOG = {
    "northstar":  ("customer", "ACCT-001", None, "Northstar Logistics · customer"),
    "lumenworks": ("customer", "ACCT-002", None, "LumenWorks · customer"),
    "beacon":     ("customer", "ACCT-003", None, "Beacon Retail · customer"),
    "agent":      ("staff", None, "agent", "Support agent · internal"),
    "manager":    ("staff", None, "manager", "Support manager · internal"),
    "admin":      ("staff", None, "admin", "Administrator · internal"),
}


def seed_users() -> dict:
    """email -> {hash, role, account_id, staff_role, label}. Hashes computed once."""
    users = {}
    for local, (role, acct, staff, label) in _CATALOG.items():
        email = f"{local}@{DOMAIN}"
        users[email] = {
            "hash": bcrypt.hashpw(_DEMO_PW.encode(), bcrypt.gensalt()),
            "role": role, "account_id": acct, "staff_role": staff, "label": label,
        }
    return users


def verify_password(user_row: dict, password: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), user_row["hash"])
    except Exception:
        return False


def context_from_user(email: str, user_row: dict) -> AuthContext:
    return AuthContext(role=user_row["role"], account_id=user_row["account_id"],
                       staff_role=user_row["staff_role"], email=email)


def issue_token(email: str, user_row: dict) -> tuple[str, str]:
    """Return (jwt, jti). jti keys the server-side conversation session."""
    jti = uuid.uuid4().hex
    now = int(time.time())
    claims = {
        "sub": email, "role": user_row["role"], "account_id": user_row["account_id"],
        "staff_role": user_row["staff_role"], "jti": jti, "iat": now, "exp": now + JWT_TTL,
    }
    return jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALG), jti


def verify_token(token: str) -> dict | None:
    """Return the verified claims, or None if invalid/expired/tampered."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        return None


def context_from_claims(claims: dict) -> AuthContext:
    return AuthContext(role=claims["role"], account_id=claims.get("account_id"),
                       staff_role=claims.get("staff_role"), email=claims.get("sub"))
