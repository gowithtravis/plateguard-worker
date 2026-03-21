"""
Backward-compatible entry points for the Boston RMC Pay portal.

New code should use :mod:`app.portals.rmc_parking` and :data:`RMC_PAY_PORTALS`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from .rmc_parking import (
    RMC_PAY_PORTALS,
    RmcParkingError,
    RmcViolation,
    check_plate_tickets_for_portal,
    search_tickets as rmc_search_tickets,
)

# Legacy names
BostonParkingError = RmcParkingError
BostonViolation = RmcViolation

_BOSTON_LABEL = "Boston (RMC Pay)"


def search_tickets(
    plate: str,
    state: str = "MA",
    *,
    session: Optional[requests.sessions.Session] = None,
    timeout: float = 10.0,
) -> List[BostonViolation]:
    """Search Boston only (same API as :func:`rmc_parking.search_tickets`)."""
    cfg = RMC_PAY_PORTALS[_BOSTON_LABEL]
    return rmc_search_tickets(
        plate,
        state,
        host=cfg["host"],
        operator_id=cfg["operator_id"],
        session=session,
        timeout=timeout,
    )


def check_plate_tickets(
    plate: str,
    state: str,
    *,
    session: Optional[requests.sessions.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """CLI / legacy helper — Boston RMC Pay only."""
    return check_plate_tickets_for_portal(
        _BOSTON_LABEL,
        plate,
        state,
        session=session,
        timeout=timeout,
    )
