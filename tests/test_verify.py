"""Grounding verifier: parsing, verdicts, and fail-open behaviour (LLM mocked)."""
import pytest
from app import verify


class _Msg:
    def __init__(self, c): self.content = c


class _Choice:
    def __init__(self, c): self.message = _Msg(c)


class _Resp:
    def __init__(self, c): self.choices = [_Choice(c)]


def _mock(content):
    return lambda messages, **kw: _Resp(content)


def test_grounded_answer_ok(monkeypatch):
    monkeypatch.setattr(verify, "ENABLED", True)
    monkeypatch.setattr(verify.llm, "complete", _mock('{"grounded":0.95,"supported":true,"escalate":false,"note":""}'))
    r = verify.check(["source text"], "the answer")
    assert r["ok"] is True and r["escalate"] is False


def test_ungrounded_answer_flagged(monkeypatch):
    monkeypatch.setattr(verify, "ENABLED", True)
    monkeypatch.setattr(verify.llm, "complete",
                        _mock('prefix {"grounded":0.2,"supported":false,"escalate":true,"note":"unsupported claim"} suffix'))
    r = verify.check(["source"], "answer")
    assert r["ok"] is False and r["escalate"] is True and r["note"] == "unsupported claim"


def test_fail_open_on_verifier_error(monkeypatch):
    monkeypatch.setattr(verify, "ENABLED", True)
    def boom(messages, **kw): raise RuntimeError("provider down")
    monkeypatch.setattr(verify.llm, "complete", boom)
    assert verify.check(["source"], "answer") is None  # never blocks the answer


def test_skipped_when_no_evidence(monkeypatch):
    monkeypatch.setattr(verify, "ENABLED", True)
    assert verify.check([], "answer") is None


def test_disabled(monkeypatch):
    monkeypatch.setattr(verify, "ENABLED", False)
    assert verify.check(["source"], "answer") is None
