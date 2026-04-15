"""
SlowAPI rate limiting (shared limiter + key functions).

Uses ``X-Forwarded-For`` (leftmost client), then ``CF-Connecting-IP`` / ``True-Client-IP`` / ``X-Real-IP`` when present, so limits track end users behind reverse proxies.
JWT routes: buckets by unverified ``sub`` from Bearer token when it looks like a JWT;
otherwise falls back to client IP (e.g. worker API key).

Public JSON-body routes (e.g. ``/api/onboard``) cannot use ``@limiter.limit`` on the
handler: SlowAPI's wrapper exposes ``*args, **kwargs`` and FastAPI then fails to bind the
Pydantic body (422). Those routes use :func:`enforce_minute_ip_limit` instead.
"""
from __future__ import annotations

import base64
import json
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def get_forwarded_ip(request: Request) -> str:
    """
    Client IP behind reverse proxies.

    Prefer the leftmost non-empty address in ``X-Forwarded-For`` (original client per
    common proxy chains). If that header is absent, fall back to ``CF-Connecting-IP``,
    ``True-Client-IP``, or ``X-Real-IP`` (many CDNs / nginx set one of these). Only if
    none are present do we use the TCP peer (often the proxy), which would bucket all
    users together.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        for part in xff.split(","):
            candidate = part.strip()
            if candidate:
                return candidate

    for header in ("cf-connecting-ip", "true-client-ip", "x-real-ip"):
        raw = request.headers.get(header)
        if not raw:
            continue
        candidate = raw.strip().split(",")[0].strip()
        if candidate:
            return candidate

    return get_remote_address(request)


def _jwt_sub_unverified(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        pad = "=" * (-len(parts[1]) % 4)
        payload_bytes = base64.urlsafe_b64decode(parts[1] + pad)
        payload = json.loads(payload_bytes.decode("utf-8"))
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


def get_authed_rate_limit_key(request: Request) -> str:
    """
    Prefer Supabase user id from JWT ``sub`` (payload decoded without verification);
    otherwise bucket by IP (worker API key and other non-JWT Bearer tokens).
    """
    sub = _jwt_sub_unverified(request)
    if sub:
        return f"user:{sub}"
    return f"ip:{get_forwarded_ip(request)}"


limiter = Limiter(key_func=get_forwarded_ip)

# In-memory rolling window for routes that cannot use SlowAPI decorators (see module doc).
_minute_ip_buckets: dict[str, deque[float]] = defaultdict(deque)


def enforce_minute_ip_limit(
    request: Request,
    *,
    scope: str,
    max_requests: int,
    window_seconds: int = 60,
    detail: str,
) -> None:
    """
    Enforce ``max_requests`` per ``window_seconds`` per client IP (see :func:`get_forwarded_ip`).

    Raises ``HTTPException(429)`` with ``detail`` when exceeded.
    """
    ip = get_forwarded_ip(request)
    key = f"{scope}:{ip}"
    now = time.monotonic()
    q = _minute_ip_buckets[key]
    while q and (now - q[0]) > window_seconds:
        q.popleft()
    if len(q) >= max_requests:
        raise HTTPException(status_code=429, detail=detail)
    q.append(now)
