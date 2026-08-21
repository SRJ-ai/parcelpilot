# ParcelPilot AI Support System

An AI support agent for ParcelPilot (B2B logistics), serving **two user contexts** over one
shared, access-controlled tool layer:

- **Customer-facing** — a logged-in customer asks about their own account, orders, entitlements,
  cancellations, and credits. Hard-scoped to their `account_id`.
- **Internal ops** — authorised staff (agent / manager) investigate across all accounts, act on
  tickets, and see a proactive "needs attention" feed.

The agent reasons over the supplied data pack with explicit **source precedence** (signed agreement
> current policy/SOP > product docs > historical tickets, which are context-only), refuses to use
deprecated documents, computes money/SLA decisions **deterministically in code**, and requires
**explicit confirmation** before any state-changing action.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and [PRODUCT.md](PRODUCT.md) for the product note.

## Tools the agent chooses between

1. `search_documents` — BM25 retrieval over policy/SOP/product/agreement sections, each tagged with
   an authority tier and status; deprecated docs excluded, agreements scoped to their owner account.
2. `lookup_data` — scoped SQL over `accounts` / `orders` / `tickets`.
3. `compute_policy_outcome` — deterministic cancellation-fee, service-credit, and SLA-breach
   calculators that apply source precedence and the dataset snapshot time.
4. `create_escalation` / `update_ticket` / `create_followup_task` — state-changing, confirmation-gated.

## Run locally

Requires [uv](https://docs.astral.sh/uv/) and a free [Groq API key](https://console.groq.com).

```bash
cd parcelpilot
cp .env.example .env          # then put your GROQ_API_KEY in .env
uv run uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

### Offline (no API key) with Ollama

```bash
ollama pull qwen2.5 && ollama serve
LLM_PROVIDER=ollama uv run uvicorn app.main:app --port 8000
```

## Tests

The golden tests encode the deliberate traps in the data pack (agreement overrides SOP, deprecated
policy ignored, wrong historical resolutions, cross-account isolation) and run without an LLM:

```bash
uv run --group dev pytest -q
```

## Deploy (Docker)

```bash
docker build -t parcelpilot .
docker run -p 8000:8000 -e GROQ_API_KEY=sk-... parcelpilot
```

On Render / Railway / Fly: point at this Dockerfile and set `GROQ_API_KEY` (and optionally
`GROQ_MODEL`). The host's `PORT` is picked up automatically.

## Mock logins (role switcher in the UI)

| Login | Context | Scope |
|---|---|---|
| `customer_northstar` | Customer | ACCT-001 only |
| `customer_lumenworks` | Customer | ACCT-002 only |
| `customer_beacon` | Customer | ACCT-003 only |
| `staff_agent` | Internal | all accounts, no high-credit approval |
| `staff_manager` | Internal | all accounts + credit approval |

## Notes

- Data is loaded read-only from `data/` at startup; the mock action store (escalations/tickets/tasks)
  is in-memory and resets on restart.
- Dataset snapshot time (`2026-08-16 11:00 Asia/Kolkata`, from the workbook README) is the single
  reference "now" for every time-based calculation.
