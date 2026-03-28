"""
Public waitlist onboarding from plateguard.io (no API key; rate limited + CORS).
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Optional

import structlog
from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from ..config import settings
from ..limiter import enforce_minute_ip_limit
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
    dob_mmdd: Optional[str] = Field(
        default=None,
        max_length=10,
        description="Optional MM/DD for Cambridge (eTIMS) plate lookup",
    )

    @field_validator("dob_mmdd", mode="before")
    @classmethod
    def _strip_dob_mmdd(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("dob_mmdd")
    @classmethod
    def _validate_dob_mmdd(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        import re

        m = re.match(r"^(\d{1,2})/(\d{1,2})$", v)
        if not m:
            raise ValueError("dob_mmdd must be MM/DD (e.g. 03/15)")
        mm, dd = int(m.group(1)), int(m.group(2))
        if mm < 1 or mm > 12 or dd < 1 or dd > 31:
            raise ValueError("dob_mmdd has invalid month or day")
        return f"{mm:02d}/{dd:02d}"


class OnboardResponse(BaseModel):
    message: str
    already_registered: bool = False


@router.post("/onboard", response_model=OnboardResponse)
async def onboard_public_waitlist(
    request: Request,
    payload: Annotated[OnboardRequest, Body(...)],
):
    """
    Waitlist signup from the public website. Requires email only; optional name and phone.

    **Not** protected by Bearer token — rate limited per IP (5/minute).
    """
    enforce_minute_ip_limit(
        request,
        scope="onboard",
        max_requests=5,
        window_seconds=60,
        detail=(
            "Too many waitlist requests from this address. Please wait a minute and try again."
        ),
    )

    if not settings.supabase_url or not settings.supabase_service_key:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY)",
        )

    try:
        svc = OnboardService()
    except OnboardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    fn = payload.first_name
    ln = payload.last_name
    phone = payload.phone

    try:
        result = await asyncio.to_thread(
            svc.process_public_waitlist_signup,
            str(payload.email),
            fn,
            ln,
            phone,
            payload.dob_mmdd,
        )
    except OnboardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        alerts = AlertService()
        await alerts.send_waitlist_welcome_email(
            str(payload.email),
            fn or "",
            ln or "",
            None,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("waitlist_welcome_email_failed", error=str(exc))

    if result.already_registered:
        return OnboardResponse(
            message="You're already on our waitlist — thanks for your interest!",
            already_registered=True,
        )

    return OnboardResponse(
        message="Thanks for joining the PlateGuard waitlist! Check your email to confirm.",
        already_registered=False,
    )
