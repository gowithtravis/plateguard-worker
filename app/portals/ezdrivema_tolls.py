from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.ezdrivema.com/paybyplatemalogin"


class EzDriveMaError(Exception):
    """Raised when the EZDriveMA toll lookup fails."""


@dataclasses.dataclass
class EzDriveMaInvoice:
    invoice_number: str
    plate: str
    state: str
    raw_html: str


def _initial_get(session: requests.sessions.Session, timeout: float) -> Tuple[str, Dict[str, str]]:
    """
    Perform the initial GET to fetch ASP.NET hidden fields and cookies.
    """
    resp = session.get(LOGIN_URL, timeout=timeout)
    try:
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise EzDriveMaError(f"Initial GET to EZDriveMA failed: {exc}") from exc

    soup = BeautifulSoup(resp.text, "html.parser")

    def value_of(name: str, default: str = "") -> str:
        el = soup.find("input", {"name": name})
        return el.get("value", default) if el else default

    fields = {
        "__VIEWSTATE": value_of("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": value_of("__VIEWSTATEGENERATOR"),
        "__VIEWSTATEENCRYPTED": value_of("__VIEWSTATEENCRYPTED"),
        "__EVENTVALIDATION": value_of("__EVENTVALIDATION"),
        "__dnnVariable": value_of("__dnnVariable"),
        "ScrollTop": value_of("ScrollTop", "0"),
        "__RequestVerificationToken": value_of("__RequestVerificationToken"),
        "dnn$ctr1035$View$hdnEnforceNumericOnly": value_of(
            "dnn$ctr1035$View$hdnEnforceNumericOnly", "Y"
        ),
    }

    return resp.text, fields


def _build_login_payload(
    hidden_fields: Dict[str, str],
    invoice_number: str,
    plate: str,
    state_code: str,
) -> Dict[str, str]:
    payload = dict(hidden_fields)
    payload.update(
        {
            "__EVENTTARGET": "dnn$ctr1035$View$lbPbpLogin",
            "__EVENTARGUMENT": "",
            "dnn$ctr1035$View$ddAuthTypeInv": "InvoiceNumber",
            "dnn$ctr1035$View$txtInvoiceNumber": invoice_number,
            "dnn$ctr1035$View$txtLicensePlate": plate,
            "dnn$ctr1035$View$ddlLicensePlateState": state_code,
            "dnn$Header1$dnnSEARCH$txtSearch": "",
        }
    )
    return payload


def lookup_invoices_by_plate(
    invoice_number: str,
    plate: str,
    state: str,
    *,
    session: Optional[requests.sessions.Session] = None,
    timeout: float = 15.0,
) -> List[EzDriveMaInvoice]:
    """
    Attempt a best-effort login to EZDriveMA Pay By Plate by invoice + plate.

    This function intentionally does not try to parse the post-login HTML into
    individual toll transactions, as that requires real credentials and may
    change without notice. Instead, it returns one EzDriveMaInvoice wrapper
    around the raw HTML response when login appears to succeed.
    """
    if not invoice_number:
        raise ValueError("invoice_number is required")
    if not plate:
        raise ValueError("plate is required")
    if not state:
        raise ValueError("state is required")

    sess: requests.sessions.Session
    if session is None:
        sess = requests.Session()
    else:
        sess = session

    _, hidden_fields = _initial_get(sess, timeout=timeout)

    # NOTE: In the real portal, ddlLicensePlateState is a numeric code
    # (e.g. MA = 26). Mapping from postal abbreviation to numeric codes
    # is out of scope here; callers should pass the numeric state code
    # they want to use. For convenience, accept two-letter codes and
    # simply pass them through when not numeric.
    state_code = state

    payload = _build_login_payload(hidden_fields, invoice_number, plate, state_code)

    try:
        resp = sess.post(LOGIN_URL, data=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise EzDriveMaError(f"POST to EZDriveMA failed: {exc}") from exc

    # We can't reliably distinguish success vs failure without real
    # credentials; for now, treat any 2xx as a "single invoice" view.
    if not (200 <= resp.status_code < 300):
        raise EzDriveMaError(f"EZDriveMA returned HTTP {resp.status_code}")

    html = resp.text
    invoice = EzDriveMaInvoice(
        invoice_number=str(invoice_number),
        plate=plate,
        state=state,
        raw_html=html,
    )
    return [invoice]

