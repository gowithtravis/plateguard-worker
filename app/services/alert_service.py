"""
Alert Service — sends notifications when new violations are detected.

Uses Resend for email. Looks up recipient via Supabase: plate → user_id → profiles.email.
"""
from __future__ import annotations

import asyncio
import html as html_module
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
import structlog

from ..config import settings
from ..models.violation import Violation, ViolationStatus, ViolationType

try:
    from supabase import create_client  # type: ignore
except Exception:  # pragma: no cover
    create_client = None  # type: ignore[assignment]


logger = structlog.get_logger()

# PlateGuard brand
COLOR_ORANGE = "#FF9A00"
COLOR_NAVY = "#1E2D4D"
COLOR_BG = "#FAFAF8"


class AlertService:
    def __init__(self) -> None:
        if not (settings.supabase_url and settings.supabase_service_key and create_client):
            self._client = None
        else:
            self._client = create_client(
                settings.supabase_url,
                settings.supabase_service_key,  # type: ignore[arg-type]
            )

    def _lookup_user_email_sync(self, violation: Violation) -> Optional[str]:
        """Resolve profile email for a violation's plate (sync; run in thread)."""
        if not self._client:
            logger.warning("alert_email_skipped_no_supabase")
            return None

        plate_row: Optional[Dict[str, Any]] = None

        if violation.plate_id:
            resp = (
                self._client.table("plates")
                .select("user_id")
                .eq("id", violation.plate_id)
                .limit(1)
                .execute()
            )
            if resp.data:
                plate_row = resp.data[0]
        else:
            plate = violation.plate_number.strip().upper()
            state = violation.state.strip().upper()
            resp = (
                self._client.table("plates")
                .select("user_id")
                .eq("plate_number", plate)
                .eq("state", state)
                .limit(1)
                .execute()
            )
            if resp.data:
                plate_row = resp.data[0]

        if not plate_row or not plate_row.get("user_id"):
            logger.info(
                "alert_email_skipped_plate_not_found",
                plate_number=violation.plate_number,
                state=violation.state,
                plate_id=violation.plate_id,
            )
            return None

        user_id = plate_row["user_id"]
        prof = (
            self._client.table("profiles")
            .select("email")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        if not prof.data or not prof.data[0].get("email"):
            logger.warning(
                "alert_email_skipped_no_profile_email",
                user_id=str(user_id),
            )
            return None

        return str(prof.data[0]["email"]).strip() or None

    def _violation_display_fields(self, violation: Violation) -> Dict[str, str]:
        """Collect display strings for email (model fields + raw_data fallbacks)."""
        raw: Dict[str, Any] = {}
        if isinstance(violation.raw_data, dict):
            raw = violation.raw_data

        def first_str(*keys: str) -> str:
            for k in keys:
                v = raw.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
            return ""

        ticket = violation.ticket_number or first_str(
            "violation_number",
            "violation_id",
            "ticket_number",
        )

        amount: Optional[float] = violation.amount_due
        if amount is None:
            for key in (
                "amount_due",
                "fine_amount",
                "balance",
                "amount",
                "violation_amount",
                "total_amount",
            ):
                v = raw.get(key)
                if v is not None:
                    try:
                        amount = float(v)
                        break
                    except (TypeError, ValueError):
                        continue

        description = violation.violation_description or first_str(
            "violation_description",
            "description",
            "violation_desc",
            "comments",
            "comment",
        )

        location = violation.location or first_str(
            "location",
            "violation_location",
            "address",
            "street",
            "block",
        )

        issue_date_str = ""
        if violation.issue_date:
            issue_date_str = violation.issue_date.strftime("%B %d, %Y")
        else:
            for key in (
                "issue_date",
                "violation_date",
                "date_issued",
                "issued_date",
                "datetime",
                "violation_datetime",
            ):
                v = raw.get(key)
                if v is not None and str(v).strip():
                    issue_date_str = str(v).strip()
                    break

        amount_str = f"${amount:,.2f}" if amount is not None else "—"

        return {
            "ticket_number": ticket or "—",
            "amount_due": amount_str,
            "description": description or "—",
            "location": location or "—",
            "issue_date": issue_date_str or "—",
            "plate": f"{violation.plate_number} ({violation.state})",
            "portal": violation.source_portal.replace("_", " ").title(),
        }

    def _build_new_violation_html(self, violation: Violation) -> str:
        f = self._violation_display_fields(violation)
        safe = {k: html_module.escape(v) for k, v in f.items()}

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>New violation — PlateGuard</title>
</head>
<body style="margin:0;padding:0;background-color:{COLOR_BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color:{COLOR_BG};padding:24px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width:560px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(30,45,77,0.08);">
          <tr>
            <td style="background-color:{COLOR_NAVY};padding:20px 24px;">
              <p style="margin:0;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;color:{COLOR_ORANGE};font-weight:600;">PlateGuard</p>
              <h1 style="margin:8px 0 0;font-size:22px;line-height:1.25;color:#ffffff;font-weight:700;">New violation detected</h1>
            </td>
          </tr>
          <tr>
            <td style="padding:24px;color:{COLOR_NAVY};font-size:15px;line-height:1.5;">
              <p style="margin:0 0 16px;">We found a <strong>new</strong> open violation for plate <strong style="color:{COLOR_ORANGE};">{safe["plate"]}</strong> via {safe["portal"]}.</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #e8e6e0;border-radius:8px;overflow:hidden;">
                <tr style="background:{COLOR_BG};">
                  <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};width:38%;">Ticket #</td>
                  <td style="padding:12px 16px;color:{COLOR_NAVY};">{safe["ticket_number"]}</td>
                </tr>
                <tr>
                  <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};border-top:1px solid #e8e6e0;">Amount due</td>
                  <td style="padding:12px 16px;color:{COLOR_NAVY};border-top:1px solid #e8e6e0;">{safe["amount_due"]}</td>
                </tr>
                <tr style="background:{COLOR_BG};">
                  <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};border-top:1px solid #e8e6e0;">Description</td>
                  <td style="padding:12px 16px;color:{COLOR_NAVY};border-top:1px solid #e8e6e0;">{safe["description"]}</td>
                </tr>
                <tr>
                  <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};border-top:1px solid #e8e6e0;">Location</td>
                  <td style="padding:12px 16px;color:{COLOR_NAVY};border-top:1px solid #e8e6e0;">{safe["location"]}</td>
                </tr>
                <tr style="background:{COLOR_BG};">
                  <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};border-top:1px solid #e8e6e0;">Issue date</td>
                  <td style="padding:12px 16px;color:{COLOR_NAVY};border-top:1px solid #e8e6e0;">{safe["issue_date"]}</td>
                </tr>
              </table>
              <p style="margin:20px 0 0;font-size:13px;color:#5c6b7f;">Log in to PlateGuard to review and take action before late fees apply.</p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 24px 24px;text-align:center;">
              <p style="margin:0;font-size:12px;color:#8a94a6;">Sent by PlateGuard · <span style="color:{COLOR_ORANGE};">Stay ahead of tickets &amp; tolls</span></p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    async def send_sample_alert_email(self, to_email: str) -> bool:
        """
        Send a test email to the given address using the same HTML template
        and Resend path as production new-violation alerts (no Supabase lookup).
        """
        if not settings.resend_api_key:
            logger.warning("test_alert_skipped_no_resend_key")
            return False

        sample = Violation(
            violation_type=ViolationType.parking,
            source_portal="boston_parking",
            ticket_number="TEST-12345",
            plate_number="SAMPLE",
            state="MA",
            amount_due=75.00,
            violation_description=(
                "Sample: No parking during street cleaning (test alert — not a real ticket)"
            ),
            location="123 Example St, Boston, MA",
            issue_date=datetime(2026, 3, 1, 12, 0, 0),
            status=ViolationStatus.open,
            raw_data={},
        )
        html_body = self._build_new_violation_html(sample)
        subject = "PlateGuard: Test alert — sample violation email"
        return await self.send_email(to_email.strip(), subject, html_body)

    def _build_waitlist_welcome_html(
        self,
        first_name: str,
        full_name: str,
        plate_number: Optional[str],
    ) -> str:
        """Branded HTML for GHL waitlist / onboard confirmation (Resend)."""
        fn_clean = (first_name or "").strip()
        safe_fn = html_module.escape(fn_clean or "there")
        full_clean = (full_name or "").strip()
        safe_full = html_module.escape(full_clean or fn_clean or "there")
        thanks = "Thanks for joining the PlateGuard waitlist."
        if full_clean and full_clean.lower() != fn_clean.lower():
            thanks += f" We have you down as <strong>{safe_full}</strong>."
        if plate_number and str(plate_number).strip():
            safe_plate = html_module.escape(str(plate_number).strip().upper())
            plate_block = (
                f'<p style="margin:0 0 16px;color:{COLOR_NAVY};">'
                f"We&apos;ll monitor plate <strong style=\"color:{COLOR_ORANGE};\">{safe_plate}</strong> "
                f"(MA) for you when PlateGuard goes live.</p>"
            )
        else:
            plate_block = (
                f'<p style="margin:0 0 16px;color:{COLOR_NAVY};">'
                "You can add a license plate from your account anytime after launch.</p>"
            )

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>You&apos;re on the PlateGuard waitlist</title>
</head>
<body style="margin:0;padding:0;background-color:{COLOR_BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color:{COLOR_BG};padding:24px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width:560px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(30,45,77,0.08);">
          <tr>
            <td style="background-color:{COLOR_NAVY};padding:20px 24px;">
              <p style="margin:0;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;color:{COLOR_ORANGE};font-weight:600;">PlateGuard</p>
              <h1 style="margin:8px 0 0;font-size:22px;line-height:1.25;color:#ffffff;font-weight:700;">You&apos;re on the list</h1>
            </td>
          </tr>
          <tr>
            <td style="padding:24px;color:{COLOR_NAVY};font-size:15px;line-height:1.55;">
              <p style="margin:0 0 16px;">Hi {safe_fn},</p>
              <p style="margin:0 0 16px;">{thanks} We&apos;ll email you when your spot opens and monitoring is ready.</p>
              {plate_block}
              <p style="margin:0;font-size:13px;color:#5c6b7f;">Questions? Just reply to this email.</p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 24px 24px;text-align:center;">
              <p style="margin:0;font-size:12px;color:#8a94a6;">PlateGuard · <span style="color:{COLOR_ORANGE};">Tickets &amp; tolls, before late fees</span></p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    async def send_waitlist_welcome_email(
        self,
        to_email: str,
        first_name: str,
        last_name: str,
        plate_number: Optional[str],
    ) -> bool:
        """
        Waitlist confirmation email (same Resend integration as other alerts).
        """
        if not settings.resend_api_key:
            logger.warning("waitlist_welcome_skipped_no_resend_key")
            return False

        fn = (first_name or "").strip()
        ln = (last_name or "").strip()
        full_name = f"{fn} {ln}".strip()
        subject = "You're on the PlateGuard waitlist"
        html_body = self._build_waitlist_welcome_html(fn, full_name, plate_number)
        return await self.send_email(to_email.strip().lower(), subject, html_body)

    async def send_new_violation_alerts(self, violations: List[Violation]) -> None:
        """Send one Resend email per new violation after resolving user via Supabase."""
        if not settings.resend_api_key:
            logger.warning("alerts_skipped_no_resend_key")
            return

        for violation in violations:
            to_email = await asyncio.to_thread(self._lookup_user_email_sync, violation)
            if not to_email:
                continue

            fields = self._violation_display_fields(violation)
            subject = (
                f"PlateGuard: New violation — ticket {fields['ticket_number']} "
                f"({violation.plate_number})"
            )
            html_body = self._build_new_violation_html(violation)
            ok = await self.send_email(to_email, subject, html_body)
            if not ok:
                logger.error(
                    "new_violation_alert_send_failed",
                    ticket=violation.ticket_number,
                    to=to_email,
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

        from_addr = settings.alert_from_email
        payload = {
            "from": from_addr,
            "to": [to_email],
            "subject": subject,
            "html": html_body,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )

        if 200 <= response.status_code < 300:
            logger.info("email_sent", to=to_email, subject=subject)
            return True

        logger.error("email_failed", status=response.status_code, body=response.text)
        return False
