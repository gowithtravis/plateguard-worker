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

# PlateGuard brand (aligned with consumer dashboard)
COLOR_ORANGE = "#FF9A00"
COLOR_NAVY = "#1E2D4D"
COLOR_BG = "#FAFAF8"
COLOR_TEXT = "#1A1A1A"
COLOR_MUTED = "#5C5C5C"
COLOR_CARD_BORDER = "#C8C7C1"

APP_ORIGIN = "https://app.plateguard.io"

# Inter + fallbacks (Google Fonts link in <head> for clients that support it)
FONT_STACK = (
    "'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "Helvetica, Arial, sans-serif"
)

# Inline PlateGuard shield (orange) — matches dashboard mark
LOGO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 48" width="36" height="44" style="display:block;" aria-hidden="true">
  <path d="M20 2L4 9v14c0 9.4 6.8 18.2 16 20.4C29.2 41.2 36 32.4 36 23V9L20 2z" fill="#FF9A00"/>
  <rect x="10" y="17" width="20" height="13" rx="2" fill="#1E2D4D"/>
  <rect x="13" y="20" width="14" height="2.5" rx="1" fill="#FFFFFF" opacity="0.9"/>
  <rect x="14" y="24.5" width="12" height="2" rx="1" fill="#FFFFFF" opacity="0.6"/>
</svg>
""".strip()


class AlertService:
    def __init__(self) -> None:
        if not (settings.supabase_url and settings.supabase_service_key and create_client):
            self._client = None
        else:
            self._client = create_client(
                settings.supabase_url,
                settings.supabase_service_key,  # type: ignore[arg-type]
            )

    def _branded_cta_button(self, href: str, label: str) -> str:
        """Primary CTA: orange fill, dark text, rounded (table-based for email clients)."""
        safe_href = html_module.escape(href, quote=True)
        safe_label = html_module.escape(label)
        return f"""
<table role="presentation" cellspacing="0" cellpadding="0" style="margin:24px 0 0;">
  <tr>
    <td style="border-radius:10px;background-color:{COLOR_ORANGE};">
      <a href="{safe_href}" target="_blank" rel="noopener noreferrer"
         style="display:inline-block;padding:14px 28px;font-family:{FONT_STACK};font-size:14px;font-weight:600;line-height:1.25;color:{COLOR_TEXT};text-decoration:none;border-radius:10px;">
        {safe_label}
      </a>
    </td>
  </tr>
</table>
""".strip()

    def _branded_email_footer(self) -> str:
        safe_unsub = html_module.escape(f"{APP_ORIGIN}/settings", quote=True)
        return f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:28px;">
  <tr>
    <td style="padding:0 8px;text-align:center;font-family:{FONT_STACK};font-size:12px;line-height:1.6;color:{COLOR_MUTED};">
      <p style="margin:0 0 8px;">PlateGuard — Credit monitoring for your license plate</p>
      <p style="margin:0;">
        <a href="{safe_unsub}" style="color:{COLOR_MUTED};text-decoration:underline;">Manage email preferences</a>
        <span style="color:{COLOR_MUTED};"> · </span>
        <a href="#" style="color:{COLOR_MUTED};text-decoration:underline;">Unsubscribe</a>
      </p>
    </td>
  </tr>
</table>
""".strip()

    def _branded_email_header_row(self) -> str:
        """Navy bar with shield SVG + PlateGuard wordmark."""
        return f"""
<tr>
  <td style="background-color:{COLOR_NAVY};padding:20px 24px;">
    <table role="presentation" cellspacing="0" cellpadding="0">
      <tr>
        <td style="vertical-align:middle;padding-right:12px;">{LOGO_SVG}</td>
        <td style="vertical-align:middle;">
          <span style="font-family:{FONT_STACK};font-size:20px;font-weight:600;color:#FFFFFF;letter-spacing:-0.02em;">
            PlateGuard
          </span>
        </td>
      </tr>
    </table>
  </td>
</tr>
""".strip()

    def _build_branded_email_html(self, *, page_title: str, white_inner_html: str) -> str:
        """Full document: #FAFAF8 outer, white card, shared header + footer."""
        safe_title = html_module.escape(page_title)
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="x-ua-compatible" content="ie=edge" />
  <title>{safe_title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
</head>
<body style="margin:0;padding:0;background-color:{COLOR_BG};font-family:{FONT_STACK};">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color:{COLOR_BG};padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
               style="max-width:560px;background-color:#FFFFFF;border-radius:16px;overflow:hidden;
                      box-shadow:0 1px 4px rgba(0,0,0,0.08);border:1px solid rgba(200,199,193,0.4);">
          {self._branded_email_header_row()}
          <tr>
            <td style="padding:28px 24px 32px;font-family:{FONT_STACK};font-size:15px;line-height:1.55;color:{COLOR_TEXT};">
              {white_inner_html}
              {self._branded_email_footer()}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

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
        violations_url = f"{APP_ORIGIN}/violations"

        inner = f"""
<h1 style="margin:0 0 12px;font-size:22px;font-weight:600;line-height:1.3;color:{COLOR_NAVY};font-family:{FONT_STACK};">
  New violation detected
</h1>
<p style="margin:0 0 20px;color:{COLOR_TEXT};font-family:{FONT_STACK};">
  We found a <strong>new</strong> open violation for plate
  <strong style="color:{COLOR_ORANGE};">{safe["plate"]}</strong> via {safe["portal"]}.
</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"
       style="border:1px solid {COLOR_CARD_BORDER};border-radius:12px;overflow:hidden;">
  <tr style="background-color:{COLOR_BG};">
    <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};width:38%;font-family:{FONT_STACK};font-size:14px;">Ticket #</td>
    <td style="padding:12px 16px;color:{COLOR_TEXT};font-family:{FONT_STACK};font-size:14px;">{safe["ticket_number"]}</td>
  </tr>
  <tr>
    <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};border-top:1px solid {COLOR_CARD_BORDER};font-family:{FONT_STACK};font-size:14px;">Amount due</td>
    <td style="padding:12px 16px;color:{COLOR_TEXT};border-top:1px solid {COLOR_CARD_BORDER};font-family:{FONT_STACK};font-size:14px;">{safe["amount_due"]}</td>
  </tr>
  <tr style="background-color:{COLOR_BG};">
    <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};border-top:1px solid {COLOR_CARD_BORDER};font-family:{FONT_STACK};font-size:14px;">Description</td>
    <td style="padding:12px 16px;color:{COLOR_TEXT};border-top:1px solid {COLOR_CARD_BORDER};font-family:{FONT_STACK};font-size:14px;">{safe["description"]}</td>
  </tr>
  <tr>
    <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};border-top:1px solid {COLOR_CARD_BORDER};font-family:{FONT_STACK};font-size:14px;">Location</td>
    <td style="padding:12px 16px;color:{COLOR_TEXT};border-top:1px solid {COLOR_CARD_BORDER};font-family:{FONT_STACK};font-size:14px;">{safe["location"]}</td>
  </tr>
  <tr style="background-color:{COLOR_BG};">
    <td style="padding:12px 16px;font-weight:600;color:{COLOR_NAVY};border-top:1px solid {COLOR_CARD_BORDER};font-family:{FONT_STACK};font-size:14px;">Issue date</td>
    <td style="padding:12px 16px;color:{COLOR_TEXT};border-top:1px solid {COLOR_CARD_BORDER};font-family:{FONT_STACK};font-size:14px;">{safe["issue_date"]}</td>
  </tr>
</table>
<p style="margin:20px 0 0;font-size:14px;color:{COLOR_MUTED};font-family:{FONT_STACK};">
  Log in to review details and take action before late fees apply.
</p>
{self._branded_cta_button(violations_url, "View in Dashboard")}
""".strip()

        return self._build_branded_email_html(
            page_title="New violation — PlateGuard",
            white_inner_html=inner,
        )

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
            source_portal="Boston (RMC Pay)",
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
        """Branded HTML for waitlist / onboard welcome (Resend)."""
        fn_clean = (first_name or "").strip()
        safe_fn = html_module.escape(fn_clean or "there")
        full_clean = (full_name or "").strip()
        safe_full = html_module.escape(full_clean or fn_clean or "there")

        thanks = "Thank you for joining the PlateGuard waitlist."
        if full_clean and full_clean.lower() != fn_clean.lower():
            thanks += f" We have you down as <strong>{safe_full}</strong>."

        if plate_number and str(plate_number).strip():
            safe_plate = html_module.escape(str(plate_number).strip().upper())
            plate_block = (
                f'<p style="margin:0 0 18px;color:{COLOR_TEXT};font-family:{FONT_STACK};">'
                f"When you&apos;re in, we can monitor plate "
                f"<strong style=\"color:{COLOR_ORANGE};\">{safe_plate}</strong> "
                f"(MA) for new violations.</p>"
            )
        else:
            plate_block = (
                f'<p style="margin:0 0 18px;color:{COLOR_TEXT};font-family:{FONT_STACK};">'
                "Once you&apos;re in, you can add license plates from your dashboard anytime.</p>"
            )

        inner = f"""
<h1 style="margin:0 0 16px;font-size:22px;font-weight:600;line-height:1.3;color:{COLOR_NAVY};font-family:{FONT_STACK};">
  Welcome to PlateGuard
</h1>
<p style="margin:0 0 16px;color:{COLOR_TEXT};font-family:{FONT_STACK};">Hi {safe_fn},</p>
<p style="margin:0 0 16px;color:{COLOR_TEXT};font-family:{FONT_STACK};">{thanks}</p>
<p style="margin:0 0 18px;color:{COLOR_TEXT};font-family:{FONT_STACK};">
  <strong>What we do:</strong> PlateGuard monitors official government and toll portals on your behalf
  so parking tickets, tolls, speed-camera, and red-light violations show up in one place—often before
  late fees stack up.
</p>
{plate_block}
<p style="margin:0 0 8px;font-size:14px;color:{COLOR_MUTED};font-family:{FONT_STACK};">
  Open the app to explore your dashboard and notification settings.
</p>
<p style="margin:0;font-size:14px;color:{COLOR_MUTED};font-family:{FONT_STACK};">
  Questions? Reply to this email—we read every message.
</p>
{self._branded_cta_button(APP_ORIGIN, "Open PlateGuard")}
""".strip()

        return self._build_branded_email_html(
            page_title="Welcome to PlateGuard",
            white_inner_html=inner,
        )

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
        subject = "Welcome to PlateGuard"
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
