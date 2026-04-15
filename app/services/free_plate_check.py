"""
Anonymous RMC Pay-only plate checks (no Supabase, no Cambridge / EZDriveMA).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import structlog

from ..config import settings
from ..portals.rmc_parking import check_plate_tickets_for_portal, default_rmc_portal_labels
from .monitor_service import MonitorService

logger = structlog.get_logger()


def _portal_display_name(portal_label: str) -> str:
    """Short city name for API ``city`` field (strip RMC Pay suffix)."""
    return portal_label.replace(" (RMC Pay)", "").strip() or portal_label


def _status_from_raw(raw: Dict[str, Any]) -> str:
    for k in ("status", "violation_status", "payment_status", "balance_status"):
        v = raw.get(k)
        if v is not None and str(v).strip():
            return str(v).strip().lower()
    return "open"


def _issue_date_str(raw: Dict[str, Any]) -> Optional[str]:
    dt = MonitorService._parse_issue_date(
        raw.get("issue_date")
        or raw.get("violation_date")
        or raw.get("date_issued")
        or raw.get("issued_date")
        or raw.get("datetime")
        or raw.get("violation_datetime")
    )
    if dt:
        return dt.isoformat()
    return None


def check_plate_free_rmc_sync(plate_number: str, state: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Query all RMC Pay portals for the plate. Does not persist.

    Returns ``(violations, portals_checked)`` where each violation dict has only
    ``city``, ``amount``, ``date``, and ``status`` (sanitized for the public API).
    """
    plate = plate_number.strip().upper()
    st = (state or "MA").strip().upper()
    portals_checked: List[str] = []
    violations: List[Dict[str, Any]] = []

    sess = requests.Session()
    delay = float(getattr(settings, "request_delay_seconds", 0) or 0)

    for portal_label in default_rmc_portal_labels():
        portals_checked.append(_portal_display_name(portal_label))
        city = _portal_display_name(portal_label)
        try:
            result = check_plate_tickets_for_portal(
                portal_label,
                plate,
                st,
                session=sess,
                timeout=20.0,
            )
            for t in result.get("tickets") or []:
                raw = t.get("raw") if isinstance(t, dict) else {}
                if not isinstance(raw, dict):
                    raw = {}
                ticket_number = str(
                    t.get("violation_number")
                    or t.get("violation_id")
                    or raw.get("violation_number")
                    or raw.get("violation_id")
                    or ""
                ).strip()
                if not ticket_number:
                    continue
                amount = MonitorService._coerce_float(
                    raw.get("amount_due")
                    or raw.get("fine_amount")
                    or raw.get("balance")
                    or raw.get("amount")
                    or raw.get("violation_amount")
                    or raw.get("total_amount")
                )
                violations.append(
                    {
                        "city": city,
                        "amount": amount,
                        "date": _issue_date_str(raw),
                        "status": _status_from_raw(raw),
                    }
                )
        except Exception as exc:  # pragma: no cover - per-portal resilience
            plate_prefix = plate[:3] if len(plate) >= 3 else plate
            logger.warning(
                "free_plate_check_portal_failed",
                portal=portal_label,
                plate_prefix=plate_prefix,
                error=str(exc),
            )
        if delay > 0:
            time.sleep(delay)

    return violations, portals_checked
