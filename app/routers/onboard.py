"""
GHL waitlist onboarding — Supabase Auth + profile + optional plate + welcome email.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..config import settings
from ..services.alert_service import AlertService
from ..services.onboard_service import OnboardError, OnboardService


logger = structlog.get_logger()
router = APIRouter()


class OnboardRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    first_name: str = Field(..., min_length=1)
    last_name: str = Field(default="", max_length=200)
    email: EmailStr
    phone: Optional[str] = None
    plate_number: Optional[str] = None


class OnboardResponse(BaseModel):
    user_id: str


@router.post("/onboard", response_model=OnboardResponse)
async def onboard_waitlist(request: OnboardRequest):
    """
    Create (or reconcile) a Supabase Auth user, upsert `profiles`, optionally add a plate,
    and send a branded welcome email via Resend.

    Duplicate signups (same email): updates profile and adds the plate only if it is new.
    """
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
        user_id = await asyncio.to_thread(
            svc.process_waitlist_signup,
            request.first_name,
            request.last_name,
            str(request.email),
            request.phone,
            request.plate_number,
        )
    except OnboardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        alerts = AlertService()
        await alerts.send_waitlist_welcome_email(
            str(request.email),
            request.first_name,
            request.last_name,
            request.plate_number,
        )
    except Exception as exc:  # pragma: no cover - email must not fail onboarding
        logger.warning("waitlist_welcome_email_failed", error=str(exc))

    return OnboardResponse(user_id=user_id)
