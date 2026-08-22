"""Auto-abstain -> escalate when the grounding guard fails. LLM + verifier mocked."""
import pytest
from app import agent
from app.ingest import load_sqlite
from app.auth import AuthContext
from app.tools import ToolBox


class _Msg:
    def __init__(self): self.content = "Your fees are all waived."; self.tool_calls = None


class _Resp:
    def __init__(self): self.choices = [type("C", (), {"message": _Msg()})()]


@pytest.fixture
def tb():
    return ToolBox(load_sqlite(), None, AuthContext("staff", staff_role="agent"))


def _history():
    h = agent.new_history()
    h.append({"role": "user", "content": "Are all my fees waived?"})
    return h


def test_low_grounding_abstains_and_escalates(monkeypatch, tb):
    monkeypatch.setattr(agent.llm, "chat", lambda messages, tools: _Resp())
    monkeypatch.setattr(agent.verify, "check",
                        lambda ev, txt: {"grounded": 0.3, "supported": False, "escalate": True, "note": "unsupported", "ok": False})
    events, pending = agent.run(_history(), tb)
    final = [e for e in events if e["type"] == "final"][0]
    assert final["abstained"] is True
    assert final["escalation_ref"] and final["escalation_ref"].startswith("ACT-")
    assert final["withheld"] == "Your fees are all waived."          # draft kept for audit, not shown
    assert "flagged it for a human" in final["text"]
    # the auto-escalation is recorded in the action store
    n = tb.con.execute("SELECT COUNT(*) FROM actions WHERE kind='create_escalation'").fetchone()[0]
    assert n == 1


def test_grounded_answer_is_shown(monkeypatch, tb):
    monkeypatch.setattr(agent.llm, "chat", lambda messages, tools: _Resp())
    monkeypatch.setattr(agent.verify, "check",
                        lambda ev, txt: {"grounded": 0.98, "supported": True, "escalate": False, "note": "", "ok": True})
    events, _ = agent.run(_history(), tb)
    final = [e for e in events if e["type"] == "final"][0]
    assert final.get("abstained") is not True and final["text"] == "Your fees are all waived."


def test_no_verifier_verdict_shows_answer(monkeypatch, tb):
    monkeypatch.setattr(agent.llm, "chat", lambda messages, tools: _Resp())
    monkeypatch.setattr(agent.verify, "check", lambda ev, txt: None)  # disabled / no evidence
    events, _ = agent.run(_history(), tb)
    final = [e for e in events if e["type"] == "final"][0]
    assert "abstained" not in final and final["text"] == "Your fees are all waived."
