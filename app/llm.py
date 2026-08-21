"""LLM adapter. Groq and Ollama are both OpenAI-compatible, so one client with a
swapped base_url covers both backends. Groq (hosted) is the default; Ollama is the
local/offline fallback.

Multiple Groq keys are supported for failover: set GROQ_API_KEY (optionally
comma-separated) plus GROQ_API_KEY_2 / GROQ_API_KEY_3 / ... Requests round-robin
across keys and fail over to the next key on a rate limit or transient error, so one
exhausted key does not take the app down.
"""
import os
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIStatusError, APIConnectionError

load_dotenv()

GROQ_BASE = "https://api.groq.com/openai/v1"
_groq_clients = None
_rr = 0


def _groq_keys() -> list[str]:
    keys: list[str] = []
    for part in (os.getenv("GROQ_API_KEY") or "").split(","):
        if part.strip():
            keys.append(part.strip())
    for i in range(2, 8):  # GROQ_API_KEY_2 .. _7
        v = os.getenv(f"GROQ_API_KEY_{i}")
        if v and v.strip():
            keys.append(v.strip())
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out or ["missing-GROQ_API_KEY"]


def _clients():
    global _groq_clients
    if _groq_clients is None:
        _groq_clients = [OpenAI(base_url=GROQ_BASE, api_key=k) for k in _groq_keys()]
    return _groq_clients


def chat(messages, tools):
    kwargs = dict(messages=messages, tools=tools, tool_choice="auto", temperature=0.2)

    if os.getenv("LLM_PROVIDER", "groq").lower() == "ollama":
        c = OpenAI(base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"), api_key="ollama")
        return c.chat.completions.create(model=os.getenv("OLLAMA_MODEL", "qwen2.5"), **kwargs)

    global _rr
    clients = _clients()
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    n = len(clients)
    last = None
    for off in range(n):
        c = clients[(_rr + off) % n]
        try:
            resp = c.chat.completions.create(model=model, **kwargs)
            _rr = (_rr + off + 1) % n  # advance start so load spreads across keys
            return resp
        except RateLimitError as e:
            last = e  # this key is throttled — try the next
        except (APIConnectionError,) as e:
            last = e
        except APIStatusError as e:
            if e.status_code in (429, 500, 502, 503):
                last = e
            else:
                raise
    raise last
