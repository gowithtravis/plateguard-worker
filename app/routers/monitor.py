"""
Monitor router — handles plate checking endpoints.
"""
from typing import Optional

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, EmailStr

from ..config import settings
from ..services.alert_service import AlertService
from ..services.monitor_service import MonitorService


logger = structlog.get_logger()
router = APIRouter()


class CheckPlateRequest(BaseModel):
    plate_number: str
    state: str = "MA"
    portals: Optional[list[str]] = None  # None = all applicable


class RunBatchRequest(BaseModel):
    source: str = "manual"  # "pg_cron" or "manual"


class CheckResponse(BaseModel):
    plate_number: str
    state: str
    violations_found: int
    new_violations: int
    portals_checked: list[str]
    errors: list[str]


class BatchResponse(BaseModel):
    plates_checked: int
    total_violations: int
    new_violations: int
    errors: list[str]


class TestAlertRequest(BaseModel):
    """Body for POST /api/test-alert — sends a sample violation email via Resend."""

    email: EmailStr


class TestAlertResponse(BaseModel):
    sent: bool
    message: str


@router.post("/test-alert", response_model=TestAlertResponse)
async def test_alert(request: TestAlertRequest):
    """
    Send a sample new-violation email to the given address.
    Uses the same HTML template and Resend integration as production alerts.
    """
    if not settings.resend_api_key:
        raise HTTPException(
            status_code=503,
            detail="RESEND_API_KEY is not configured",
        )

    alerts = AlertService()
    ok = await alerts.send_sample_alert_email(str(request.email))
    if not ok:
        raise HTTPException(
            status_code=502,
            detail="Failed to send test email via Resend; check worker logs",
        )

    return TestAlertResponse(
        sent=True,
        message="Test alert sent successfully",
    )


@router.post("/check-plate", response_model=CheckResponse)
async def check_plate(request: CheckPlateRequest):
    """
    Check a single plate across specified portals.
    Checks RMC Pay cities (Boston, New Bedford, Lowell, Brookline) and Cambridge (eTIMS)
    by default when portals are omitted. Cambridge requires Browserbase, ``TWOCAPTCHA_API_KEY``,
    and profile ``dob_mmdd`` when the plate is tied to a user (batch checks).
    """
    service = MonitorService()
    result = await service.check_single_plate(
        plate_number=request.plate_number,
        state=request.state,
        portals=request.portals,
    )
    return result


@router.post("/run-batch", response_model=BatchResponse)
async def run_batch(request: RunBatchRequest, background_tasks: BackgroundTasks):
    """
    Batch check endpoint stub.
    Currently runs synchronously with placeholder aggregation.
    """
    logger.info("batch_check_triggered", source=request.source)

    # For pg_cron: acknowledge immediately, run in background
    if request.source == "pg_cron":
        background_tasks.add_task(_run_batch_background)
        return BatchResponse(
            plates_checked=0,
            total_violations=0,
            new_violations=0,
            errors=["Running in background — check logs for results"],
        )

    service = MonitorService()
    result = await service.check_all_active_plates()
    return result


async def _run_batch_background():
    """Background task for batch plate checking (placeholder)."""
    logger.info("batch_check_background_starting")
    service = MonitorService()
    result = await service.check_all_active_plates()
    logger.info(
        "batch_check_background_complete",
        plates_checked=result["plates_checked"],
        new_violations=result["new_violations"],
    )

