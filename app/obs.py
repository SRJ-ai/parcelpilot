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


def current_id() -> str:
    return _request_id.get()


import threading
import time as _time

_METRICS = {}
_LAT = []  # recent request latencies (ms), capped
_START = _time.time()
_mlock = threading.Lock()


def incr(name: str, n: int = 1):
    with _mlock:
        _METRICS[name] = _METRICS.get(name, 0) + n


def observe_latency(ms: float):
    with _mlock:
        _LAT.append(ms)
        if len(_LAT) > 500:
            del _LAT[: len(_LAT) - 500]


def metrics_snapshot() -> dict:
    with _mlock:
        lat = sorted(_LAT)
        p50 = lat[len(lat) // 2] if lat else 0
        p95 = lat[int(len(lat) * 0.95)] if lat else 0
        return {
            "uptime_seconds": round(_time.time() - _START),
            "counters": dict(_METRICS),
            "latency_ms": {"count": len(lat), "p50": round(p50), "p95": round(p95)},
        }


def event(msg: str, level=logging.INFO, **fields):
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    _log.log(level, "rid=%s %s %s", _request_id.get(), msg, kv)
    incr(f"event.{msg}")


def error(msg: str, **fields):
    event(msg, level=logging.ERROR, **fields)
