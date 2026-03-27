"""
Verify Supabase Auth access tokens (Bearer JWT) from the consumer dashboard.
"""
from __future__ import annotations

import asyncio

import structlog
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .supabase_client import supabase_client

logger = structlog.get_logger()
security = HTTPBearer()


def _user_id_from_jwt_sync(token: str) -> str:
    if not supabase_client:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured",
        )
    try:
        resp = supabase_client.auth.get_user(token)
    except Exception as exc:
        logger.warning("supabase_jwt_verify_failed", error=str(exc))
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
        ) from exc

    user = getattr(resp, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    uid = getattr(user, "id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return str(uid)


async def verify_supabase_jwt(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """Return the authenticated user's id (JWT `sub`)."""
    return await asyncio.to_thread(_user_id_from_jwt_sync, credentials.credentials)
