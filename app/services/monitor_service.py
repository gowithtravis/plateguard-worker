"""
Monitor Service — orchestrates plate checks across portals.

Implements the core flow:
1. Run portal scrapers
2. Wrap results as Violation models
3. Persist via ViolationStore
4. Trigger alerts via AlertService
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog

from ..config import settings
from ..models.violation import Violation, ViolationType, ViolationStatus
from ..portals.cambridge_etims import (
    CAMBRIDGE_PORTAL_LABEL,
    browserbase_configured,
    search_violations_sync,
    twocaptcha_configured,
)
from ..portals.kelley_ryan import (
    KELLEY_RYAN_PORTAL,
    search_parking_ticket as kelley_ryan_search_ticket,
)
from ..portals.manual_ticket_portals import MANUAL_TICKET_PORTAL_LABELS
from ..portals.rmc_parking import (
    RMC_PAY_PORTALS,
    check_plate_tickets_for_portal,
    default_rmc_portal_labels,
)
from ..portals.somerville_chs import (
    SOMERVILLE_CHS_PORTAL,
    search_parking_ticket as somerville_chs_search_ticket,
)
from .alert_service import AlertService
from .violation_store import ViolationStore


logger = structlog.get_logger()

# Legacy portal token from older plates rows — expands to all RMC Pay cities.
LEGACY_BOSTON_PORTAL = "boston_parking"


def normalize_plate_portals(portals: Optional[list[str]]) -> list[str]:
    """
    Resolve which portals to query (RMC Pay cities + Cambridge eTIMS).

    - ``None`` / empty → all RMC cities plus Cambridge (eTIMS).
    - ``boston_parking`` → same full default set (backward compatibility).
    - Otherwise only known labels are kept; if nothing remains, full default set.

    Ticket-number-only portals (``kelley_ryan``, ``somerville_chs``) are supported for
    manual reporting but are **never** included here — they are rechecked separately.
    """
    all_rmc = default_rmc_portal_labels()
    default_all = list(all_rmc) + [CAMBRIDGE_PORTAL_LABEL]
    if portals is None or len(portals) == 0:
        return list(default_all)

    resolved: list[str] = []
    for p in portals:
        if p in MANUAL_TICKET_PORTAL_LABELS:
            continue
        if p == LEGACY_BOSTON_PORTAL:
            resolved.extend(default_all)
        elif p in RMC_PAY_PORTALS or p == CAMBRIDGE_PORTAL_LABEL:
            resolved.append(p)

    seen: set[str] = set()
    out: list[str] = []
    for x in resolved:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out if out else list(default_all)


class MonitorService:
    def __init__(self) -> None:
        self.store = ViolationStore()
        self.alerts = AlertService()

    async def check_single_plate(
        self,
        plate_number: str,
        state: str = "MA",
        portals: Optional[list[str]] = None,
        plate_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Check one plate across specified (or all applicable) portals."""
        to_check = normalize_plate_portals(portals)

        all_violations: list[Violation] = []
        new_violations: list[Violation] = []
        errors: list[str] = []
        checked: list[str] = []

        for portal_name in to_check:
            if portal_name == CAMBRIDGE_PORTAL_LABEL:
                if not browserbase_configured():
                    logger.warning(
                        "cambridge_skipped_browserbase_not_configured",
                        plate_number=plate_number,
                    )
                    continue
                if not twocaptcha_configured():
                    logger.warning(
                        "cambridge_skipped_twocaptcha_not_configured",
                        plate_number=plate_number,
                    )
                    continue
                if not user_id:
                    logger.warning(
                        "cambridge_skipped_no_user_id",
                        plate_number=plate_number,
                    )
                    continue
                dob_mmdd = self.store.get_profile_dob_mmdd_sync(user_id)
                if not dob_mmdd:
                    logger.warning(
                        "cambridge_skipped_no_dob_mmdd",
                        user_id=user_id,
                        plate_number=plate_number,
                    )
                    continue
                try:
                    tickets = await asyncio.to_thread(
                        search_violations_sync,
                        plate_number,
                        state,
                        dob_mmdd,
                    )
                    checked.append(portal_name)
                    portal_new = 0
                    for ticket in tickets:
                        violation = self._from_rmc_ticket(
                            ticket,
                            plate_number,
                            state,
                            plate_id=plate_id,
                            source_portal=portal_name,
                        )
                        all_violations.append(violation)
                        is_new = await self.store.upsert_violation(violation)
                        if is_new:
                            new_violations.append(violation)
                            portal_new += 1
                    await self.store.log_check(
                        plate_number=plate_number,
                        state=state,
                        portal=portal_name,
                        status="success",
                        violations_found=len(tickets),
                        new_violations=portal_new,
                        plate_id=plate_id,
                    )
                    await asyncio.sleep(settings.request_delay_seconds)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("portal_check_failed", portal=portal_name, error=str(exc))
                    errors.append(f"{portal_name}: {exc}")
                    await self.store.log_check(
                        plate_number=plate_number,
                        state=state,
                        portal=portal_name,
                        status="error",
                        error_message=str(exc),
                        plate_id=plate_id,
                    )
                continue

            if portal_name not in RMC_PAY_PORTALS:
                errors.append(f"Unknown portal: {portal_name}")
                continue

            try:
                result = check_plate_tickets_for_portal(portal_name, plate_number, state)
                checked.append(portal_name)

                tickets = result.get("tickets", [])
                portal_new = 0
                for ticket in tickets:
                    violation = self._from_rmc_ticket(
                        ticket,
                        plate_number,
                        state,
                        plate_id=plate_id,
                        source_portal=portal_name,
                    )
                    all_violations.append(violation)

                    is_new = await self.store.upsert_violation(violation)
                    if is_new:
                        new_violations.append(violation)
                        portal_new += 1

                await self.store.log_check(
                    plate_number=plate_number,
                    state=state,
                    portal=portal_name,
                    status="success",
                    violations_found=len(tickets),
                    new_violations=portal_new,
                    plate_id=plate_id,
                )

                await asyncio.sleep(settings.request_delay_seconds)

            except Exception as exc:  # pragma: no cover - defensive
                logger.error("portal_check_failed", portal=portal_name, error=str(exc))
                errors.append(f"{portal_name}: {exc}")
                await self.store.log_check(
                    plate_number=plate_number,
                    state=state,
                    portal=portal_name,
                    status="error",
                    error_message=str(exc),
                    plate_id=plate_id,
                )

        if new_violations:
            await self.alerts.send_new_violation_alerts(new_violations)

        return {
            "plate_number": plate_number,
            "state": state,
            "violations_found": len(all_violations),
            "new_violations": len(new_violations),
            "portals_checked": checked,
            "errors": errors,
        }

    async def submit_manual_ticket_report(
        self,
        *,
        user_id: str,
        plate_id: str,
        ticket_number: str,
        city: str,
        portal_type: str,
    ) -> dict:
        """
        Validate a ticket against Kelley & Ryan or Somerville CHS and persist a violation.

        ``city`` is required for ``kelley_ryan`` (municipality name or numeric town id).
        For ``somerville_chs``, ``city`` is optional but should reference Somerville.
        """
        if portal_type not in (KELLEY_RYAN_PORTAL, SOMERVILLE_CHS_PORTAL):
            raise ValueError(f"Unsupported portal_type: {portal_type!r}")

        if not self.store.verify_plate_belongs_to_user_sync(plate_id, user_id):
            raise PermissionError("Plate does not belong to the given user_id")

        plate_row = self.store.get_plate_row_sync(plate_id)
        if not plate_row:
            raise ValueError("Plate not found")

        plate_number = str(plate_row["plate_number"])
        state = str(plate_row.get("state") or "MA")

        if portal_type == KELLEY_RYAN_PORTAL:
            result = await asyncio.to_thread(
                kelley_ryan_search_ticket,
                city,
                plate_number,
                ticket_number,
            )
            if not result.found or not result.details:
                return {"ok": False, "error": "Ticket not found on Kelley & Ryan portal"}

            merged_raw: dict = {
                **result.details,
                "manual_submission": True,
                "city": city.strip(),
                "portal_type": portal_type,
            }
            if result.raw_html_excerpt:
                merged_raw["result_html_excerpt"] = result.raw_html_excerpt

            violation = self._from_rmc_ticket(
                merged_raw,
                plate_number,
                state,
                plate_id=plate_id,
                source_portal=KELLEY_RYAN_PORTAL,
            )
            violation.status = self._violation_status_from_payload(merged_raw)
            is_new = await self.store.upsert_violation(violation)
            return {
                "ok": True,
                "new_violation": is_new,
                "ticket_number": ticket_number,
                "source_portal": KELLEY_RYAN_PORTAL,
                **self._violation_summary_dict(violation),
            }

        # somerville_chs
        result = await asyncio.to_thread(
            somerville_chs_search_ticket,
            plate_number,
            ticket_number,
        )
        if not result.found or not result.details:
            return {"ok": False, "error": "Ticket not found on Somerville (City Hall Systems) portal"}

        merged_raw = {
            **result.details,
            "manual_submission": True,
            "city": (city or "Somerville").strip(),
            "portal_type": portal_type,
        }
        if result.raw_html_excerpt:
            merged_raw["result_html_excerpt"] = result.raw_html_excerpt
        if result.final_url:
            merged_raw["final_url"] = result.final_url

        violation = self._from_rmc_ticket(
            merged_raw,
            plate_number,
            state,
            plate_id=plate_id,
            source_portal=SOMERVILLE_CHS_PORTAL,
        )
        violation.status = self._violation_status_from_payload(merged_raw)
        is_new = await self.store.upsert_violation(violation)
        return {
            "ok": True,
            "new_violation": is_new,
            "ticket_number": ticket_number,
            "source_portal": SOMERVILLE_CHS_PORTAL,
            **self._violation_summary_dict(violation),
        }

    async def recheck_manual_portal_violations(self) -> dict:
        """
        Refresh violations stored under ticket-number-only portals (amount/status).
        """
        rows = self.store.get_manual_portal_violations_sync()
        errors: list[str] = []
        checked = 0

        for row in rows:
            portal = row.get("source_portal")
            ticket = str(row.get("ticket_number") or "").strip()
            plate = str(row.get("plate_number") or "").strip()
            state = str(row.get("state") or "MA")
            plate_id = str(row["plate_id"]) if row.get("plate_id") else None
            prev_raw = row.get("raw_data") or {}

            if not ticket or not plate or not portal:
                continue

            try:
                if portal == KELLEY_RYAN_PORTAL:
                    city = (prev_raw.get("city") or prev_raw.get("manual_report_city") or "").strip()
                    if not city:
                        errors.append(
                            f"kelley_ryan ticket {ticket}: missing city in violation raw_data; skipping recheck"
                        )
                        continue
                    result = await asyncio.to_thread(
                        kelley_ryan_search_ticket,
                        city,
                        plate,
                        ticket,
                    )
                elif portal == SOMERVILLE_CHS_PORTAL:
                    result = await asyncio.to_thread(
                        somerville_chs_search_ticket,
                        plate,
                        ticket,
                    )
                else:
                    continue

                if not result.found or not getattr(result, "details", None):
                    errors.append(
                        f"{portal} ticket {ticket}: not found on portal during recheck (may be paid or removed)"
                    )
                    continue

                checked += 1

                merged_raw = {**prev_raw, **result.details, "manual_submission": True}
                if getattr(result, "raw_html_excerpt", None):
                    merged_raw["result_html_excerpt"] = result.raw_html_excerpt
                if portal == SOMERVILLE_CHS_PORTAL and getattr(result, "final_url", None):
                    merged_raw["final_url"] = result.final_url

                violation = self._from_rmc_ticket(
                    merged_raw,
                    plate,
                    state,
                    plate_id=plate_id,
                    source_portal=portal,
                )
                violation.status = self._violation_status_from_payload(merged_raw)
                await self.store.upsert_violation(violation)

                await self.store.log_check(
                    plate_number=plate,
                    state=state,
                    portal=portal,
                    status="success",
                    violations_found=1,
                    new_violations=0,
                    plate_id=plate_id,
                )
                await asyncio.sleep(settings.request_delay_seconds)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("manual_portal_recheck_failed", portal=portal, ticket=ticket, error=str(exc))
                errors.append(f"{portal} ticket {ticket}: {exc}")
                await self.store.log_check(
                    plate_number=plate,
                    state=state,
                    portal=portal,
                    status="error",
                    error_message=str(exc),
                    plate_id=plate_id,
                )

        return {"manual_ticket_rechecks": checked, "manual_ticket_recheck_errors": errors}

    async def check_all_active_plates(self) -> dict:
        """
        Check all active plates in the database.
        Uses semaphore to limit concurrent checks.
        """
        plates = await self.store.get_active_plates()
        logger.info("batch_check_starting", plate_count=len(plates))

        semaphore = asyncio.Semaphore(settings.max_concurrent_checks)

        async def check_with_semaphore(plate: dict):
            async with semaphore:
                return await self.check_single_plate(
                    plate_number=plate["plate_number"],
                    state=plate.get("state", "MA"),
                    portals=plate.get("portals"),
                    plate_id=str(plate["id"]) if plate.get("id") is not None else None,
                    user_id=str(plate["user_id"]) if plate.get("user_id") else None,
                )

        tasks = [check_with_semaphore(plate) for plate in plates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_violations = 0
        total_new = 0
        all_errors: list[str] = []

        for result in results:
            if isinstance(result, Exception):
                all_errors.append(str(result))
            else:
                total_violations += result["violations_found"]
                total_new += result["new_violations"]
                all_errors.extend(result["errors"])

        manual = await self.recheck_manual_portal_violations()
        all_errors.extend(manual.get("manual_ticket_recheck_errors", []))

        return {
            "plates_checked": len(plates),
            "total_violations": total_violations,
            "new_violations": total_new,
            "errors": all_errors,
            "manual_ticket_rechecks": manual.get("manual_ticket_rechecks", 0),
            "manual_ticket_recheck_errors": manual.get("manual_ticket_recheck_errors", []),
        }

    @staticmethod
    def _violation_summary_dict(violation: Violation) -> dict:
        st = violation.status
        status_str = st.value if isinstance(st, ViolationStatus) else str(st)
        return {
            "amount_due": violation.amount_due,
            "status": status_str,
            "violation_description": violation.violation_description,
            "location": violation.location,
            "due_date": violation.due_date.isoformat() if violation.due_date else None,
        }

    def _from_rmc_ticket(
        self,
        ticket: dict,
        plate_number: str,
        state: str,
        *,
        plate_id: Optional[str] = None,
        source_portal: str,
    ) -> Violation:
        """
        Map a raw RMC Pay violation dict into a Violation model.

        Fills structured fields when RMCPay-style keys exist; full payload stays in raw_data.
        """
        ticket_number = str(ticket.get("violation_number") or ticket.get("violation_id") or "")

        amount_due = self._coerce_float(
            ticket.get("amount_due")
            or ticket.get("fine_amount")
            or ticket.get("balance")
            or ticket.get("amount")
            or ticket.get("violation_amount")
            or ticket.get("total_amount")
        )

        violation_description = self._first_non_empty_str(
            ticket,
            "violation_description",
            "description",
            "violation_desc",
            "comments",
            "comment",
        )

        location = self._first_non_empty_str(
            ticket,
            "location",
            "violation_location",
            "address",
            "street",
            "block",
        )

        issue_date = self._parse_issue_date(
            ticket.get("issue_date")
            or ticket.get("violation_date")
            or ticket.get("date_issued")
            or ticket.get("issued_date")
            or ticket.get("datetime")
            or ticket.get("violation_datetime")
        )

        return Violation(
            violation_type=ViolationType.parking,
            source_portal=source_portal,
            ticket_number=ticket_number,
            plate_number=plate_number,
            state=state,
            plate_id=plate_id,
            amount_due=amount_due,
            violation_description=violation_description,
            location=location,
            issue_date=issue_date,
            status=ViolationStatus.open,
            raw_data=ticket,
        )

    def _violation_status_from_payload(self, payload: dict) -> ViolationStatus:
        """Infer paid/open from scraper text fields."""
        text_bits: list[str] = []
        st = payload.get("status_text")
        if st:
            text_bits.append(str(st))
        for k, v in (payload.get("kv_pairs") or {}).items():
            if "status" in str(k).lower():
                text_bits.append(str(v))
        blob = " ".join(text_bits).lower()
        if "paid" in blob or "satisfied" in blob or "closed" in blob:
            return ViolationStatus.paid
        amount = self._coerce_float(
            payload.get("amount_due")
            or payload.get("balance")
            or payload.get("fine_amount")
        )
        if amount is not None and amount <= 0:
            return ViolationStatus.paid
        if "past due" in blob or "delinquent" in blob:
            return ViolationStatus.past_due
        return ViolationStatus.open

    @staticmethod
    def _coerce_float(value: object) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _first_non_empty_str(data: dict, *keys: str) -> Optional[str]:
        for k in keys:
            v = data.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    @staticmethod
    def _parse_issue_date(value: object) -> Optional[datetime]:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        s = str(value).strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
        for fmt in (
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%m-%d-%Y",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d")
            except ValueError:
                pass
        return None

