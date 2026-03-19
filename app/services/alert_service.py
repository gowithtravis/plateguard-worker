"""
Alert Service — sends notifications when new violations are detected.

Phase 1: Email via Resend (scaffolded, disabled until configured).
"""
from __future__ import annotations

from typing import Optional

import httpx
import structlog

from ..config import settings


logger = structlog.get_logger()


class AlertService:
    async def send_new_violation_alerts(self, violations: list):
        """Send email alerts for newly detected violations (placeholder)."""
        if not settings.resend_api_key:
            logger.warning("alerts_skipped_no_resend_key")
            return

        for violation in violations:
            logger.info(
                "new_violation_alert",
                ticket=getattr(violation, "ticket_number", None),
                plate=getattr(violation, "plate_number", None),
            )

    async def send_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
    ) -> bool:
        """Send an email via Resend API."""
        if not settings.resend_api_key:
            logger.warning("email_not_sent_no_resend_key")
            return False

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.alert_from_email,
                    "to": [to_email],
                    "subject": subject,
                    "html": html_body,
                },
            )

            if response.status_code == 200:
                logger.info("email_sent", to=to_email, subject=subject)
                return True

            logger.error("email_failed", status=response.status_code, body=response.text)
            return False

