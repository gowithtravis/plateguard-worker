"""
Monitor router — handles plate checking endpoints.
"""
from typing import Optional

import structlog
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

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


@router.post("/check-plate", response_model=CheckResponse)
async def check_plate(request: CheckPlateRequest):
    """
    Check a single plate across specified portals.
    For now, only Boston parking is fully wired up.
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

