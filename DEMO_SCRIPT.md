# ParcelPilot — Demo Video Script (~5 minutes)

A tight, timed shot-list for the submission video. Covers the three required beats: **solution
architecture**, **a working demo**, and **key product/technical decisions**. Times are cumulative.
Everything below uses real logins and real records from the data pack (verified), so nothing needs
faking. Rehearse once; the whole thing is ~5:00.

**Before you record**
- Start the app: `cd parcelpilot && cp .env.example .env` (add `GROQ_API_KEY`), then
  `uv run uvicorn app.main:app --port 8000` → open `http://localhost:8000`.
- Have ARCHITECTURE.md open in a second tab for the 45-second architecture beat.
- Zoom the browser to ~110% so the tool-trace chips and sidebar are legible on video.

---

## 0:00–0:30 · Hook + what this is (30s)

> "This is ParcelPilot — an AI support agent for a B2B logistics company. Its support team manually
> searches policies, customer contracts, product docs, past tickets, and live order data to answer
> every request. The hard part isn't search — it's that **the sources contradict each other**: a
> customer's contract can override policy, some docs are deprecated, and past ticket answers can be
> wrong. So the product's defining feature is knowing what it can answer confidently, what it must
> not, and when to hand off to a human. It runs in two contexts — customer-facing and internal ops —
> over one access-controlled tool layer."

*(On screen: the app's landing state, then flip briefly to the sidebar showing the role switcher.)*

## 0:30–1:15 · Architecture in 45 seconds (45s)

*(Show the ASCII data-flow diagram in ARCHITECTURE.md §1.)*

> "Architecture is deliberately simple. A FastAPI backend; identity is set server-side at login and
> passed as an `AuthContext` the model can never touch. The orchestrator runs the model's native
> tool-calling loop — no framework — over four tools: **document search** with authority tiers,
> **scoped SQL lookup**, a **deterministic policy calculator**, and **state-changing actions** that
> are confirmation-gated. Retrieval is BM25 over section-chunked PDFs — right-sized for six
> documents. Structured data is SQLite loaded from the workbook, so access scoping is just a WHERE
> clause. Two things are enforced in code, never in the prompt: **access control** and the
> **confirmation gate**. And every answer passes a **grounding self-check** before it reaches the
> user."

## 1:15–2:15 · Demo 1 — the canonical multi-step question (60s)

*(Role switcher → `customer_northstar`. Type the example question.)*

**Ask:** `Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.`

> "Watch the tool trace. It looks up the order — and this only returns because I'm scoped to
> Northstar's account. It searches the policy documents, finding both the general SOP, which charges
> a 250-rupee fee, and Northstar's own agreement, which waives it. Then it runs the deterministic
> calculator, which applies the contract over the policy."

*(Answer appears with the green "Verified against sources" badge.)*

> "The answer: no fee — and it cites *Northstar agreement, section 2*. Note what it did **not** do:
> there's a historical ticket in the data that recorded a 250-rupee fee for this exact case — the
> agent treats that as wrong, because a signed agreement outranks a past ticket. And the green badge
> means a second pass verified every claim traces back to the sources."

## 2:15–3:00 · Demo 2 — access control is structural (45s)

*(Same Northstar customer session. Ask about another account's data.)*

**Ask:** `What are the details of order ORD-2001?` *(ORD-2001 belongs to LumenWorks, not Northstar.)*

> "Now I'll ask this same Northstar customer about an order that belongs to a *different* company.
> It returns nothing — not because I told the model to be polite, but because the scope is a SQL
> predicate in the tool layer. A prompt-injection or a 'show me the other account' request can't talk
> its way past a WHERE clause. Privacy is structural, not a model instruction."

## 3:00–4:00 · Demo 3 — a state-changing action + confirmation (60s)

*(Role switcher → `staff_manager`. The staff view reveals the proactive feed + audit trail.)*

**Ask:** `Review ticket TKT-505 and escalate it as a P1 security incident.`

> "Switching to an internal staff view. I'll ask it to escalate a suspected security ticket. It
> looks up the ticket, checks the policy for the right severity — and then **stops**. It does not
> create the escalation. It shows a preview and waits."

*(Point at the confirmation card.)*

> "This confirmation gate lives in the orchestrator, so a state change can never fire without an
> explicit human click — even if the model 'forgot' to ask. I'll confirm it."

*(Click **Confirm & run**. Point to the sidebar Audit trail updating.)*

> "Action committed, with a reference — and it's now in the **audit trail** on the left: who did
> what, when. That's the record a regulated team needs to trust an autonomous tool."

## 4:00–4:40 · The two client problems (40s)

*(Point at the "Needs attention" feed in the staff sidebar.)*

> "That covers the core. For the two broader problems: **Problem 1, proactive detection** — this
> feed, on the left, runs deterministic rules over live data: security incidents, likely outages,
> breached SLAs, and — importantly — it **clusters recurring issues from the ticket text itself**,
> not a hardcoded list, and flags when one theme hits multiple customers at once. **Problem 2,
> trust** — that's the whole spine: authority tiers, deterministic math, access control in code, the
> confirmation gate, and the grounding check that **withholds** an answer and routes to a human when
> it can't verify it. A withheld answer beats a confident wrong one."

## 4:40–5:00 · Decisions + close (20s)

> "Two decisions I'd call out. One: money decisions are computed in **code**, not by the model — the
> failure that loses real money is a plausible wrong number, so I made that path deterministic and
> unit-tested against the exact traps in the data. Two: I chose **precision over recall** everywhere —
> the agent is allowed to say 'I'm not sure, here's what to verify.' The reliability traps are pinned
> by a golden test suite that runs in CI without an LLM. Thanks for watching."

*(End on the passing test output or the repo README.)*

---

## Optional B-roll / cutaways (if you have time to edit)

- `uv run --group dev pytest -q` → **62 passed** — over the "verified, not asserted" line.
- The service-credit example (`A pickup was 3 hours late due to carrier fault — do I get a service
  credit?`) as an alternate multi-step demo — it shows the agent *refusing to promise* a credit when
  fault/timing is unverified.
- ARCHITECTURE.md §5 authority-tier table on screen while narrating source precedence.

## Timing cheatsheet

| Beat | Budget | Running |
|---|---|---|
| Hook + framing | 0:30 | 0:30 |
| Architecture | 0:45 | 1:15 |
| Demo 1 — multi-step + grounding | 1:00 | 2:15 |
| Demo 2 — access control | 0:45 | 3:00 |
| Demo 3 — action + confirmation + audit | 1:00 | 4:00 |
| Two client problems | 0:40 | 4:40 |
| Decisions + close | 0:20 | 5:00 |

**Recording tips:** pre-type the four prompts into a scratch file and paste them — dead air while
typing kills pacing. If a Groq call is slow, cut the wait in edit. Keep the browser at 110% zoom so
the tool-trace chips read clearly. One clean take per demo beat is enough; the confirmation and audit
beats are the strongest — don't rush them.
