"""
Kelley & Ryan ePay — parking ticket lookup (MA municipalities).

https://epay.kelleyryan.com/search — HTML form, CSRF + town (numeric) + plate + ticket.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://epay.kelleyryan.com"
SEARCH_PATH = "/search"

KELLEY_RYAN_PORTAL = "kelley_ryan"

# Known MA towns (synced with live dropdown; full list refreshed via fetch_town_name_to_id).
PRIORITY_TOWN_IDS: Dict[str, str] = {
    "somerville": "17",
    "worcester": "4",
    "springfield": "3",
    "brockton": "6",
    "lawrence": "9",
    "malden": "12",
    "revere": "36",
    "watertown": "39",
    "quincy": "16",
    "salem": "37",
    "medford": "13",
}

_NO_RESULTS_MARKERS = (
    "no results found!",
    "could not find any bills matching your search criteria",
)

USER_AGENT = (
    "Mozilla/5.0 (compatible; PlateGuardWorker/1.0; +https://plateguard.io)"
)


class KelleyRyanError(Exception):
    """Raised when the Kelley & Ryan portal returns an unexpected response."""


@dataclass
class KelleyRyanTicketResult:
    """Outcome of a parking ticket search."""

    found: bool
    town_id: str
    town_label: Optional[str]
    plate: str
    ticket_number: str
    details: Optional[Dict[str, Any]] = None
    raw_html_excerpt: Optional[str] = None


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )


def fetch_town_name_to_id(client: Optional[httpx.Client] = None) -> Dict[str, str]:
    """
    Parse ``<select name="town">`` options from the search page.

    Keys are normalized lowercase names (e.g. ``"somerville"`` → ``"17"``).
    Prefer matching ``PRIORITY_TOWN_IDS`` when IDs match expected cities.
    """
    own_client = client is None
    c = client or _client()
    try:
        r = c.get(SEARCH_PATH)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        select = soup.find("select", attrs={"name": "town"})
        if not select:
            raise KelleyRyanError("town dropdown not found on search page")

        mapping: Dict[str, str] = {}
        for opt in select.find_all("option"):
            value = (opt.get("value") or "").strip()
            label = (opt.get_text() or "").strip()
            if not value or not label:
                continue
            key = _normalize_city_key(label)
            if key:
                mapping[key] = value
        # Ensure priority IDs stay addressable by short city name
        mapping.update(PRIORITY_TOWN_IDS)
        return mapping
    finally:
        if own_client:
            c.close()


def _normalize_city_key(label: str) -> str:
    s = label.strip()
    s = re.sub(r",\s*ma\b.*$", "", s, flags=re.I).strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def resolve_town_id(city: str, towns: Dict[str, str]) -> str:
    """
    Resolve a user-provided city string to a numeric ``town`` form value.

    Accepts: numeric ID, ``"Somerville"``, ``"SOMERVILLE, MA"``, etc.
    """
    raw = (city or "").strip()
    if not raw:
        raise ValueError("city is required for Kelley & Ryan searches")

    if raw.isdigit():
        return raw

    key = _normalize_city_key(raw)
    if key in towns:
        return towns[key]

    for prefix in ("city of ", "town of "):
        if key.startswith(prefix):
            short = key[len(prefix) :].strip()
            if short in towns:
                return towns[short]

    raise ValueError(
        f"Unknown Kelley & Ryan city/town: {city!r}. "
        "Use a Massachusetts municipality name from the portal dropdown."
    )


def _is_no_results(html: str) -> bool:
    lower = html.lower()
    return any(m in lower for m in _NO_RESULTS_MARKERS)


def _parse_money(text: str) -> Optional[float]:
    m = re.search(r"\$\s*([\d,]+\.\d{2})", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_dates(text: str) -> list[datetime]:
    out: list[datetime] = []
    for m in re.finditer(
        r"\b(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})\b", text
    ):
        s = m.group(1)
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
            try:
                out.append(datetime.strptime(s, fmt))
                break
            except ValueError:
                continue
    return out


def _parse_violation_from_html(html: str, ticket_number: str) -> Dict[str, Any]:
    """Best-effort parse of result HTML into RMCPay-shaped keys for Violation mapping."""
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one(".card-body") or soup

    # Key / value rows from tables
    kv: Dict[str, str] = {}
    for row in root.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
        if len(cells) >= 2 and cells[0] and cells[1]:
            k = cells[0].rstrip(":").strip()
            kv[k.lower()] = cells[1]

    blob = root.get_text("\n", strip=True)
    amount = _parse_money(blob)
    if amount is None:
        for label in ("amount due", "balance", "total", "fine"):
            for ck, cv in kv.items():
                if label in ck:
                    amount = _parse_money(cv)
                    break
            if amount is not None:
                break

    issue_date = None
    due_date = None
    for ck, cv in kv.items():
        if "issue" in ck or "violation date" in ck or "date issued" in ck:
            issue_date = cv
        if "due" in ck and "paid" not in ck:
            due_date = cv

    if not issue_date:
        dates = _parse_dates(blob)
        if dates:
            issue_date = dates[0].strftime("%Y-%m-%d")
    if not due_date:
        dates = _parse_dates(blob)
        if len(dates) > 1:
            due_date = dates[-1].strftime("%Y-%m-%d")

    status_text = None
    for ck, cv in kv.items():
        if "status" in ck:
            status_text = cv
            break
    if not status_text and re.search(r"\bpaid\b", blob, re.I):
        status_text = "Paid"

    description = None
    for ck, cv in kv.items():
        if any(x in ck for x in ("violation", "offense", "description", "comment")):
            description = cv
            break

    location = None
    for ck, cv in kv.items():
        if any(x in ck for x in ("location", "address", "where", "block")):
            location = cv
            break

    return {
        "violation_number": ticket_number,
        "violation_id": ticket_number,
        "amount_due": amount,
        "balance": amount,
        "fine_amount": amount,
        "issue_date": issue_date,
        "due_date": due_date,
        "violation_description": description,
        "location": location,
        "status_text": status_text,
        "portal": KELLEY_RYAN_PORTAL,
        "kv_pairs": kv,
    }


def search_parking_ticket(
    city: str,
    plate: str,
    ticket_number: str,
    *,
    town_map: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> KelleyRyanTicketResult:
    """
    Look up a parking ticket: GET search page (CSRF), POST /search with parking fields.
    """
    plate_n = (plate or "").strip().upper()
    ticket_n = (ticket_number or "").strip()
    if not plate_n or not ticket_n:
        raise ValueError("plate and ticket_number are required")

    own_client = client is None
    c = client or _client()
    try:
        towns = town_map if town_map is not None else fetch_town_name_to_id(c)
        town_id = resolve_town_id(city, towns)
        town_label = None
        for name, tid in towns.items():
            if tid == town_id:
                town_label = name
                break

        g = c.get(SEARCH_PATH)
        g.raise_for_status()
        soup = BeautifulSoup(g.text, "lxml")
        csrf_el = soup.find("input", attrs={"name": "csrf"})
        if not csrf_el or not csrf_el.get("value"):
            raise KelleyRyanError("CSRF token not found on search page")
        csrf = csrf_el["value"]

        r = c.post(
            SEARCH_PATH,
            data={
                "csrf": csrf,
                "search": "parking",
                "town": town_id,
                "plate": plate_n,
                "ticket": ticket_n,
            },
        )
        r.raise_for_status()
        html = r.text

        if _is_no_results(html):
            return KelleyRyanTicketResult(
                found=False,
                town_id=town_id,
                town_label=town_label,
                plate=plate_n,
                ticket_number=ticket_n,
            )

        details = _parse_violation_from_html(html, ticket_n)
        details["town_id"] = town_id
        details["city_query"] = city

        excerpt = html[:8000] if len(html) > 8000 else html
        return KelleyRyanTicketResult(
            found=True,
            town_id=town_id,
            town_label=town_label,
            plate=plate_n,
            ticket_number=ticket_n,
            details=details,
            raw_html_excerpt=excerpt,
        )
    finally:
        if own_client:
            c.close()
