from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.ezdrivema.com/paybyplatemalogin"

# Stored on violations.source_portal for manual reports / rechecks
EZDRIVEMA_PORTAL = "ezdrivema"


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


def _state_abbr_to_dropdown_value(login_html: str) -> Dict[str, str]:
    """Map two-letter state codes to ``ddlLicensePlateState`` option values from the login page."""
    soup = BeautifulSoup(login_html, "html.parser")
    mapping: Dict[str, str] = {}
    for sel in soup.find_all("select"):
        name = sel.get("name") or ""
        if "ddlLicensePlateState" not in name:
            continue
        for opt in sel.find_all("option"):
            val = (opt.get("value") or "").strip()
            if not val or val == "0":
                continue
            text = re.sub(r"\s+", " ", (opt.get_text() or "").strip().replace("\xa0", " "))
            m = re.search(r"\(([A-Z]{2})\)\s*$", text)
            if m:
                mapping[m.group(1)] = val
            m2 = re.match(r"^([A-Z]{2})\s*[-–]", text)
            if m2:
                mapping[m2.group(1)] = val
            if re.search(r"massachusetts", text, re.I):
                mapping.setdefault("MA", val)
    return mapping


_FAILURE_MARKERS = (
    "invalid invoice",
    "invalid login",
    "could not be found",
    "could not locate",
    "no invoices match",
    "we were unable to locate",
    "unable to verify",
    "no matching",
    "please check your invoice",
    "login failed",
)

_SUCCESS_MARKERS = (
    "amount due",
    "balance due",
    "total due",
    "unpaid toll",
    "invoice balance",
    "open invoice",
    "payment due",
    "make a payment",
    "account summary",
)


def _response_indicates_invoice_found(html: str) -> bool:
    low = html.lower()
    if any(x in low for x in _FAILURE_MARKERS):
        return False
    if any(x in low for x in _SUCCESS_MARKERS):
        return True
    if re.search(r"\$\s*[\d,]+\.\d{2}", html):
        return True
    if "txtinvoicenumber" in low and "lbpbplogin" in low:
        return False
    return True


def _parse_amounts_from_html(html: str) -> Optional[float]:
    found = re.findall(r"\$\s*([\d,]+\.\d{2})", html)
    if not found:
        return None
    nums = [float(x.replace(",", "")) for x in found]
    return max(nums)


def _details_from_ezdrive_html(
    html: str,
    *,
    invoice_number: str,
    plate: str,
    state_abbr: str,
) -> Dict[str, Any]:
    amt = _parse_amounts_from_html(html)
    return {
        "violation_number": invoice_number,
        "violation_id": invoice_number,
        "ticket_number": invoice_number,
        "invoice_number": invoice_number,
        "amount_due": amt,
        "violation_description": "EZDriveMA Pay By Plate MA (toll)",
        "source": "EZDriveMA",
    }


def invoice_lookup_for_manual_report(
    invoice_number: str,
    plate: str,
    state_abbr: str,
    *,
    timeout: float = 30.0,
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Validate invoice + plate on Pay By Plate MA and return structured fields for storage.

    Returns ``(found, details_dict, html_excerpt)``. ``details_dict`` is empty when not found.
    """
    inv = (invoice_number or "").strip()
    pl = re.sub(r"[^A-Za-z0-9]", "", (plate or "").upper())
    st = (state_abbr or "").strip().upper()
    if not inv or not pl or not st:
        raise ValueError("invoice_number, plate, and state are required")

    sess = requests.Session()
    login_html, hidden = _initial_get(sess, timeout=timeout)
    mapping = _state_abbr_to_dropdown_value(login_html)
    state_code = mapping.get(st)
    if not state_code:
        if st.isdigit():
            state_code = st
        else:
            raise EzDriveMaError(
                f"State {st!r} not found in EZDriveMA plate state dropdown; try another state or contact support."
            )

    payload = _build_login_payload(hidden, inv, pl, state_code)
    try:
        resp = sess.post(LOGIN_URL, data=payload, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise EzDriveMaError(f"POST to EZDriveMA failed: {exc}") from exc

    post_html = resp.text
    excerpt = post_html[:8000]

    if not _response_indicates_invoice_found(post_html):
        return False, {}, excerpt

    details = _details_from_ezdrive_html(
        post_html, invoice_number=inv, plate=pl, state_abbr=st
    )
    return True, details, excerpt


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

