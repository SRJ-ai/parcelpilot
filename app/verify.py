"""Grounding self-verification. Before a substantive answer reaches the user, a strict
second pass checks that every factual / policy / numeric claim is supported by the tool
evidence gathered this turn. For a financial support agent, a confidently wrong answer
is worse than a slow one — so when the answer isn't grounded, we flag it and steer
toward human escalation rather than showing it as fact.

Toggle with GROUNDING_CHECK=0. Fails open (never blocks an answer on verifier error).
"""
import json
import os
from app import llm, obs

ENABLED = os.getenv("GROUNDING_CHECK", "1") != "0"
THRESHOLD = float(os.getenv("GROUNDING_THRESHOLD", "0.6"))

_JUDGE = """You are a strict grounding verifier for a financial logistics support assistant.
You are given SOURCES (tool results the assistant retrieved) and the assistant's ANSWER.
Judge only whether the ANSWER's factual, policy, and numeric claims are supported by the SOURCES.
Do not judge writing quality. A general clarifying reply with no factual claims is fully grounded.

Return ONLY a JSON object:
{"grounded": 0.0-1.0, "supported": true/false, "escalate": true/false, "note": "short caution or empty"}
- grounded: fraction of the answer's claims backed by the sources.
- supported: true if no claim contradicts or exceeds the sources.
- escalate: true if the answer commits to a number/action not present in the sources, or asserts something needing human judgement.
- note: one short user-facing caution if not fully grounded, else empty."""


def _parse(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        raise ValueError("no json object")
    return json.loads(text[a:b + 1])


def check(evidence: list[str], answer: str) -> dict | None:
    """Return {grounded, supported, escalate, note} or None if disabled / no evidence /
    verifier unavailable (fail-open)."""
    if not ENABLED or not evidence or not answer.strip():
        return None
    sources = "\n\n".join(evidence)[:6000]
    try:
        resp = llm.complete([
            {"role": "system", "content": _JUDGE},
            {"role": "user", "content": f"SOURCES:\n{sources}\n\nANSWER:\n{answer}"},
        ])
        data = _parse(resp.choices[0].message.content or "")
        out = {
            "grounded": max(0.0, min(1.0, float(data.get("grounded", 1.0)))),
            "supported": bool(data.get("supported", True)),
            "escalate": bool(data.get("escalate", False)),
            "note": str(data.get("note", ""))[:300],
        }
        out["ok"] = out["supported"] and out["grounded"] >= THRESHOLD and not out["escalate"]
        obs.event("grounding", grounded=round(out["grounded"], 2),
                  supported=out["supported"], escalate=out["escalate"])
        return out
    except Exception as e:
        obs.error("grounding_failed", err=f"{type(e).__name__}: {e}")
        return None  # fail open: never block an answer because the verifier hiccuped
