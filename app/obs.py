"""Minimal structured logging: one logger, a per-request id via contextvar, and a
key=value event helper. Enough to trace a request through the agent in production
without pulling in a logging framework.
"""
import logging
import os
import uuid
import contextvars

_request_id = contextvars.ContextVar("request_id", default="-")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
_log = logging.getLogger("parcelpilot")

# Quiet noisy third-party per-request logs (httpx logs every provider URL at INFO).
for _noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def new_request_id() -> str:
    rid = uuid.uuid4().hex[:8]
    _request_id.set(rid)
    return rid


def event(msg: str, level=logging.INFO, **fields):
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    _log.log(level, "rid=%s %s %s", _request_id.get(), msg, kv)


def error(msg: str, **fields):
    event(msg, level=logging.ERROR, **fields)
