"""
Public signup helpers for app.plateguard.io (CORS + rate limit; no Bearer).

When ``signUp`` reports the email already exists (e.g. waitlist user), the client should
call ``POST /api/signup/set-password`` with the same email and chosen password, then
``supabase.auth.signInWithPassword`` and redirect to ``/dashboard``.
"""
from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from ..config import settings
from ..rate_limit import enforce_onboard_rate_limit
from ..services.onboard_service import OnboardError, OnboardService


logger = structlog.get_logger()
router = APIRouter()


class SetPasswordRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)


class SetPasswordResponse(BaseModel):
    ok: bool = True
    message: str = "Password saved. Sign in with your email and password, then continue to your dashboard."


@router.post("/set-password", response_model=SetPasswordResponse)
async def set_password_for_existing_account(request: Request, body: SetPasswordRequest):
    """
    Set the Supabase Auth password for an **existing** user (same email as waitlist).

    The app signup page should call this when ``signUp`` returns a duplicate-email error:
    then run ``signInWithPassword`` on the client and redirect to ``/dashboard``.

    (Client-only ``supabase.auth.updateUser({ password })`` requires an active session;
    this endpoint uses the service role so no session is needed first.)
    """
    enforce_onboard_rate_limit(request)

    if not settings.supabase_url or not settings.supabase_service_key:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY)",
        )

    try:
        svc = OnboardService()
    except OnboardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        await asyncio.to_thread(
            svc.set_password_for_existing_user,
            str(body.email),
            body.password,
        )
    except OnboardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return SetPasswordResponse()
