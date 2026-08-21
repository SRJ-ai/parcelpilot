"""LLM adapter. Groq and Ollama are both OpenAI-compatible, so one client with a
swapped base_url covers both backends. Groq (hosted) is the default; Ollama is the
local/offline fallback. Client + env are read lazily on first call so `.env` is
loaded regardless of import order.
"""
import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
_client = None
_model = None


def _ensure():
    global _client, _model
    if _client is not None:
        return
    provider = os.getenv("LLM_PROVIDER", "groq").lower()
    if provider == "ollama":
        _client = OpenAI(base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                         api_key="ollama")
        _model = os.getenv("OLLAMA_MODEL", "qwen2.5")
    else:  # groq
        _client = OpenAI(base_url="https://api.groq.com/openai/v1",
                         api_key=os.getenv("GROQ_API_KEY") or "missing-GROQ_API_KEY")
        _model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def chat(messages, tools):
    _ensure()
    return _client.chat.completions.create(
        model=_model, messages=messages, tools=tools,
        tool_choice="auto", temperature=0.2,
    )
