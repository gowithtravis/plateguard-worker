"""
Somerville — City Hall Systems ePay parking ticket lookup.

https://epay.cityhallsystems.com — municipality selection, then parking ticket (``pt``)
form with ticket number + plate. No browser required (httpx + session cookies).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from bs4 import BeautifulSoup

HOME_URL = "https://epay.cityhallsystems.com/"
SELECTION_URL = "https://epay.cityhallsystems.com/selection"

# Somerville's municipality code on the CHS landing form
SOMERVILLE_MUNI_CODE = "somerville.ma.us"
SOMERVILLE_CHS_PORTAL = "somerville_chs"

USER_AGENT = (
    "Mozilla/5.0 (compatible; PlateGuardWorker/1.0; +https://plateguard.io)"
)


class SomervilleCHSError(Exception):
    """Raised when the City Hall Systems portal returns an unexpected response."""


@dataclass
class SomervilleCHSTicketResult:
    found: bool
    plate: str
    ticket_number: str
    details: Optional[Dict[str, Any]] = None
    final_url: Optional[str] = None
    raw_html_excerpt: Optional[str] = None


def _extract_form_token(html: str) -> str:
    m = re.search(r'name="form\[_token\]" value="([^"]+)"', html)
    if not m:
        raise SomervilleCHSError("form[_token] not found in CHS page")
    return m.group(1)


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )


def _bill_page_heuristic(html: str, final_url: str) -> bool:
    """
    Distinguish a bill / payment view from the ticket search form.

    CHS often returns HTTP 200 on the same ``/selection`` URL even when no bill exists,
    so we use content signals that appear on real bill pages.
    """
    lower = html.lower()
    if any(
        frag in final_url.lower()
        for frag in ("/bill", "/cart", "/payment", "/checkout")
    ):
        return True
    if re.search(r'href="[^"]*/(bill|cart)[^"]*"', html, re.I):
        return True
    if re.search(r"\$\s*[\d,]+\.\d{2}", html):
        return True
    if "current balance" in lower and re.search(
        r"\$\s*[\d,]+\.\d{2}", html
    ):
        return True
    if "amount due" in lower and re.search(r"\$\s*[\d,]+\.\d{2}", html):
        return True
    if "pay this amount" in lower:
        return True
    if "view/pay" in lower and "bill" in lower:
        return True
    return False


def _parse_money(text: str) -> Optional[float]:
    amounts: list[float] = []
    for m in re.finditer(r"\$\s*([\d,]+\.\d{2})", text):
        try:
            amounts.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    return max(amounts) if amounts else None


def _parse_chs_details(html: str, ticket_number: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    amount = _parse_money(text)

    kv: Dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
        if len(cells) >= 2 and cells[0] and cells[1]:
            kv[cells[0].rstrip(":").strip().lower()] = cells[1]

    status_text = None
    for k, v in kv.items():
        if "status" in k:
            status_text = v
            break
    if not status_text and re.search(r"\bpaid\b", text, re.I):
        status_text = "Paid"

    description = None
    for k, v in kv.items():
        if "violation" in k or "description" in k:
            description = v
            break

    location = None
    for k, v in kv.items():
        if "location" in k or "address" in k:
            location = v
            break

    return {
        "violation_number": ticket_number,
        "violation_id": ticket_number,
        "amount_due": amount,
        "balance": amount,
        "fine_amount": amount,
        "violation_description": description,
        "location": location,
        "status_text": status_text,
        "portal": SOMERVILLE_CHS_PORTAL,
        "kv_pairs": kv,
    }


def search_parking_ticket(
    plate: str,
    ticket_number: str,
    *,
    client: Optional[httpx.Client] = None,
) -> SomervilleCHSTicketResult:
    """
    Somerville parking ticket lookup: select municipality, open ``pt`` flow, search bill.
    """
    plate_n = (plate or "").strip().upper()
    ticket_n = (ticket_number or "").strip()
    if not plate_n or not ticket_n:
        raise ValueError("plate and ticket_number are required")

    own_client = client is None
    c = client or _client()
    try:
        c.get(HOME_URL).raise_for_status()
        r0 = c.post(
            HOME_URL,
            data={"code": SOMERVILLE_MUNI_CODE, "submit": "1"},
        )
        r0.raise_for_status()

        r1 = c.get(SELECTION_URL)
        r1.raise_for_status()
        token = _extract_form_token(r1.text)

        r2 = c.post(
            SELECTION_URL,
            data={"form[code]": "pt", "form[for]": "", "form[_token]": token},
        )
        r2.raise_for_status()
        token2 = _extract_form_token(r2.text)

        r3 = c.post(
            SELECTION_URL,
            data={
                "form[code]": "pt",
                "form[for]": ticket_n,
                "form[plate]": plate_n,
                "form[_token]": token2,
            },
        )
        r3.raise_for_status()

        html = r3.text
        final_url = str(r3.url)
        found = _bill_page_heuristic(html, final_url)

        if not found:
            return SomervilleCHSTicketResult(
                found=False,
                plate=plate_n,
                ticket_number=ticket_n,
                final_url=final_url,
            )

        details = _parse_chs_details(html, ticket_n)
        excerpt = html[:8000] if len(html) > 8000 else html
        return SomervilleCHSTicketResult(
            found=True,
            plate=plate_n,
            ticket_number=ticket_n,
            details=details,
            final_url=final_url,
            raw_html_excerpt=excerpt,
        )
    finally:
        if own_client:
            c.close()
