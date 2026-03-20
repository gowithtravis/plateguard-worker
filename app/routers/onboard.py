"""
Public waitlist onboarding from plateguard.io (no API key; rate limited + CORS).
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..config import settings
from ..rate_limit import enforce_onboard_rate_limit
from ..services.alert_service import AlertService
from ..services.onboard_service import OnboardError, OnboardService


logger = structlog.get_logger()
router = APIRouter()


class OnboardRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: EmailStr
    first_name: Optional[str] = Field(default=None, max_length=200)
    last_name: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=50)


class OnboardResponse(BaseModel):
    message: str
    already_registered: bool = False


@router.post("/onboard", response_model=OnboardResponse)
async def onboard_public_waitlist(request: Request, body: OnboardRequest):
    """
    Waitlist signup from the public website. Requires email only; optional name and phone.

    **Not** protected by Bearer token — uses per-IP rate limiting (5/hour) instead.
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

    fn = body.first_name
    ln = body.last_name
    phone = body.phone

    try:
        result = await asyncio.to_thread(
            svc.process_public_waitlist_signup,
            str(body.email),
            fn,
            ln,
            phone,
        )
    except OnboardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    if result.already_registered:
        return OnboardResponse(
            message="You're already on our waitlist — thanks for your interest!",
            already_registered=True,
        )

    try:
        alerts = AlertService()
        await alerts.send_waitlist_welcome_email(
            str(body.email),
            fn or "",
            ln or "",
            None,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("waitlist_welcome_email_failed", error=str(exc))

    return OnboardResponse(
        message="Thanks for joining the PlateGuard waitlist! Check your email to confirm.",
        already_registered=False,
    )
