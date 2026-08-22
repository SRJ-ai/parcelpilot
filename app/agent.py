"""Agent orchestrator: native tool-calling loop + confirmation gate.

Read/compute tools auto-execute. When the model requests a state-changing tool that
the caller is allowed to run, the loop PAUSES and returns a confirmation preview; the
action executes only after the user confirms. The gate lives here, not in the prompt,
so the guarantee holds even if the model forgets to ask.
"""
import json
from app.config import SNAPSHOT_NOW
from app.tools import ToolBox, WRITE_TOOLS, TOOL_SPECS
from app import llm, obs

READ_TOOLS = {"search_documents", "lookup_data", "compute_policy_outcome"}
MAX_STEPS = 8
MAX_HISTORY = 40  # non-system messages kept; older turns are dropped to bound cost/context


def _trim(history):
    """Drop oldest turns when history grows past MAX_HISTORY, cutting only at a `user`
    boundary so a tool response is never orphaned from its tool_call."""
    if len(history) <= MAX_HISTORY + 1:
        return
    cut = len(history) - MAX_HISTORY
    while cut < len(history) and history[cut].get("role") != "user":
        cut += 1
    if 1 < cut < len(history):
        del history[1:cut]

SYSTEM_PROMPT = f"""You are ParcelPilot's AI support agent. The dataset snapshot (your "now") is {SNAPSHOT_NOW.isoformat()}. Use it for every time-based judgement.

SOURCE PRECEDENCE when sources conflict: (1) the customer's signed agreement, (2) current support policy / SOP, (3) current product documentation. Historical ticket resolutions are CONTEXT ONLY and may be wrong — never treat them as authority. Never use documents marked deprecated as current policy.

TOOLS: use search_documents for policy/agreement/product questions; lookup_data for account/order/ticket rows; compute_policy_outcome for any cancellation fee, service credit, or SLA breach (do NOT do this arithmetic yourself — the tool applies precedence and the snapshot time correctly).

BEHAVIOUR:
- Ground every answer in tool results. Cite the source by document name and section (e.g. "Northstar agreement §2", "SOP v4 §1"). Never paste raw tool output, result indices, or JSON (e.g. {"cursor":0}) into your answer.
- When you state a computed fee, credit, or SLA result, use the tool's own `reason` text — do not restate policy numbers or thresholds from memory (you may get the default wrong).
- Do not promise a service credit when carrier fault, pickup timing, or customer fault is unknown — say what must be verified.
- If a known issue (e.g. SwiftShip webhook delay KI-211) could explain a symptom, say so before concluding a failure.
- ESCALATE (via create_escalation) for: P1 / suspected security incidents, an already-breached SLA, credits above INR 1,000 (manager approval), conflicting or insufficient data, or any exception not supported by an agreement or current policy.
- State uncertainty and breaches openly; never hide them.
- To perform a state-changing action, CALL the tool directly — the system automatically asks the user to confirm before it runs. Do NOT ask for approval in prose first; the tool call itself triggers the confirmation step. Never assume approval.
Be concise and professional."""


def _tool_summary(name, args):
    if name == "lookup_data":
        return f"{name}({args.get('entity')}, {args.get('filters', {})})"
    if name == "compute_policy_outcome":
        return f"{name}({args.get('kind')}, {args.get('order_id') or args.get('ticket_id') or ''})"
    if name == "search_documents":
        return f"search_documents(\"{args.get('query','')[:50]}\")"
    return f"{name}({args})"


def new_history():
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def run(history, toolbox: ToolBox):
    """Drive the loop until a final answer or a confirmation pause.
    Returns (events, pending). pending is None, or a dict to resume with confirm()."""
    events = []
    _trim(history)
    for _ in range(MAX_STEPS):
        resp = llm.chat(history, TOOL_SPECS).choices[0].message
        msg = {"role": "assistant", "content": resp.content or ""}
        if resp.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in resp.tool_calls]
        history.append(msg)

        if not resp.tool_calls:
            events.append({"type": "final", "text": resp.content or ""})
            return events, None

        for tc in resp.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            events.append({"type": "tool_call", "name": name, "label": _tool_summary(name, args)})
            obs.event("tool_call", name=name, role=toolbox.auth.role)

            if name in READ_TOOLS:
                result = getattr(toolbox, name)(**args)
                history.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
                events.append({"type": "tool_result", "name": name})
            elif name in WRITE_TOOLS:
                preview = toolbox.preview_write(name, args)
                if not preview.get("allowed"):
                    history.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(preview)})
                    events.append({"type": "tool_result", "name": name, "note": preview.get("message")})
                else:
                    # PAUSE: everything before this tool_call has a response; this one waits for confirm.
                    events.append({"type": "confirm_required", "action": name, "args": args,
                                   "preview": preview["preview"]})
                    return events, {"tool_call_id": tc.id, "name": name, "args": args}
            else:
                history.append({"role": "tool", "tool_call_id": tc.id,
                                "content": json.dumps({"error": f"unknown tool {name}"})})
    events.append({"type": "final", "text": "Stopping: reached the step limit. Escalating to a human agent may be appropriate."})
    return events, None


def confirm(history, toolbox: ToolBox, pending: dict, approved: bool):
    """Resume after the user's confirm/cancel decision."""
    if approved:
        result = toolbox.commit_write(pending["name"], pending["args"])
        obs.event("action_committed", action=pending["name"], ref=result.get("ref"), role=toolbox.auth.role)
    else:
        result = {"cancelled": True, "message": "User declined the action; do not perform it."}
        obs.event("action_cancelled", action=pending["name"])
    history.append({"role": "tool", "tool_call_id": pending["tool_call_id"], "content": json.dumps(result)})
    events = []
    if approved:
        events.append({"type": "action_committed", "ref": result.get("ref"), "action": pending["name"]})
    else:
        events.append({"type": "action_cancelled", "action": pending["name"]})
    more, new_pending = run(history, toolbox)
    return events + more, new_pending
