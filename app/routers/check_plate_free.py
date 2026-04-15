"""
Public anonymous plate check (RMC Pay cities only) — no auth, no persistence.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Annotated, Optional

import structlog
from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from ..config import settings
from ..constants.us_states import US_STATE_CODES
from ..limiter import enforce_minute_ip_limit, get_forwarded_ip
from ..portals.rmc_parking import default_rmc_portal_labels
from ..services.alert_service import AlertService
from ..services.free_plate_check import check_plate_free_rmc_sync
from ..services.onboard_service import OnboardError, OnboardService


logger = structlog.get_logger()
router = APIRouter()

_PLATE_NORMALIZED = re.compile(r"^[A-Za-z0-9]{2,8}$")


def _normalize_plate_for_free_check(raw: str) -> str:
    s = raw.replace(" ", "").replace("-", "")
    if not _PLATE_NORMALIZED.fullmatch(s):
        raise ValueError("invalid_plate")
    return s.upper()


def _validate_state_code(state: Optional[str]) -> str:
    s = (state or "MA").strip().upper()
    if s not in US_STATE_CODES:
        raise ValueError("invalid_state")
    return s


def _rmc_city_labels_for_response() -> list[str]:
    return [p.replace(" (RMC Pay)", "").strip() or p for p in default_rmc_portal_labels()]


class CheckPlateFreeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    plate_number: str = Field(default="", max_length=64)
    state: str = Field(default="MA", max_length=8)
    # Plain str so bots can send junk + ``website`` honeypot without failing before the handler.
    email: Optional[str] = Field(default=None, max_length=320)
    website: Optional[str] = Field(default=None, max_length=512)

    @field_validator("email", mode="before")
    @classmethod
    def _empty_email_to_none(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v


_email_adapter = TypeAdapter(EmailStr)


class FreeViolationItem(BaseModel):
    city: str
    amount: Optional[float] = None
    date: Optional[str] = None
    status: str = "open"


class CheckPlateFreeResponse(BaseModel):
    plate_number: str
    state: str
    violations_found: int
    violations: list[FreeViolationItem]
    portals_checked: list[str]
    checked_at: str
    waitlist_enrolled: Optional[bool] = None
    waitlist_message: Optional[str] = None


def _honeypot_response() -> CheckPlateFreeResponse:
    return CheckPlateFreeResponse(
        plate_number="***",
        state="MA",
        violations_found=0,
        violations=[],
        portals_checked=_rmc_city_labels_for_response(),
        checked_at=datetime.now(timezone.utc).isoformat(),
        waitlist_enrolled=None,
        waitlist_message=None,
    )


@router.post("/check-plate-free", response_model=CheckPlateFreeResponse)
async def check_plate_free(
    request: Request,
    payload: Annotated[CheckPlateFreeRequest, Body(...)],
):
    """
    Check a plate across all RMC Pay MA cities (no Cambridge, no EZDriveMA).
    Anonymous — results are not stored. Optional ``email`` enrolls the waitlist (same as ``/api/onboard``).
    """
    if payload.website is not None and str(payload.website).strip():
        return _honeypot_response()

    try:
        plate = _normalize_plate_for_free_check(payload.plate_number or "")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid plate_number.") from None
    try:
        state = _validate_state_code(payload.state)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid state.") from None

    enforce_minute_ip_limit(
        request,
        scope="free_plate_check",
        max_requests=3,
        window_seconds=3600,
        detail=(
            "Too many free plate checks from this address. You can try again in up to an hour "
            "(limit: 3 checks per hour per IP)."
        ),
    )

    prefix = plate[:3] if len(plate) >= 3 else plate
    logger.info(
        "check_plate_free_request",
        client_ip=get_forwarded_ip(request),
        plate_prefix=prefix,
        ts=datetime.now(timezone.utc).isoformat(),
    )

    violations_raw, portals_checked = await asyncio.to_thread(
        check_plate_free_rmc_sync,
        plate,
        state,
    )

    violations = [FreeViolationItem(**v) for v in violations_raw]
    checked_at = datetime.now(timezone.utc).isoformat()

    waitlist_enrolled: Optional[bool] = None
    waitlist_message: Optional[str] = None

    if payload.email is not None and str(payload.email).strip():
        try:
            normalized_email = str(
                _email_adapter.validate_python(str(payload.email).strip().lower())
            )
        except ValidationError:
            raise HTTPException(status_code=422, detail="Invalid email address.") from None
        if not settings.supabase_url or not settings.supabase_service_key:
            waitlist_enrolled = False
            waitlist_message = "Waitlist signup skipped: Supabase is not configured."
        else:
            try:
                svc = OnboardService()
            except OnboardError as exc:
                waitlist_enrolled = False
                waitlist_message = exc.message
            else:
                try:
                    result = await asyncio.to_thread(
                        svc.process_public_waitlist_signup,
                        normalized_email,
                        None,
                        None,
                        None,
                        None,
                    )
                    try:
                        alerts = AlertService()
                        await alerts.send_waitlist_welcome_email(
                            normalized_email,
                            "",
                            "",
                            None,
                        )
                    except Exception as exc:  # pragma: no cover
                        logger.warning(
                            "free_check_waitlist_welcome_email_failed",
                            error=str(exc),
                        )
                    waitlist_enrolled = True
                    waitlist_message = (
                        "You're already on our waitlist - thanks for your interest!"
                        if result.already_registered
                        else "Thanks for joining the PlateGuard waitlist! Check your email to confirm."
                    )
                except OnboardError as exc:
                    waitlist_enrolled = False
                    waitlist_message = exc.message

    return CheckPlateFreeResponse(
        plate_number=plate,
        state=state,
        violations_found=len(violations),
        violations=violations,
        portals_checked=portals_checked,
        checked_at=checked_at,
        waitlist_enrolled=waitlist_enrolled,
        waitlist_message=waitlist_message,
    )
