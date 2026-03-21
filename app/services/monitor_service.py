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
from ..portals.rmc_parking import (
    RMC_PAY_PORTALS,
    check_plate_tickets_for_portal,
    default_rmc_portal_labels,
)
from .alert_service import AlertService
from .violation_store import ViolationStore


logger = structlog.get_logger()

# Legacy portal token from older plates rows — expands to all RMC Pay cities.
LEGACY_BOSTON_PORTAL = "boston_parking"


def normalize_plate_portals(portals: Optional[list[str]]) -> list[str]:
    """
    Resolve which RMC Pay portals to query.

    - ``None`` / empty → all configured RMC cities.
    - ``boston_parking`` → all RMC cities (backward compatibility).
    - Otherwise only known RMC labels are kept; if nothing remains, all RMC cities.
    """
    all_rmc = default_rmc_portal_labels()
    if portals is None or len(portals) == 0:
        return list(all_rmc)

    resolved: list[str] = []
    for p in portals:
        if p == LEGACY_BOSTON_PORTAL:
            resolved.extend(all_rmc)
        elif p in RMC_PAY_PORTALS:
            resolved.append(p)

    seen: set[str] = set()
    out: list[str] = []
    for x in resolved:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out if out else list(all_rmc)


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
    ) -> dict:
        """Check one plate across specified (or all applicable) portals."""
        to_check = normalize_plate_portals(portals)

        all_violations: list[Violation] = []
        new_violations: list[Violation] = []
        errors: list[str] = []
        checked: list[str] = []

        for portal_name in to_check:
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

        return {
            "plates_checked": len(plates),
            "total_violations": total_violations,
            "new_violations": total_new,
            "errors": all_errors,
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

