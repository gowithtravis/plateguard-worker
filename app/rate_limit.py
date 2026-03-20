"""
Simple in-memory rate limiting for public endpoints (per-process).

For production behind multiple workers, use Redis or an edge rate limiter.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import HTTPException, Request

# Max POST /api/onboard attempts per client IP per rolling hour
ONBOARD_MAX_PER_HOUR = 5
ONBOARD_WINDOW_SECONDS = 3600

_onboard_buckets: Dict[str, Deque[float]] = defaultdict(deque)


def get_client_ip(request: Request) -> str:
    """Prefer X-Forwarded-For (first hop) when behind a proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def enforce_onboard_rate_limit(request: Request) -> None:
    """Allow at most ONBOARD_MAX_PER_HOUR signups per IP per rolling window."""
    ip = get_client_ip(request)
    now = time.monotonic()
    q = _onboard_buckets[ip]
    while q and (now - q[0]) > ONBOARD_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= ONBOARD_MAX_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="Too many signup attempts from this address. Please try again in an hour.",
        )
    q.append(now)
