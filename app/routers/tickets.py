"""
Manual ticket reporting — validate ticket on external portals and store in Supabase.
"""
from __future__ import annotations

from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps.supabase_jwt import verify_supabase_jwt
from ..services.monitor_service import MonitorService


logger = structlog.get_logger()
router = APIRouter()


class ReportTicketRequest(BaseModel):
    user_id: str = Field(..., description="Supabase auth user id (profiles.id)")
    plate_id: str = Field(..., description="UUID of the plates row")
    ticket_number: str
    city: str = Field(
        "",
        description="Kelley & Ryan municipality name or numeric town id; optional for Somerville CHS",
    )
    portal_type: Literal["kelley_ryan", "somerville_chs"]


class ReportTicketResponse(BaseModel):
    ok: bool
    error: Optional[str] = None
    new_violation: Optional[bool] = None
    ticket_number: Optional[str] = None
    source_portal: Optional[str] = None
    amount_due: Optional[float] = None
    status: Optional[str] = None
    violation_description: Optional[str] = None
    location: Optional[str] = None
    due_date: Optional[str] = None


@router.post("/report-ticket", response_model=ReportTicketResponse)
async def report_ticket(
    body: ReportTicketRequest,
    auth_user_id: str = Depends(verify_supabase_jwt),
) -> ReportTicketResponse:
    """
    Look up a ticket on Kelley & Ryan or Somerville (City Hall Systems) and save the violation.

    Requires a valid Supabase access token; ``user_id`` must match the authenticated user.
    """
    if body.user_id != auth_user_id:
        raise HTTPException(
            status_code=403,
            detail="user_id must match the authenticated user",
        )

    if body.portal_type == "kelley_ryan" and not (body.city or "").strip():
        raise HTTPException(
            status_code=400,
            detail="city is required when portal_type is kelley_ryan",
        )

    service = MonitorService()
    try:
        result = await service.submit_manual_ticket_report(
            user_id=body.user_id.strip(),
            plate_id=body.plate_id.strip(),
            ticket_number=body.ticket_number.strip(),
            city=(body.city or "").strip(),
            portal_type=body.portal_type,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Plate does not belong to the given user_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not result.get("ok"):
        logger.info(
            "manual_ticket_report_not_found",
            portal=body.portal_type,
            ticket_number=body.ticket_number,
        )
        return ReportTicketResponse(
            ok=False,
            error=result.get("error") or "Ticket not found",
        )

    return ReportTicketResponse(
        ok=True,
        new_violation=result.get("new_violation"),
        ticket_number=result.get("ticket_number"),
        source_portal=result.get("source_portal"),
        amount_due=result.get("amount_due"),
        status=result.get("status"),
        violation_description=result.get("violation_description"),
        location=result.get("location"),
        due_date=result.get("due_date"),
    )
