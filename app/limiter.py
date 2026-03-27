"""
SlowAPI rate limiting (shared limiter + key functions).

Uses ``X-Forwarded-For`` first hop when present (Railway / reverse proxy).
JWT routes: buckets by unverified ``sub`` from Bearer token when it looks like a JWT;
otherwise falls back to client IP (e.g. worker API key).
"""
from __future__ import annotations

import base64
import json
from typing import Optional

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def get_forwarded_ip(request: Request) -> str:
    """Client IP: first address in ``X-Forwarded-For``, else socket client."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or get_remote_address(request)
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
