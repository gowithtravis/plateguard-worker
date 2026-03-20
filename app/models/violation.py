"""
Violation models (placeholder).

Define rich Pydantic models/enums here if you want structured violations
instead of raw dicts from the scrapers.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class ViolationType(str, Enum):
    parking = "parking"
    toll = "toll"
    speed_camera = "speed_camera"
    red_light_camera = "red_light_camera"


class ViolationStatus(str, Enum):
    open = "open"
    paid = "paid"
    appealed = "appealed"
    voided = "voided"
    past_due = "past_due"


class Violation(BaseModel):
    violation_type: ViolationType = ViolationType.parking
    source_portal: str
    ticket_number: str
    plate_number: str
    state: str = "MA"
    # When known (e.g. batch checks from Supabase), used to resolve user email for alerts
    plate_id: Optional[str] = None
    amount_due: Optional[float] = None
    violation_description: Optional[str] = None
    issue_date: Optional[datetime] = None
    location: Optional[str] = None
    status: ViolationStatus = ViolationStatus.open
    due_date: Optional[datetime] = None
    late_fee_amount: Optional[float] = None
    vehicle_make: Optional[str] = None
    vehicle_color: Optional[str] = None
    photo_urls: Optional[list[str]] = None
    raw_data: Any = None

