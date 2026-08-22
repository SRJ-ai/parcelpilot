"""Source-document access control + serving.

Customers see the general policies/SOPs/product docs plus *their own* signed agreement.
Internal staff see everything, including other customers' agreements and the operational
workbook. The deprecated policy is staff-only. Access is decided here, server-side, from
the verified AuthContext — never from the client.
"""
from app.config import DOCUMENTS, DATA_DIR, WORKBOOK

WORKBOOK_ID = "data"


def doc_id(file: str) -> str:
    return file.split("_", 1)[0] if file[:2].isdigit() else file.rsplit(".", 1)[0]


def _entry(d: dict) -> dict:
    return {"id": doc_id(d["file"]), "file": d["file"], "title": d["title"],
            "doc_type": d["doc_type"], "status": d["status"], "owner_account_id": d["owner_account_id"]}


def visible_docs(auth) -> list[dict]:
    """List of docs this user may open."""
    out = []
    for d in DOCUMENTS:
        owner = d["owner_account_id"]
        if d["status"] == "deprecated" and auth.is_customer:
            continue  # customers never see the deprecated policy
        if owner is None:
            out.append(_entry(d))                       # general policy/product docs → everyone
        elif not auth.is_customer:
            out.append(_entry(d))                       # staff see every agreement
        elif owner == auth.account_id:
            out.append(_entry(d))                       # customer sees only their own agreement
    if not auth.is_customer:                            # workbook: staff only
        out.append({"id": WORKBOOK_ID, "file": WORKBOOK, "title": "Operational Dataset (accounts, orders, tickets)",
                    "doc_type": "workbook", "status": "current", "owner_account_id": None})
    return out


def resolve(auth, did: str) -> dict | None:
    for d in visible_docs(auth):
        if d["id"] == did:
            return d
    return None


def path_for(entry: dict):
    return DATA_DIR / entry["file"]


def media_type(file: str) -> str:
    if file.lower().endswith(".pdf"):
        return "application/pdf"
    if file.lower().endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "application/octet-stream"
