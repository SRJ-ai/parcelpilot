# ParcelPilot AI Support System — Architecture Note

## 1. Overview

A single agent backend serving **two user contexts** over one shared data/tool layer:

- **Customer-facing chatbot** — a logged-in customer asks about *their own* account, orders, entitlements, cancellations, and credits. Hard-scoped to their `account_id`.
- **Internal ops chatbot** — authorised ParcelPilot staff (agent / manager roles) investigate across all accounts, act on tickets, and see a proactive "needs attention" feed.

Both contexts share the same tools, retrieval index, and structured data. The **only** difference is the `AuthContext` passed into the tool layer, which decides what rows and documents are visible and which actions are allowed. Access control lives in code, never in the prompt.

Stack: **Python 3.11 + FastAPI**, an **OpenAI-compatible LLM client** driving the native tool-calling loop (no agent framework) — **Groq** (hosted, `openai/gpt-oss-120b`) by default, with an **Ollama** backend (local or Ollama Cloud) as a drop-in offline fallback via the same wire format, **SQLite** (loaded from the workbook at startup) for structured data, **in-memory BM25** over section-chunked documents for retrieval, and a **vanilla HTML/JS** chat page with tool-activity badges and a confirmation modal.

## 2. Agent design

A single orchestrating agent driving the model's native tool-calling loop:

1. Receive the user message plus the server-side `AuthContext` (role + account scope).
2. The model plans and requests tools. The backend executes read/compute tools immediately, injecting the auth scope.
3. When the model requests a **state-changing** tool, the backend does **not** execute it. It returns the proposed action to the UI as a preview and pauses. The action runs only after the user explicitly confirms (see §6).
4. Loop until the model produces a final answer, or a stop condition (escalation, missing data, unsupported exception, or the step limit) is hit.

The system prompt encodes: the snapshot time, the source-precedence rule, the escalation triggers, and the "state uncertainty, don't hide it" behaviour from the policies. Crucially, the prompt states the *rules*; the tool layer *enforces* the ones that must be deterministic (access scope, fee/credit math, confirmation).

**Why no framework (LangChain/LlamaIndex):** the corpus is six short documents and the data is ~20 rows. A framework would add dependencies and indirection without buying anything. The native OpenAI-compatible tool loop gives full control over the two things that actually matter here — the confirmation gate and access scoping — which are awkward to bolt onto a generic agent runner.

**Why an OpenAI-compatible client (Groq / Ollama):** both backends speak the same wire format, so one thin adapter (`app/llm.py`) covers a fast hosted default (Groq) and a zero-cost offline path (Ollama, local or Ollama Cloud) by swapping only a base URL and model name. Groq additionally supports multiple API keys with round-robin load-spreading and automatic fail-over on rate limits, so one throttled key never takes the app down.

## 3. Tool design

Four tools, each receiving `AuthContext` from the server (never from the model):

1. **`search_documents(query, include_deprecated=false)`** — retrieval over policies, SOPs, product docs, and agreements.
   - Returns section chunks with authority metadata: `authority_tier`, `status` (current/deprecated), `effective` date, and `owner_account_id` for agreements.
   - Deprecated documents (v2) are excluded by default and only surfaced if a caller explicitly passes `include_deprecated=true` (e.g. to compare against historical policy).
   - Agreement chunks are filtered by `owner_account_id`: a customer only retrieves their own agreement plus general policy; they can never retrieve another customer's contract.

2. **`lookup_data(entity, filters)`** — structured lookup over `accounts`, `orders`, `tickets` in SQLite.
   - Every query is rewritten with a scope predicate: customers get `WHERE account_id = :ctx_account`; staff get full access. A customer-supplied `account_id` filter can never override their own scope. Enforced in the query builder, so a prompt-injection asking for another account returns nothing.

3. **`compute_policy_outcome(kind, order_id?, ticket_id?, severity?)`** — deterministic calculators that combine order/ticket data with the *resolved* policy. `kind` is one of `cancellation`, `service_credit` (both need `order_id`), or `sla_breach` (needs `ticket_id` + `severity`). This is the "calculation" tool and it is where source precedence is applied in code rather than left to the model (see §5).

4. **State-changing (mocked, confirmation-gated):** `create_escalation`, `update_ticket`, `create_followup_task`. These write to a local store and require explicit user confirmation before execution.

## 4. Document and structured-data handling

**Documents** are chunked by section (each numbered clause of each PDF becomes a chunk) and tagged at ingestion with authority metadata. Retrieval is BM25 over these chunks. Because the corpus is tiny and the hard part is *reliability*, not recall, effort goes into the metadata and precedence logic rather than embeddings. A vector store is the documented scale-up path if the corpus grows; it is not needed at this size and would be theatre here.

**Structured data** is loaded from `ParcelPilot_Assessment_Data.xlsx` into an in-memory SQLite database at startup (`accounts`, `orders`, `tickets`). SQLite is used precisely because it makes access scoping a clean `WHERE` clause and makes the calculation tool auditable. The dataset snapshot time from the README sheet (`2026-08-16 11:00 Asia/Kolkata`) is loaded as the single reference "now" for every time-based calculation, so "how late is the pickup" is deterministic and reproducible.

## 5. Source reliability and conflict handling

Every source gets an **authority tier** at ingestion:

| Tier | Source | Role |
|---|---|---|
| 4 | Signed customer agreement (active term) | Overrides everything for that account |
| 3 | Current support policy / SOP | Default rules |
| 2 | Current product documentation | Factual product behaviour |
| 1 | Historical ticket resolutions | Context only — **never authoritative**, may be wrong |
| 0 | Deprecated documents (v2) | Excluded from current answers |

Conflicts are resolved **deterministically in code** wherever an action or number depends on them, not left to model judgment. Worked examples from the actual data pack that the design must get right:

- **ORD-1001 (Northstar, cancel 2h after booking, BOOKED):** SOP §1 says INR 250 after 30 min; the Northstar agreement waives cancellation fees on any pre-pickup BOOKED shipment. `compute_policy_outcome` checks the tier-4 agreement first → **no fee**. The tier-1 historical ticket TKT-450 (which recorded INR 250 for exactly this case) is contradicted and is flagged, not followed.
- **ORD-2002 (LumenWorks, carrier fault, pickup window ends 06:30, snapshot 11:00):** default SOP threshold is 2h / lower of INR 500 or 10%; the LumenWorks agreement replaces this with 4h / fixed INR 300. 11:00 − 06:30 = 4.5h > 4h → **INR 300**.
- **TKT-451 / KI-208:** the historical resolution "Growth supports only 3,000 rows" conflicts with the product doc (limit 5,000; the 3,000-row failure is bug KI-208 with a split-file workaround). Tier-2 product doc wins; the historical answer is treated as wrong.

**Uncertainty is surfaced, not hidden.** Per SOP §3, the system refuses to promise a credit when carrier fault, pickup timing, or customer fault is unknown, and refuses to declare a pickup failed when KI-211 (webhook delay) could explain a stale BOOKED status. In those cases it explains the conflict/uncertainty and recommends verification or escalation.

**Escalation triggers** (human judgment required): P1 or suspected security incident (e.g. TKT-505 API-key exposure), an already-breached first-response SLA (e.g. TKT-501, Northstar P1 target 15 min, created 10:30, breached by snapshot 11:00), any individual credit above INR 1,000 (manager approval), conflicting or insufficient data, and any exception not supported by an agreement or current policy.

### 5.1 Grounding self-verification

Deterministic calculators keep the *numbers* correct, but the model still writes the prose around them. For a financial support agent, a confidently wrong sentence is the real adoption risk. So before a substantive answer reaches the user, a second strict pass (`app/verify.py`) judges the drafted answer against the tool evidence gathered that turn and returns `{grounded (0–1), supported, escalate, note}`.

When the answer is grounded, it is shown with a quiet **"Verified against sources"** pill. When it is **not** (grounded below threshold, an unsupported claim, or an over-reaching commitment), the agent does not show a hedged answer — it **abstains**: the draft is withheld (kept only for audit), an internal review escalation is auto-filed, and the user gets a plain "I'm not confident, a human will follow up" message plus a **"Routed to a human agent"** handoff with the escalation reference. For a financial agent, abstention beats a confidently wrong answer. The check fails open — a verifier hiccup never blocks an answer — is logged with a grounding score, and can be disabled with `GROUNDING_CHECK=0`. This turns "trust" from a prompt instruction into a measured property that actively gates what reaches the user.

## 6. Confirmation before actions

The confirmation gate lives in the orchestration layer, not the prompt. When the model requests a state-changing tool, the backend intercepts the tool call, returns a structured **preview** ("Escalate TKT-501 to P1, reason: SLA breach — confirm?") to the UI, and stops the loop. The action executes only when the user clicks confirm; on cancel, the decision is fed back to the model as a tool result so it can respond appropriately. This makes the guarantee independent of whether the model "remembered" to ask.

## 7. Access control and privacy

`AuthContext` is created server-side from the mocked login (a role switcher: *Customer: <account>*, *Staff: agent*, *Staff: manager*) and is never model-controllable. Enforcement points:

- **Structured data:** scope predicate injected into every query. Customers see only their own account/orders/tickets.
- **Documents:** agreement chunks filtered by `owner_account_id`; customers never retrieve another customer's contract.
- **Actions:** role gates — customers cannot escalate/modify tickets directly (their asks become staff escalations); credits above INR 1,000 require the `manager` role.

Because scoping happens in the tool layer, a prompt-injection or a customer asking "show me Northstar's contract" returns nothing rather than leaking data.

## 8. Interface

A single-page chat UI served by FastAPI: an animated **tool-activity trace** showing each tool as it runs (`search_documents("...")`, `lookup_data(orders, ...)`, `compute_policy_outcome(...)`) with a live/done state per step, a **confirmation card** for state-changing actions, and a **role switcher** to mock the two contexts. The client renders the model's markdown (tables, citations, lists) with a small self-contained parser. Internal (staff) sessions additionally get a **Proactive Attention panel** (Problem 1): deterministic rules over the live data surface open P1s, SLA breaches, security tickets, and clusters of the same known issue (e.g. repeated bulk-upload tickets → KI-208), ranked by urgency. (Responses render the full tool trace then the final answer; token streaming is a listed next step, not yet implemented.)

## 9. Major technical trade-offs

- **BM25 over a vector DB** — right-sized for six short docs; reliability metadata matters more than semantic recall here. Vector search is the documented scale path.
- **SQLite in-memory over a hosted DB** — the data is static and tiny; SQLite gives clean scoping and auditable calculations with zero ops.
- **Deterministic calculators over model arithmetic** — money and SLA decisions are computed in code so they are correct, testable, and reproducible; the model orchestrates and explains but does not do the math that drives an action.
- **No agent framework** — the native tool loop keeps the confirmation gate and access scoping explicit and under our control.
- **Confirmation enforced in orchestration, not prompt** — the safety guarantee cannot be defeated by the model forgetting to ask.

## 10. Production hardening

Beyond the assessment core, the app carries the controls a real deployment needs:

- **Server-issued session tokens** — identity is established once at `POST /session` (the mock authentication boundary) and returned as an opaque token; `/chat`, `/confirm`, and `/proactive` accept only that token, and the `AuthContext` is looked up server-side. A client cannot declare its own role or account in the request body — closing the impersonation hole where a customer could simply send `{"login": "staff_agent"}`.
- **Thread-safe data access** — the shared in-memory SQLite connection is serialized behind a lock (SQLite forbids concurrent use of one connection).
- **Bounded conversation history** — old turns are dropped at a `user` boundary (never orphaning a tool response) to cap token cost and context growth.
- **Resilience** — LLM request timeout with no double-retry (key fail-over is ours), and a graceful, user-facing message on provider failure.
- **Abuse controls** — request-body validation (message length), per-token rate limiting, session TTL and eviction.
- **Observability** — structured logs with a per-request id (`X-Request-ID`), an HTTP access line with latency, and tool-call / action / grounding traces.
- **Operational** — `/health` endpoint, security headers + CSP, a non-root container with a healthcheck, and CI running the offline test suite on every push.

The trust boundary and limits are covered by HTTP/auth integration tests (with the LLM mocked), so the impersonation defence and rate limits are verified, not assumed.
