---
title: ParcelPilot Support Console
emoji: 📦
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 8000
pinned: false
---

# ParcelPilot AI Support System

An AI support agent for ParcelPilot (B2B logistics), serving **two user contexts** over one
shared, access-controlled tool layer:

- **Customer-facing** — a logged-in customer asks about their own account, orders, entitlements,
  cancellations, and credits. Hard-scoped to their `account_id`.
- **Internal ops** — authorised staff (agent / manager) investigate across all accounts, act on
  tickets, and see a proactive "needs attention" feed.

The agent reasons over the supplied data pack with explicit **source precedence** (signed agreement
> current policy/SOP > product docs > historical tickets, which are context-only), refuses to use
deprecated documents, computes money/SLA decisions **deterministically in code**, requires
**explicit confirmation** before any state-changing action, and runs an **answer-grounding self-check**
that flags any answer whose claims aren't backed by the retrieved evidence.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design, [PRODUCT.md](PRODUCT.md) for the product note,
and [DEMO_SCRIPT.md](DEMO_SCRIPT.md) for the ~5-minute demo walkthrough.

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

## Deploy

**One-click (Render blueprint).** This repo ships a [`render.yaml`](render.yaml). In Render:
*New → Blueprint → select this repo → set `GROQ_API_KEY`*. It builds the Dockerfile, wires the
`/health` check, and injects `PORT` automatically — no manual dashboard config.

**Any Docker host (Railway / Fly / Koyeb / HF Spaces).**

```bash
docker build -t parcelpilot .
docker run -p 8000:8000 -e GROQ_API_KEY=sk-... parcelpilot
```

Point the host at this Dockerfile and set `GROQ_API_KEY` (optionally `GROQ_MODEL`). The container
runs as a non-root user, reads the host's `PORT`, and exposes a `/health` endpoint used by the
built-in `HEALTHCHECK`. The production start command (`uv run --no-dev uvicorn app.main:app`) and the
healthcheck are verified to boot with prod-only dependencies.

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

## AI tool usage

This project was built with an AI coding assistant (Claude, via the Hermes agent CLI) used as a
pair-programmer, not an autopilot. Concretely:

- **Design & trade-offs** — I drove the architecture decisions (native tool loop over a framework,
  authority tiers, deterministic calculators, defence-in-depth for trust); the assistant was used to
  pressure-test them and draft alternatives I then accepted or rejected.
- **Implementation** — used for scaffolding modules (retrieval, tools, orchestrator, UI) and
  boilerplate (tool schemas, FastAPI handlers), with every generated block reviewed, edited, and
  verified against the data pack before it landed.
- **Tests** — the golden-trap suite was co-written to pin the specific contradictions in the corpus
  (agreement overrides, deprecated policy, wrong historical resolutions, cross-account isolation);
  I chose *what* to test, the assistant helped write the cases.
- **Docs** — [ARCHITECTURE.md](ARCHITECTURE.md) and [PRODUCT.md](PRODUCT.md) were drafted with the
  assistant and edited by me for accuracy; claims were checked against the running code.

Judgment calls — what to build, what to leave out, how the product should behave, and which claims
are true — are mine. The assistant accelerated the work; it didn't make the decisions.
