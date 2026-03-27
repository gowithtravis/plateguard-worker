"""
Public waitlist onboarding: Supabase Auth (admin API) + profiles + welcome email.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional

import structlog

from ..deps.supabase_client import supabase_client

try:
    from gotrue.errors import AuthApiError  # type: ignore
except Exception:  # pragma: no cover
    AuthApiError = Exception  # type: ignore[misc,assignment]


logger = structlog.get_logger()


class OnboardError(Exception):
    """Raised when onboarding cannot complete; carries HTTP status for the API."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class PublicWaitlistResult:
    """Outcome of a public site waitlist signup."""

    user_id: str
    already_registered: bool


class OnboardService:
    """Supabase Auth admin + profiles for plateguard.io waitlist signups."""

    def __init__(self) -> None:
        if not supabase_client:
            raise OnboardError("Supabase is not configured", status_code=503)
        self._client = supabase_client

    def find_auth_user_id_by_email(self, email: str) -> Optional[str]:
        """List Auth users via admin API until a matching email is found."""
        normalized = email.strip().lower()
        page = 1
        per_page = 1000

        while True:
            try:
                users = self._client.auth.admin.list_users(page=page, per_page=per_page)
            except Exception as exc:
                logger.error("supabase_auth_list_users_failed", error=str(exc))
                raise OnboardError("Failed to query Supabase Auth") from exc

            for u in users:
                em = (u.email or "").strip().lower()
                if em == normalized:
                    return str(u.id)

            if len(users) < per_page:
                break
            page += 1

        return None

    def create_auth_user_new(self, email: str, first_name: str, last_name: str) -> tuple[str, bool]:
        """
        Create user with admin create_user. Returns (user_id, created_new).
        If email already exists, returns (existing_id, False).
        """
        email_clean = email.strip().lower()
        fn = (first_name or "").strip()
        ln = (last_name or "").strip()

        attributes = {
            "email": email_clean,
            "password": secrets.token_urlsafe(32),
            "email_confirm": True,
            "user_metadata": {"first_name": fn, "last_name": ln},
        }

        try:
            response = self._client.auth.admin.create_user(attributes)
            uid = response.user.id
            if not uid:
                raise OnboardError("Invalid response from Supabase Auth")
            return str(uid), True
        except OnboardError:
            raise
        except AuthApiError as exc:
            code = getattr(exc, "code", None)
            duplicate_codes = (
                "email_exists",
                "user_already_exists",
                "identity_already_exists",
            )
            if code in duplicate_codes:
                existing = self.find_auth_user_id_by_email(email_clean)
                if existing:
                    logger.info("onboard_auth_user_already_exists", email=email_clean)
                    return existing, False
            logger.warning(
                "supabase_auth_create_user_api_error",
                code=code,
                message=str(exc),
            )
            raise OnboardError("Failed to create Supabase Auth user") from exc
        except Exception as exc:
            logger.exception("supabase_auth_create_user_failed")
            raise OnboardError("Failed to create Supabase Auth user") from exc

    def upsert_profile(
        self,
        user_id: str,
        email: str,
        full_name: Optional[str],
        phone: Optional[str],
        dob_mmdd: Optional[str] = None,
    ) -> None:
        row = {
            "id": user_id,
            "email": email.strip().lower(),
            "full_name": (full_name or "").strip() or None,
            "phone": phone.strip() if phone and phone.strip() else None,
            "dob_mmdd": dob_mmdd.strip() if dob_mmdd and dob_mmdd.strip() else None,
        }
        try:
            self._client.table("profiles").upsert(row, on_conflict="id").execute()
        except Exception as exc:
            logger.exception("profile_upsert_failed", user_id=user_id)
            raise OnboardError("Failed to upsert profile") from exc

    def process_public_waitlist_signup(
        self,
        email: str,
        first_name: Optional[str],
        last_name: Optional[str],
        phone: Optional[str],
        dob_mmdd: Optional[str] = None,
    ) -> PublicWaitlistResult:
        """
        Create or reconcile Auth user + profile for the public waitlist form.
        Does not create plates (website flow has no plate field).
        """
        email_clean = email.strip().lower()
        fn = (first_name or "").strip()
        ln = (last_name or "").strip()
        full_name = f"{fn} {ln}".strip() or None

        existing = self.find_auth_user_id_by_email(email_clean)
        if existing:
            self.upsert_profile(existing, email_clean, full_name, phone, dob_mmdd)
            return PublicWaitlistResult(user_id=existing, already_registered=True)

        user_id, created_new = self.create_auth_user_new(email_clean, fn, ln)
        self.upsert_profile(user_id, email_clean, full_name, phone, dob_mmdd)
        return PublicWaitlistResult(
            user_id=user_id,
            already_registered=not created_new,
        )

    def set_password_for_existing_user(self, email: str, password: str) -> None:
        """
        Set or replace the Auth password for an existing user (e.g. waitlist signup).

        Used when the app signup flow detects the email already exists: the client can call
        this endpoint (service role on the server), then ``signInWithPassword`` and redirect
        to the dashboard—equivalent to ``supabase.auth.updateUser`` with a new password after
        a session exists, but works without a prior client session.
        """
        email_clean = email.strip().lower()
        pw = password or ""
        if len(pw) < 8:
            raise OnboardError("Password must be at least 8 characters", status_code=400)
        if len(pw) > 72:
            raise OnboardError("Password must be 72 characters or fewer", status_code=400)

        uid = self.find_auth_user_id_by_email(email_clean)
        if not uid:
            raise OnboardError("No account found for this email", status_code=404)

        try:
            self._client.auth.admin.update_user_by_id(uid, {"password": pw})
        except OnboardError:
            raise
        except AuthApiError as exc:
            logger.warning(
                "set_password_auth_api_error",
                email=email_clean,
                code=getattr(exc, "code", None),
                message=str(exc),
            )
            raise OnboardError("Could not set password. Try a stronger password.", status_code=400) from exc
        except Exception as exc:
            logger.exception("set_password_failed", email=email_clean)
            raise OnboardError("Could not set password", status_code=502) from exc
