from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Optional

import requests


logger = logging.getLogger(__name__)

API_BASE_URL = "https://bostonma.rmcpay.com/rmcapi/api/violation_index.php"


class BostonParkingError(Exception):
    """Raised when the Boston parking lookup fails."""


@dataclasses.dataclass
class BostonViolation:
    violation_id: str
    violation_number: str
    plate: str
    state: str
    raw: Dict[str, Any]


def _build_search_params(plate: str, state: str) -> Dict[str, str]:
    normalized_plate = plate.strip().upper()
    normalized_state = state.strip().upper()

    return {
        "lpn": normalized_plate,
        "stateid": normalized_state,
        "operatorid": "bostonma",
        "plate_type_id": "0",
        "devicenumber": "",
        "payment_plan_id": "",
        "immobilization_id": "",
        "single_violation": "false",
    }


def search_tickets(
    plate: str,
    state: str = "MA",
    *,
    session: Optional[requests.sessions.Session] = None,
    timeout: float = 10.0,
) -> List[BostonViolation]:
    """
    Search for parking tickets for a given plate and state in Boston.

    This calls the public RMCPay JSON API used by the City of Boston's
    parking portal:
        https://bostonma.rmcpay.com/rmcapi/api/violation_index.php/searchviolation
    """
    if not plate:
        raise ValueError("plate is required")

    if not state:
        raise ValueError("state is required")

    sess: requests.sessions.Session
    if session is None:
        sess = requests.Session()
    else:
        sess = session

    params = _build_search_params(plate, state)
    url = f"{API_BASE_URL}/searchviolation"

    logger.info(
        "Requesting Boston parking violations for %s (%s)", params["lpn"], params["stateid"]
    )

    try:
        resp = sess.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise BostonParkingError(f"HTTP error talking to Boston parking API: {exc}") from exc

    try:
        payload: Dict[str, Any] = resp.json()
    except ValueError as exc:
        raise BostonParkingError("Boston parking API returned non-JSON response") from exc

    status = payload.get("status")
    errorcode = payload.get("errorcode")

    # No tickets case: API returns status 404 + errorcode 10 with empty data array.
    if status == 404 and errorcode == 10:
        return []

    if status != 200 or errorcode not in (0, None):
        reason = payload.get("reason") or "Unknown error"
        raise BostonParkingError(
            f"Boston parking API error {status} (code {errorcode}): {reason}"
        )

    data = payload.get("data") or []
    violations: List[BostonViolation] = []

    for item in data:
        try:
            violation_id = str(item.get("violation_id") or "")
            violation_number = str(item.get("violation_number") or "")
            lpn = str(item.get("lpn") or params["lpn"])
            stateid = str(item.get("stateid") or params["stateid"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Skipping malformed violation item: %r (%s)", item, exc)
            continue

        if not violation_id or not violation_number:
            logger.debug("Skipping violation with missing ids: %r", item)
            continue

        violations.append(
            BostonViolation(
                violation_id=violation_id,
                violation_number=violation_number,
                plate=lpn,
                state=stateid,
                raw=item,
            )
        )

    return violations


def check_plate_tickets(
    plate: str,
    state: str,
    *,
    session: Optional[requests.sessions.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    Public helper used by the CLI.

    Returns a JSON-serializable structure summarizing the search result.
    """
    violations = search_tickets(plate, state, session=session, timeout=timeout)

    return {
        "plate": plate.strip().upper(),
        "state": state.strip().upper(),
        "has_tickets": bool(violations),
        "count": len(violations),
        "tickets": [dataclasses.asdict(v) for v in violations],
    }

