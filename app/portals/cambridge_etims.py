"""
Cambridge, MA parking ticket lookup via eTIMS (Browserbase + Playwright).

Plate search with state, passenger type, DOB (MM/DD), and image CAPTCHA.
CAPTCHA is solved via 2Captcha using a screenshot of ``#captcha``.
"""
from __future__ import annotations

import base64
import re
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import structlog
from bs4 import BeautifulSoup

from ..config import settings

logger = structlog.get_logger(__name__)

CAMBRIDGE_PORTAL_LABEL = "Cambridge (eTIMS)"
CAMBRIDGE_INPUT_URL = "https://wmq.etimspayments.com/pbw/include/cambridge/input.jsp"
ETIMS_ORIGIN = "https://wmq.etimspayments.com"

MAX_SUBMIT_ATTEMPTS = 3
POST_LOAD_CAPTCHA_WAIT_MS = 5_000


class CambridgeEtimError(Exception):
    """Raised when the Cambridge eTIMS flow fails."""


def browserbase_configured() -> bool:
    return bool(
        (settings.browserbase_api_key or "").strip()
        and (settings.browserbase_project_id or "").strip()
    )


def twocaptcha_configured() -> bool:
    return bool((settings.twocaptcha_api_key or "").strip())


def parse_dob_mmdd(value: str) -> tuple[str, str]:
    """
    Parse profile ``dob_mmdd`` (``MM/DD``) into zero-padded month and day
    for the ``birthMonth`` / ``birthDay`` selects.
    """
    s = (value or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s)
    if not m:
        raise CambridgeEtimError(
            f"Invalid dob_mmdd format {value!r}; expected MM/DD (e.g. 03/15)"
        )
    mm, dd = int(m.group(1)), int(m.group(2))
    if mm < 1 or mm > 12 or dd < 1 or dd > 31:
        raise CambridgeEtimError(f"Invalid month/day in dob_mmdd: {value!r}")
    return f"{mm:02d}", f"{dd:02d}"


def _parse_results_html(html: str) -> List[Dict[str, Any]]:
    """
    Best-effort parse of post-submit eTIMS HTML into RMC-shaped ticket dicts.

    Heuristics may need tuning as the vendor updates markup.
    """
    soup = BeautifulSoup(html, "lxml")
    lower = soup.get_text(" ", strip=True).lower()

    if re.search(r"invalid\s+security|invalid\s+captcha|security\s+code\s+you\s+entered", lower):
        raise CambridgeEtimError("Security check failed (invalid CAPTCHA or session)")

    if re.search(
        r"no\s+matching|could\s+not\s+find|did\s+not\s+match|unable\s+to\s+locate|"
        r"no\s+open\s+citations|no\s+citations\s+found|no\s+records\s+found",
        lower,
    ):
        return []

    seen: set[str] = set()
    tickets: List[Dict[str, Any]] = []

    for m in re.finditer(r"ticketNumber=([^&\"'<>\\s]+)", html, flags=re.I):
        raw = unquote(m.group(1).strip())
        if not raw or raw in seen:
            continue
        seen.add(raw)
        tickets.append(
            {
                "violation_number": raw,
                "violation_id": raw,
                "ticket_number": raw,
                "source": CAMBRIDGE_PORTAL_LABEL,
            }
        )

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "ticket" not in href.lower():
            continue
        full = urljoin(ETIMS_ORIGIN, href)
        qs = parse_qs(urlparse(full).query)
        for key in ("ticketNumber", "ticket_number", "ticketNum", "citationNumber"):
            vals = qs.get(key)
            if not vals:
                continue
            raw = unquote(str(vals[0]).strip())
            if raw and raw not in seen:
                seen.add(raw)
                tickets.append(
                    {
                        "violation_number": raw,
                        "violation_id": raw,
                        "ticket_number": raw,
                        "source": CAMBRIDGE_PORTAL_LABEL,
                    }
                )

    money = re.compile(r"\$\s*([\d,]+\.\d{2})")
    for tr in soup.find_all("tr"):
        row_text = tr.get_text(" ", strip=True)
        if not money.search(row_text):
            continue
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        if re.match(r"^[\$]?[\d,]+\.?\d*$", cells[0].strip()):
            continue
        ticket_guess = re.sub(r"[^A-Z0-9\-]", "", cells[0].upper())
        if len(ticket_guess) < 5:
            continue
        if ticket_guess in seen:
            continue
        seen.add(ticket_guess)
        amt_m = money.search(row_text)
        desc = cells[1] if len(cells) > 1 else ""
        loc = cells[2] if len(cells) > 2 else ""
        tickets.append(
            {
                "violation_number": ticket_guess,
                "violation_id": ticket_guess,
                "ticket_number": ticket_guess,
                "amount_due": amt_m.group(1).replace(",", "") if amt_m else None,
                "violation_description": desc or None,
                "location": loc or None,
                "source": CAMBRIDGE_PORTAL_LABEL,
            }
        )

    return tickets


def _still_on_search_form(page: Any) -> bool:
    """True if the plate search form (plate field + CAPTCHA) is still present."""
    try:
        return bool(page.locator("#plateNumber").count() and page.locator("#captcha").count())
    except Exception:
        return False


def _security_error_in_html(html: str) -> bool:
    return bool(
        re.search(
            r"invalid\s+security|invalid\s+captcha|security\s+code\s+you\s+entered",
            html,
            re.I,
        )
    )


def _wait_after_load_for_captcha(page: Any) -> None:
    """Let the page and CAPTCHA image settle before scraping the image."""
    page.wait_for_timeout(POST_LOAD_CAPTCHA_WAIT_MS)


def _fill_plate_fields(page: Any, plate: str, st: str, mm: str, dd: str) -> None:
    page.locator("#ticketNumber").fill("")
    page.locator("#platePrefix").select_option(st)
    page.locator("#plateNumber").fill(plate)
    page.locator("#birthMonth").select_option(mm)
    page.locator("#birthDay").select_option(dd)


def _captcha_text_input(page: Any) -> Tuple[Any, str]:
    """
    Resolve the CAPTCHA answer field. The server uses a random ``name``/``id`` each
    load (e.g. ``LRGQLD``) but keeps ``class="captchaDynamic"``. Prefer the field
    inside ``form[name=inputForm]`` so we never pick a stray input elsewhere.
    """
    specs: List[Tuple[str, str]] = [
        ('form[name="inputForm"] input.captchaDynamic', "form[name=inputForm] input.captchaDynamic"),
        ("input.captchaDynamic", "input.captchaDynamic"),
        (
            "xpath=//img[@id='captcha']/following::input[not(@type='hidden')][1]",
            "xpath=//img[@id='captcha']/following::input[1]",
        ),
    ]
    for css_or_xpath, desc in specs:
        loc = page.locator(css_or_xpath)
        try:
            if loc.count() > 0:
                return loc.first, desc
        except Exception:
            continue
    raise CambridgeEtimError("Could not locate Cambridge CAPTCHA text input")


def _form_field_snapshot(page: Any) -> Dict[str, Any]:
    """Read key form values from the live DOM (for debug logging)."""
    return page.evaluate(
        """() => {
            const f = document.forms.inputForm || document.querySelector('form[name="inputForm"]');
            if (!f) return { error: "inputForm not found" };
            const cap = f.querySelector("input.captchaDynamic");
            return {
                ticketNumber: f.elements.ticketNumber?.value ?? null,
                statePlate: f.elements.statePlate?.value ?? null,
                plateNumber: f.elements.plateNumber?.value ?? null,
                birthMonth: f.elements.birthMonth?.value ?? null,
                birthDay: f.elements.birthDay?.value ?? null,
                plateType: f.elements.plateType?.value ?? null,
                requestType: f.elements.requestType?.value ?? null,
                captchaInputName: cap?.getAttribute("name") ?? null,
                captchaInputId: cap?.id ?? null,
                captchaInputClass: cap?.className ?? null,
                captchaValue: cap?.value ?? null,
            };
        }"""
    )


def _visible_page_errors(page: Any) -> List[str]:
    """Collect likely error/alert text visible after submit."""
    raw = page.evaluate(
        """() => {
            const texts = [];
            const sel = [
                ".error", ".alert", ".alert-danger", ".alert-warning",
                "[role='alert']", ".validationError", ".errormsg",
                "font[color='red']", ".text-danger", "#errorMessage"
            ].join(", ");
            document.querySelectorAll(sel).forEach((el) => {
                const t = (el.textContent || "").trim().replace(/\\s+/g, " ");
                if (t.length > 2 && t.length < 800) texts.push(t);
            });
            return [...new Set(texts)];
        }"""
    )
    return list(raw) if isinstance(raw, list) else []


def _save_submit_debug_screenshot(page: Any, session_id: str, attempt: int) -> str:
    safe = re.sub(r"[^\w.-]+", "_", str(session_id))[:72]
    path = f"/tmp/cambridge_etims_{safe}_a{attempt}_{int(time.time() * 1000)}.png"
    page.screenshot(path=path, full_page=True)
    return path


def _solve_captcha_with_2captcha(
    page: Any,
    solver: Any,
    *,
    attempt: int,
    session_id: str,
) -> str:
    from twocaptcha import ApiException, NetworkException, TimeoutException, ValidationException

    captcha_img = page.locator("#captcha")
    captcha_img.wait_for(state="visible", timeout=30_000)
    png = captcha_img.screenshot(type="png")
    if not png:
        raise CambridgeEtimError("CAPTCHA image screenshot was empty")

    b64 = base64.standard_b64encode(png).decode("ascii")
    logger.info(
        "cambridge_2captcha_request",
        attempt=attempt,
        session_id=session_id,
        png_bytes=len(png),
    )

    try:
        raw = solver.normal(
            b64,
            minLen=3,
            maxLen=8,
        )
    except (ValidationException, ApiException, NetworkException, TimeoutException) as exc:
        logger.warning("cambridge_2captcha_api_error", error=str(exc), attempt=attempt)
        raise CambridgeEtimError(f"2Captcha request failed: {exc}") from exc
    except Exception as exc:
        logger.warning("cambridge_2captcha_error", error=str(exc), attempt=attempt)
        raise CambridgeEtimError(f"2Captcha solve failed: {exc}") from exc

    code = raw.get("code") if isinstance(raw, dict) else raw
    text = str(code or "").strip()
    if not text:
        raise CambridgeEtimError("2Captcha returned an empty solution")

    logger.info(
        "cambridge_2captcha_solved",
        attempt=attempt,
        session_id=session_id,
        solution_length=len(text),
    )
    return text


def _fill_captcha_input_and_submit(
    page: Any,
    solution: str,
    timeout_ms: int,
    *,
    attempt: int,
    session_id: str,
) -> None:
    captcha_inp, selector_desc = _captcha_text_input(page)
    captcha_inp.wait_for(state="visible", timeout=30_000)
    captcha_inp.fill("")
    captcha_inp.fill(solution.strip())

    filled_value = ""
    try:
        filled_value = captcha_inp.input_value()
    except Exception:
        pass

    handle = captcha_inp.element_handle()
    meta: Dict[str, Any] = {}
    if handle:
        try:
            meta = page.evaluate(
                """(el) => ({
                    tag: el.tagName,
                    id: el.id || null,
                    name: el.name || null,
                    type: el.type || null,
                    className: el.className || null,
                    value: el.value || "",
                    readOnly: el.readOnly,
                    disabled: el.disabled,
                })""",
                handle,
            )
        except Exception:
            meta = {}

    logger.info(
        "cambridge_captcha_field_after_fill",
        attempt=attempt,
        session_id=session_id,
        playwright_selector=selector_desc,
        input_value_from_playwright=filled_value,
        input_value_length=len(filled_value),
        dom_field=meta,
    )

    snapshot = _form_field_snapshot(page)
    logger.info(
        "cambridge_form_values_before_submit",
        attempt=attempt,
        session_id=session_id,
        form=snapshot,
    )

    page.locator('input[type="submit"][name="submit"]').click()
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(2000)

    after_url = page.url
    logger.info(
        "cambridge_after_submit",
        attempt=attempt,
        session_id=session_id,
        url=after_url,
    )

    try:
        shot_path = _save_submit_debug_screenshot(page, session_id, attempt)
        logger.info(
            "cambridge_submit_debug_screenshot",
            attempt=attempt,
            session_id=session_id,
            path=shot_path,
        )
    except Exception as exc:
        logger.warning(
            "cambridge_submit_debug_screenshot_failed",
            attempt=attempt,
            session_id=session_id,
            error=str(exc),
        )

    errors = _visible_page_errors(page)
    if errors:
        logger.info(
            "cambridge_visible_errors_after_submit",
            attempt=attempt,
            session_id=session_id,
            messages=errors,
        )
    else:
        logger.info(
            "cambridge_visible_errors_after_submit",
            attempt=attempt,
            session_id=session_id,
            messages=[],
        )


def search_violations_sync(
    plate_number: str,
    state: str,
    dob_mmdd: str,
    *,
    timeout_ms: int = 120_000,
) -> List[Dict[str, Any]]:
    """
    Run Browserbase + Playwright against Cambridge eTIMS (blocking).

    Returns ticket dicts compatible with :meth:`MonitorService._from_rmc_ticket`.
    """
    if not browserbase_configured():
        raise CambridgeEtimError("Browserbase is not configured (API key / project id)")
    if not twocaptcha_configured():
        raise CambridgeEtimError("2Captcha is not configured (TWOCAPTCHA_API_KEY)")

    plate = re.sub(r"[^A-Z0-9]", "", (plate_number or "").upper())
    st = (state or "MA").strip().upper()
    if not plate:
        raise CambridgeEtimError("plate_number is required")
    mm, dd = parse_dob_mmdd(dob_mmdd)

    from browserbase import Browserbase
    from playwright.sync_api import sync_playwright
    from twocaptcha import TwoCaptcha

    solver = TwoCaptcha(settings.twocaptcha_api_key.strip(), defaultTimeout=180)

    bb = Browserbase(api_key=settings.browserbase_api_key.strip())
    session = bb.sessions.create(project_id=settings.browserbase_project_id.strip())
    connect_url = session.connect_url

    logger.info(
        "cambridge_etims_session_started",
        session_id=session.id,
        plate=plate,
        state=st,
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(connect_url)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                page.set_default_timeout(timeout_ms)

                for attempt in range(1, MAX_SUBMIT_ATTEMPTS + 1):
                    logger.info(
                        "cambridge_etims_attempt_start",
                        attempt=attempt,
                        max_attempts=MAX_SUBMIT_ATTEMPTS,
                        session_id=session.id,
                        plate=plate,
                        state=st,
                    )

                    if attempt == 1:
                        page.goto(
                            CAMBRIDGE_INPUT_URL,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                    else:
                        logger.info(
                            "cambridge_etims_attempt_refresh",
                            attempt=attempt,
                            session_id=session.id,
                            reason="search_form_still_visible_after_submit",
                        )
                        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)

                    _wait_after_load_for_captcha(page)
                    _fill_plate_fields(page, plate, st, mm, dd)
                    solution = _solve_captcha_with_2captcha(
                        page,
                        solver,
                        attempt=attempt,
                        session_id=session.id,
                    )
                    _fill_captcha_input_and_submit(
                        page,
                        solution,
                        timeout_ms,
                        attempt=attempt,
                        session_id=session.id,
                    )
                    html = page.content()

                    if not _still_on_search_form(page):
                        logger.info(
                            "cambridge_etims_attempt_done",
                            attempt=attempt,
                            session_id=session.id,
                            outcome="left_search_form",
                            plate=plate,
                        )
                        return _parse_results_html(html)

                    logger.warning(
                        "cambridge_etims_attempt_still_on_search_form",
                        attempt=attempt,
                        max_attempts=MAX_SUBMIT_ATTEMPTS,
                        session_id=session.id,
                        plate=plate,
                        will_retry=attempt < MAX_SUBMIT_ATTEMPTS,
                    )

                    if attempt < MAX_SUBMIT_ATTEMPTS:
                        continue

                    if _security_error_in_html(html):
                        raise CambridgeEtimError(
                            "Security check failed after "
                            f"{MAX_SUBMIT_ATTEMPTS} attempts (invalid CAPTCHA or session)."
                        )
                    raise CambridgeEtimError(
                        f"Still on Cambridge eTIMS search form after {MAX_SUBMIT_ATTEMPTS} attempts "
                        "(CAPTCHA or form validation)."
                    )
            finally:
                browser.close()
    finally:
        logger.info("cambridge_etims_session_finished", session_id=session.id)
