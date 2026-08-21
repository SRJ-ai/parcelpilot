"""Groq multi-key parsing + failover rotation."""
import importlib
import app.llm as llm


def test_key_parsing_numbered_and_comma(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k1, k2")   # comma-separated
    monkeypatch.setenv("GROQ_API_KEY_2", "k3")
    monkeypatch.setenv("GROQ_API_KEY_3", "k1")     # duplicate is dropped
    keys = llm._groq_keys()
    assert keys == ["k1", "k2", "k3"]


def test_missing_key_placeholder(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    for i in range(2, 8):
        monkeypatch.delenv(f"GROQ_API_KEY_{i}", raising=False)
    assert llm._groq_keys() == ["missing-GROQ_API_KEY"]


def test_failover_to_next_key_on_rate_limit(monkeypatch):
    from openai import RateLimitError

    class FakeResp:
        pass

    class Throttled:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise RateLimitError("rate", response=_R(429), body=None)

    class Working:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return FakeResp()

    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setattr(llm, "_groq_clients", [Throttled(), Working()])
    llm._rr = 0
    out = llm.chat([{"role": "user", "content": "hi"}], [])
    assert isinstance(out, FakeResp)  # rotated past the throttled key


class _R:
    """Minimal stand-in for an httpx.Response so RateLimitError constructs."""
    def __init__(self, status):
        self.status_code = status
        self.request = None
        self.headers = {}
