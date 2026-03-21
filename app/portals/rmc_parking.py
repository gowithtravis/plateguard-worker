"""
RMC Pay municipal parking portals (shared JSON API across MA cities).

Each city uses the same API shape; only the host subdomain and ``operatorid`` differ.

**``operator_id`` in config** is sent as the ``operatorid`` query parameter. It must match
what that city's RMC Pay site uses (often the same as the hostname prefix, e.g. ``quincyma``
for ``quincyma.rmcpay.com``, but not guaranteed). Confirm via browser DevTools → Network
when running a plate search on the city's portal if lookups fail or return non-JSON.

Optional per-portal key **``api_base_path``** overrides the default RMC API base path if a
city uses a different URL layout (see logs for the exact ``url`` requested).
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# City display name (stored in plates.portals & violations.source_portal) → RMC host + operatorid
# Optional ``api_base_path`` overrides DEFAULT_RMC_API_BASE_PATH for non-standard deployments.
RMC_PAY_PORTALS: Dict[str, Dict[str, Any]] = {
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
    "Chelsea (RMC Pay)": {
        "host": "chelseama.rmcpay.com",
        "operator_id": "chelseama",
    },
    "Salem (RMC Pay)": {
        "host": "salemma.rmcpay.com",
        "operator_id": "salemma",
    },
    "Quincy (RMC Pay)": {
        "host": "quincyma.rmcpay.com",
        "operator_id": "quincyma",
    },
    "Salisbury (RMC Pay)": {
        "host": "salisburyma.rmcpay.com",
        "operator_id": "salisburyma",
    },
    "Northampton (RMC Pay)": {
        "host": "northampton.rmcpay.com",
        "operator_id": "northampton",
    },
    "Plymouth County (RMC Pay)": {
        "host": "plymouthma.rmcpay.com",
        "operator_id": "plymouthma",
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


DEFAULT_RMC_API_BASE_PATH = "/rmcapi/api/violation_index.php"


def _api_base_url(host: str, api_base_path: Optional[str] = None) -> str:
    host = host.strip().lower().rstrip("/")
    if "://" in host:
        raise ValueError("host must be a bare hostname, e.g. bostonma.rmcpay.com")
    path = (api_base_path or DEFAULT_RMC_API_BASE_PATH).strip()
    if not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/")
    return f"https://{host}{path}"


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
    api_base_path: Optional[str] = None,
    session: Optional[requests.sessions.Session] = None,
    timeout: float = 10.0,
) -> List[RmcViolation]:
    """
    Search parking tickets for a plate via the RMC Pay JSON API.

    ``host`` is the full RMC hostname (e.g. ``bostonma.rmcpay.com``).
    ``operator_id`` is sent as query param ``operatorid`` and must match that portal's RMC
    deployment (verify in Network tab if responses are not JSON).
    ``api_base_path`` optionally overrides :data:`DEFAULT_RMC_API_BASE_PATH`.
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
    base = _api_base_url(host, api_base_path)
    url = f"{base}/searchviolation"

    # Full URL after encoding — log before request so failures still show intent.
    req = requests.Request("GET", url, params=params).prepare()
    full_url = req.url
    logger.info(
        "rmc_pay_search_request url=%s host=%s operator_id=%s plate=%s state=%s",
        full_url,
        host,
        operator_id,
        params["lpn"],
        params["stateid"],
    )

    try:
        resp = sess.send(req, timeout=timeout)
    except requests.RequestException as exc:
        raise RmcParkingError(f"HTTP error talking to RMC Pay API ({host}): {exc}") from exc

    # Log final URL after redirects (if any).
    if resp.url != full_url:
        logger.info("rmc_pay_search_response_final_url url=%s", resp.url)

    try:
        payload: Dict[str, Any] = resp.json()
    except ValueError as exc:
        body = resp.text or ""
        preview = body[:500]
        logger.error(
            "rmc_pay_non_json_response host=%s status_code=%s url=%s content_type=%s "
            "body_preview_first_500_chars=%r",
            host,
            resp.status_code,
            resp.url,
            resp.headers.get("Content-Type"),
            preview,
        )
        raise RmcParkingError(
            f"RMC Pay API ({host}) returned non-JSON response "
            f"(HTTP {resp.status_code}); see worker logs for url and body preview"
        ) from exc

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
        api_base_path=cfg.get("api_base_path"),
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
