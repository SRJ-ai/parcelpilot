# ParcelPilot AI Support — Product Note

## Product context

ParcelPilot's 20-person customer-ops team manually searches policies, agreements, product docs, past
tickets, and operational data to resolve customer requests. This system is an AI support agent that
does that reasoning reliably, in two contexts (customer-facing and internal ops), over a deliberately
imperfect source base where agreements override policies and some past answers are wrong.

*Design world (assumption, no interview run):* an **Operate**-mode support console — dense, legible,
calm, brand carried in details, not decoration. Recorded here rather than blocking the build on a
full product interview; revisit with `/impeccable init` if this becomes a real product.

## Additional client problem addressed

**Primary: Problem 2 — Trust & Reliability.** This is the adoption risk: one confidently wrong answer
or action erodes the team's trust. It is addressed structurally, not with a disclaimer:

- **Authority tiers assigned at ingestion** — agreement (4) > current policy/SOP (3) > product doc (2)
  > historical ticket (1, context-only) > deprecated (0, excluded). Precedence is data, not vibes.
- **Deterministic calculators** compute every fee, credit, and SLA decision in code, applying the
  resolved terms and the dataset snapshot time — so the number that drives an action is correct and
  reproducible, and the model only orchestrates and explains.
- **Uncertainty is surfaced, never hidden** — no credit promised while fault/timing is unknown; a
  stale BOOKED status is checked against known issue KI-211 before concluding a pickup failed;
  breached SLAs are stated plainly and escalated.
- **Confirmation gate in the orchestrator** — state-changing actions cannot fire without explicit
  user confirmation, independent of whether the model "remembered" to ask.
- **Answer-grounding self-check** — before a substantive answer reaches the user, a strict second
  pass (`app/verify.py`) judges whether every factual/policy/numeric claim is supported by the tool
  evidence gathered that turn. Grounded answers get a "Verified against sources" badge; ungrounded
  or over-reaching ones get a visible caution that steers toward human escalation. It fails open
  (a verifier hiccup never blocks an answer) and is toggleable via `GROUNDING_CHECK`.
- **Access control in the tool layer** — a prompt-injection or a customer asking for another
  account's data returns nothing; it is not a model instruction that can be talked around.

The golden test suite pins the exact traps in the data pack (ORD-1001 fee waiver, ORD-2002 contract
credit, wrong historical resolutions in TKT-450/451, deprecated v2 ignored, cross-account isolation),
so reliability is verified, not asserted.

**Bonus: Problem 1 — Proactive Issue Detection.** Internal staff get a "needs attention" feed:
deterministic rules over live data surface suspected security incidents, likely P1 outages, breached
SLAs, recurring known-issue clusters (e.g. repeated bulk-upload tickets → KI-208), and outstanding
carrier-fault pickups that likely owe a credit — ranked by urgency, with heuristic severity clearly
flagged for human confirmation.

## What I would build next (prioritised)

1. **Retrieval eval harness (building on the shipped grounding check).** The answer-grounding
   self-check (§ above) already flags per-answer ungrounded claims at runtime. The next step is an
   offline, labelled question set with expected sources/answers, run in CI, to catch silent
   regressions across the whole corpus as policies change — turning per-turn grounding into a
   measurable, tracked quality gate.
2. **Real auth + audit log.** Replace the mock role switcher with SSO/session auth, persist actions
   to a durable store, and log every tool call, source cited, and confirmation — for compliance and
   for debugging "why did it say that".
3. **Change-aware document ingestion.** Watch the doc source; when a policy version supersedes
   another, auto-deprecate the old one and flag answers that relied on it. Contract terms structured
   on upload rather than hand-encoded.
4. **Streaming + richer tool trace UI.** Token streaming and an expandable "show sources / show
   computation" panel per answer, so staff can audit the reasoning inline.
5. **Feedback loop.** Thumbs + correction capture on answers, feeding the eval set and surfacing
   which policies/queries are hardest — closing the loop between the two problems.

## What I intentionally left out

- **No vector database** — six short documents; BM25 over section chunks with authority metadata is
  right-sized. Vector search is the documented scale path, not a day-one need.
- **No agent framework** — the native tool loop keeps the confirmation gate and access scoping
  explicit; a framework would add dependency and indirection without benefit here.
- **Business-hours SLA math is approximate** — 24x7 targets are asserted exactly; business-hour /
  weekend-sensitive targets return "verify calendar" rather than a false breach. A real business
  calendar is straightforward but out of scope for the assessment.
- **Mock, in-memory action store** — escalations/tickets/tasks are not persisted; this is the seam
  where a real ticketing integration (Zendesk/Jira) would plug in.
- **No token streaming yet** — responses render the full tool trace then the answer; adequate for the
  demo, listed above as the next UI step.

## One metric

**Autonomous resolution rate with zero trust violations** — the share of customer requests the agent
resolves end-to-end (answer or correctly-gated action) *without* a wrong answer, a leaked record, or
an unconfirmed state change. It captures both halves of the job: usefulness (deflection) and the
trust bar that makes deflection safe. A high deflection rate means nothing if it comes with violations,
so the two are measured together, not traded off.
