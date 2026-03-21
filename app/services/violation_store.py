"""
Violation Store — Supabase CRUD operations for violations and checks.

Currently this is a thin placeholder; wire it up once Supabase is ready.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from ..config import settings

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

    async def upsert_violation(self, violation: Any) -> bool:
        """
        Insert or update a violation.
        Returns True if this is a NEW violation (placeholder logic).
        """
        if not self.client:
            return False

        existing = (
            self.client.table("violations")
            .select("id")
            .eq("source_portal", getattr(violation, "source_portal", None))
            .eq("ticket_number", getattr(violation, "ticket_number", None))
            .execute()
        )

        violation_data = {
            "raw_data": getattr(violation, "raw_data", {}),
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
        }

        if existing.data:
            self.client.table("violations").update(violation_data).eq(
                "id", existing.data[0]["id"]
            ).execute()
            logger.info("violation_updated")
            return False

        self.client.table("violations").insert(violation_data).execute()
        logger.info("violation_new")
        return True

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

