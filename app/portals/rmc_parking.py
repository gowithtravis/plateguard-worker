"""
RMC Pay municipal parking portals (shared JSON API across MA cities).

Each city uses the same API shape; only the host subdomain and operatorid differ.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# City display name (stored in plates.portals & violations.source_portal) → RMC host + operatorid
RMC_PAY_PORTALS: Dict[str, Dict[str, str]] = {
    "Boston (RMC Pay)": {
        "host": "bostonma.rmcpay.com",
        "operator_id": "bostonma",
    },
    "New Bedford (RMC Pay)": {
        "host": "newbedford.rmcpay.com",
        "operator_id": "newbedford",
    },
    "Lowell (RMC Pay)": {
        "host": "lowellma.rmcpay.com",
        "operator_id": "lowellma",
    },
    "Brookline (RMC Pay)": {
        "host": "brookline.rmcpay.com",
        "operator_id": "brookline",
    },
}


def default_rmc_portal_labels() -> List[str]:
    """Default `plates.portals` list: all supported RMC Pay cities."""
    return list(RMC_PAY_PORTALS.keys())


class RmcParkingError(Exception):
    """Raised when an RMC Pay parking lookup fails."""


@dataclasses.dataclass
class RmcViolation:
    violation_id: str
    violation_number: str
    plate: str
    state: str
    raw: Dict[str, Any]


def _api_base_url(host: str) -> str:
    host = host.strip().lower().rstrip("/")
    if "://" in host:
        raise ValueError("host must be a bare hostname, e.g. bostonma.rmcpay.com")
    return f"https://{host}/rmcapi/api/violation_index.php"


def _build_search_params(
    plate: str,
    state: str,
    operator_id: str,
) -> Dict[str, str]:
    normalized_plate = plate.strip().upper()
    normalized_state = state.strip().upper()

    return {
        "lpn": normalized_plate,
        "stateid": normalized_state,
        "operatorid": operator_id,
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
    host: str,
    operator_id: str,
    session: Optional[requests.sessions.Session] = None,
    timeout: float = 10.0,
) -> List[RmcViolation]:
    """
    Search parking tickets for a plate via the RMC Pay JSON API.

    ``host`` is the full RMC hostname (e.g. ``bostonma.rmcpay.com``).
    ``operator_id`` must match the city's operator id (typically the subdomain label).
    """
    if not plate:
        raise ValueError("plate is required")
    if not state:
        raise ValueError("state is required")
    if not host:
        raise ValueError("host is required")
    if not operator_id:
        raise ValueError("operator_id is required")

    sess: requests.sessions.Session
    if session is None:
        sess = requests.Session()
    else:
        sess = session

    params = _build_search_params(plate, state, operator_id)
    base = _api_base_url(host)
    url = f"{base}/searchviolation"

    logger.info(
        "Requesting RMC Pay violations host=%s operator=%s plate=%s state=%s",
        host,
        operator_id,
        params["lpn"],
        params["stateid"],
    )

    try:
        resp = sess.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise RmcParkingError(f"HTTP error talking to RMC Pay API ({host}): {exc}") from exc

    try:
        payload: Dict[str, Any] = resp.json()
    except ValueError as exc:
        raise RmcParkingError(f"RMC Pay API ({host}) returned non-JSON response") from exc

    status = payload.get("status")
    errorcode = payload.get("errorcode")

    if status == 404 and errorcode == 10:
        return []

    if status != 200 or errorcode not in (0, None):
        reason = payload.get("reason") or "Unknown error"
        raise RmcParkingError(
            f"RMC Pay API error ({host}) {status} (code {errorcode}): {reason}"
        )

    data = payload.get("data") or []
    violations: List[RmcViolation] = []

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
            RmcViolation(
                violation_id=violation_id,
                violation_number=violation_number,
                plate=lpn,
                state=stateid,
                raw=item,
            )
        )

    return violations


def check_plate_tickets_for_portal(
    portal_label: str,
    plate: str,
    state: str,
    *,
    session: Optional[requests.sessions.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    Run a search for one configured RMC portal (by ``portal_label`` key in
    :data:`RMC_PAY_PORTALS`). Returns JSON-serializable summary for the monitor service.
    """
    if portal_label not in RMC_PAY_PORTALS:
        raise ValueError(f"Unknown RMC Pay portal label: {portal_label!r}")

    cfg = RMC_PAY_PORTALS[portal_label]
    violations = search_tickets(
        plate,
        state,
        host=cfg["host"],
        operator_id=cfg["operator_id"],
        session=session,
        timeout=timeout,
    )

    return {
        "portal": portal_label,
        "plate": plate.strip().upper(),
        "state": state.strip().upper(),
        "has_tickets": bool(violations),
        "count": len(violations),
        "tickets": [dataclasses.asdict(v) for v in violations],
    }
