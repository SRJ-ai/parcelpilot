"""Static configuration: dataset snapshot, document registry, authority tiers.

The dataset snapshot time is the single reference "now" for every time-based
calculation (pickup lateness, SLA breach). It comes from the workbook README
sheet and is loaded here so results are deterministic and reproducible.
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# README sheet: "Dataset snapshot" = 2026-08-16 11:00 Asia/Kolkata (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))
SNAPSHOT_NOW = datetime(2026, 8, 16, 11, 0, tzinfo=IST)
CURRENCY = "INR"

# Authority tiers (higher wins on conflict). Historical tickets = tier 1 (context
# only, never authoritative). Deprecated docs = tier 0 (excluded from current answers).
TIER_AGREEMENT = 4
TIER_POLICY = 3
TIER_PRODUCT = 2
TIER_HISTORICAL = 1
TIER_DEPRECATED = 0

# Document registry: metadata assigned at ingestion. owner_account_id restricts
# agreement visibility so a customer can never retrieve another customer's contract.
DOCUMENTS = [
    {
        "file": "01_Support_Policy_v3_CURRENT.pdf",
        "title": "Support Policy v3 (CURRENT)",
        "doc_type": "policy",
        "status": "current",
        "authority_tier": TIER_POLICY,
        "effective": "2026-05-01",
        "owner_account_id": None,
    },
    {
        "file": "02_Support_Policy_v2_DEPRECATED.pdf",
        "title": "Support Policy v2 (DEPRECATED)",
        "doc_type": "policy",
        "status": "deprecated",
        "authority_tier": TIER_DEPRECATED,
        "effective": "2025-01-01",
        "owner_account_id": None,
    },
    {
        "file": "03_Cancellation_and_Service_Credit_SOP_v4.pdf",
        "title": "Cancellation & Service Credit SOP v4",
        "doc_type": "sop",
        "status": "current",
        "authority_tier": TIER_POLICY,
        "effective": "2026-06-15",
        "owner_account_id": None,
    },
    {
        "file": "04_Product_Operations_Guide_and_Known_Issues.pdf",
        "title": "Product Operations Guide & Known Issues",
        "doc_type": "product_doc",
        "status": "current",
        "authority_tier": TIER_PRODUCT,
        "effective": "2026-08-14",
        "owner_account_id": None,
    },
    {
        "file": "05_Northstar_Logistics_Enterprise_Agreement.pdf",
        "title": "Northstar Logistics Enterprise Agreement",
        "doc_type": "agreement",
        "status": "current",
        "authority_tier": TIER_AGREEMENT,
        "effective": "2026-01-01",
        "owner_account_id": "ACCT-001",
    },
    {
        "file": "06_LumenWorks_Service_Agreement.pdf",
        "title": "LumenWorks Service Agreement",
        "doc_type": "agreement",
        "status": "current",
        "authority_tier": TIER_AGREEMENT,
        "effective": "2026-03-01",
        "owner_account_id": "ACCT-002",
    },
]

WORKBOOK = "ParcelPilot_Assessment_Data.xlsx"
