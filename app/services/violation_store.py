"""
Violation Store — Supabase CRUD operations for violations and checks.

Currently this is a thin placeholder; wire it up once Supabase is ready.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from ..config import settings
from ..portals.manual_ticket_portals import MANUAL_TICKET_PORTAL_LABELS

try:
    from supabase import create_client  # type: ignore
except Exception:  # pragma: no cover - optional in scaffold
    create_client = None  # type: ignore[assignment]


logger = structlog.get_logger()


class ViolationStore:
    def __init__(self):
        if not (settings.supabase_url and settings.supabase_service_key and create_client):
            logger.warning("supabase_not_configured")
            self.client = None
        else:
            self.client = create_client(settings.supabase_url, settings.supabase_service_key)  # type: ignore[arg-type]

    async def get_active_plates(self) -> list[dict]:
        """Fetch all active plates from Supabase (placeholder)."""
        if not self.client:
            return []
        response = (
            self.client.table("plates")
            .select("id, plate_number, state, portals, user_id")
            .eq("is_active", True)
            .execute()
        )
        return response.data

    def get_profile_dob_mmdd_sync(self, user_id: str) -> Optional[str]:
        """Return ``profiles.dob_mmdd`` (MM/DD) for Cambridge eTIMS plate search."""
        if not self.client:
            return None
        try:
            response = (
                self.client.table("profiles")
                .select("dob_mmdd")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("profile_dob_fetch_failed", user_id=user_id, error=str(exc))
            return None
        if not response.data:
            return None
        raw = response.data[0].get("dob_mmdd")
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None

    def _violation_to_row(self, violation: Any) -> dict[str, Any]:
        """Map a :class:`~app.models.violation.Violation` into Supabase column names."""
        vt = getattr(violation, "violation_type", None)
        st = getattr(violation, "status", None)
        issue = getattr(violation, "issue_date", None)
        due = getattr(violation, "due_date", None)

        def _enum_val(x: Any) -> Optional[str]:
            if x is None:
                return None
            return x.value if hasattr(x, "value") else str(x)

        row: dict[str, Any] = {
            "source_portal": getattr(violation, "source_portal", None),
            "ticket_number": getattr(violation, "ticket_number", None),
            "plate_id": getattr(violation, "plate_id", None),
            "plate_number": getattr(violation, "plate_number", None),
            "state": getattr(violation, "state", None) or "MA",
            "violation_type": _enum_val(vt),
            "amount_due": getattr(violation, "amount_due", None),
            "violation_description": getattr(violation, "violation_description", None),
            "issue_date": issue.isoformat() if issue else None,
            "location": getattr(violation, "location", None),
            "status": _enum_val(st),
            "due_date": due.isoformat() if due else None,
            "late_fee_amount": getattr(violation, "late_fee_amount", None),
            "raw_data": getattr(violation, "raw_data", None) or {},
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
        }
        return {k: v for k, v in row.items() if v is not None}

    async def upsert_violation(self, violation: Any) -> bool:
        """
        Insert or update a violation (matched on ``source_portal`` + ``ticket_number``).

        Returns True if a new row was inserted, False if an existing row was updated.
        """
        if not self.client:
            return False

        source_portal = getattr(violation, "source_portal", None)
        ticket_number = getattr(violation, "ticket_number", None)
        if not source_portal or not ticket_number:
            logger.warning(
                "violation_upsert_skipped_missing_keys",
                source_portal=source_portal,
                ticket_number=ticket_number,
            )
            return False

        existing = (
            self.client.table("violations")
            .select("id")
            .eq("source_portal", source_portal)
            .eq("ticket_number", ticket_number)
            .execute()
        )

        violation_data = self._violation_to_row(violation)
        # Always refresh raw_data + last_checked_at even if other fields were omitted
        violation_data["raw_data"] = getattr(violation, "raw_data", None) or {}
        violation_data["last_checked_at"] = datetime.now(timezone.utc).isoformat()

        if existing.data:
            self.client.table("violations").update(violation_data).eq(
                "id", existing.data[0]["id"]
            ).execute()
            logger.info("violation_updated", ticket_number=ticket_number, portal=source_portal)
            return False

        self.client.table("violations").insert(violation_data).execute()
        logger.info("violation_new", ticket_number=ticket_number, portal=source_portal)
        return True

    def verify_plate_belongs_to_user_sync(self, plate_id: str, user_id: str) -> bool:
        """Return True if ``plates.id`` is owned by ``profiles.id`` / auth user."""
        if not self.client:
            return False
        try:
            response = (
                self.client.table("plates")
                .select("id")
                .eq("id", plate_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "plate_owner_check_failed",
                plate_id=plate_id,
                user_id=user_id,
                error=str(exc),
            )
            return False
        return bool(response.data)

    def get_plate_row_sync(self, plate_id: str) -> Optional[dict[str, Any]]:
        """Fetch a single plate row (for manual ticket reporting)."""
        if not self.client:
            return None
        try:
            response = (
                self.client.table("plates")
                .select("id, plate_number, state, user_id")
                .eq("id", plate_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("plate_fetch_failed", plate_id=plate_id, error=str(exc))
            return None
        if not response.data:
            return None
        return response.data[0]

    def get_manual_portal_violations_sync(self) -> list[dict[str, Any]]:
        """
        Violations stored under ticket-number-only portals (Kelley & Ryan, Somerville CHS).

        Used by the batch monitor to refresh amounts/status for manually reported tickets.
        """
        if not self.client:
            return []
        try:
            response = (
                self.client.table("violations")
                .select(
                    "id, plate_id, plate_number, state, ticket_number, source_portal, amount_due, status, raw_data"
                )
                .in_("source_portal", sorted(MANUAL_TICKET_PORTAL_LABELS))
                .execute()
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("manual_portal_violations_fetch_failed", error=str(exc))
            return []
        return list(response.data or [])

    async def log_check(
        self,
        plate_number: str,
        portal: str,
        status: str,
        violations_found: int = 0,
        new_violations: int = 0,
        error_message: Optional[str] = None,
        duration_ms: Optional[int] = None,
        state: Optional[str] = None,
        plate_id: Optional[str] = None,
    ):
        """Log a monitoring check to the checks table."""
        if not self.client:
            return

        effective_plate_id = plate_id

        # Look up the plate UUID first (checks table expects plate_id UUID).
        if effective_plate_id is None:
            plate_query = (
                self.client.table("plates")
                .select("id")
                .eq("plate_number", plate_number)
            )
            if state:
                plate_query = plate_query.eq("state", state)

            existing_plate = plate_query.limit(1).execute()
            if not existing_plate.data:
                # For now, skip logging if the plate doesn't exist yet.
                logger.info(
                    "skip_check_logging_plate_missing",
                    plate_number=plate_number,
                    state=state,
                    portal=portal,
                    status=status,
                )
                return

            effective_plate_id = existing_plate.data[0]["id"]

        check_data = {
            "plate_id": effective_plate_id,
            "portal": portal,
            "status": status,
            "violations_found": violations_found,
            "new_violations": new_violations,
            "error_message": error_message,
            "duration_ms": duration_ms,
        }

        self.client.table("checks").insert(check_data).execute()

