"""
GHL waitlist onboarding: Supabase Auth user, profile, optional plate.
"""
from __future__ import annotations

import secrets
from typing import Optional

import httpx
import structlog

from ..config import settings

try:
    from supabase import create_client  # type: ignore
except Exception:  # pragma: no cover
    create_client = None  # type: ignore[assignment]


logger = structlog.get_logger()


class OnboardError(Exception):
    """Raised when onboarding cannot complete; carries HTTP status for the API."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class OnboardService:
    """Create or reconcile Auth user + profile + optional plate for waitlist signups."""

    DEFAULT_PLATE_STATE = "MA"

    def __init__(self) -> None:
        self._url = (settings.supabase_url or "").rstrip("/")
        self._key = settings.supabase_service_key or ""
        if not self._url or not self._key or not create_client:
            raise OnboardError("Supabase is not configured", status_code=503)
        self._client = create_client(self._url, self._key)  # type: ignore[arg-type]

    def _auth_headers(self) -> dict:
        return {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    def find_auth_user_id_by_email(self, email: str) -> Optional[str]:
        """List Auth users (admin) until a matching email is found."""
        normalized = email.strip().lower()
        page = 1
        per_page = 1000

        while True:
            try:
                r = httpx.get(
                    f"{self._url}/auth/v1/admin/users",
                    params={"per_page": per_page, "page": page},
                    headers=self._auth_headers(),
                    timeout=60.0,
                )
            except httpx.RequestException as exc:
                logger.error("supabase_auth_list_users_failed", error=str(exc))
                raise OnboardError("Failed to query Supabase Auth") from exc

            if r.status_code >= 400:
                logger.error(
                    "supabase_auth_list_users_http_error",
                    status=r.status_code,
                    body=r.text[:500],
                )
                raise OnboardError("Failed to query Supabase Auth")

            body = r.json()
            if isinstance(body, list):
                users = body
            else:
                users = body.get("users") or []
            for u in users:
                em = (u.get("email") or "").strip().lower()
                if em == normalized:
                    uid = u.get("id")
                    if uid:
                        return str(uid)

            if len(users) < per_page:
                break
            page += 1

        return None

    def create_auth_user(self, email: str, first_name: str, last_name: str) -> str:
        """Create a confirmed Auth user; on duplicate email, return existing user id."""
        payload = {
            "email": email.strip().lower(),
            "password": secrets.token_urlsafe(32),
            "email_confirm": True,
            "user_metadata": {
                "first_name": first_name.strip(),
                "last_name": last_name.strip(),
            },
        }
        try:
            r = httpx.post(
                f"{self._url}/auth/v1/admin/users",
                headers=self._auth_headers(),
                json=payload,
                timeout=60.0,
            )
        except httpx.RequestException as exc:
            logger.error("supabase_auth_create_user_failed", error=str(exc))
            raise OnboardError("Failed to create Supabase Auth user") from exc

        if r.status_code in (200, 201):
            data = r.json()
            uid = data.get("id")
            if not uid:
                logger.error("supabase_auth_create_missing_id", body=r.text[:500])
                raise OnboardError("Invalid response from Supabase Auth")
            return str(uid)

        # Duplicate or validation: resolve existing user
        if r.status_code in (409, 422, 400):
            existing = self.find_auth_user_id_by_email(email)
            if existing:
                logger.info("onboard_auth_user_already_exists", email=email)
                return existing
            logger.warning(
                "supabase_auth_create_rejected",
                status=r.status_code,
                body=r.text[:500],
            )
            raise OnboardError(
                "Could not create user and no existing user found for this email",
                status_code=502,
            )

        logger.error(
            "supabase_auth_create_unexpected",
            status=r.status_code,
            body=r.text[:500],
        )
        raise OnboardError("Failed to create Supabase Auth user")

    def upsert_profile(
        self,
        user_id: str,
        email: str,
        full_name: str,
        phone: Optional[str],
    ) -> None:
        row = {
            "id": user_id,
            "email": email.strip().lower(),
            "full_name": full_name.strip() or None,
            "phone": phone.strip() if phone and phone.strip() else None,
        }
        try:
            self._client.table("profiles").upsert(row, on_conflict="id").execute()
        except Exception as exc:
            logger.exception("profile_upsert_failed", user_id=user_id)
            raise OnboardError("Failed to upsert profile") from exc

    def ensure_plate(self, user_id: str, plate_number: str) -> None:
        """Insert plate if this user does not already have this plate+state."""
        pn = plate_number.strip().upper()
        if not pn:
            return

        try:
            existing = (
                self._client.table("plates")
                .select("id")
                .eq("user_id", user_id)
                .eq("plate_number", pn)
                .eq("state", self.DEFAULT_PLATE_STATE)
                .limit(1)
                .execute()
            )
            if existing.data:
                logger.info(
                    "onboard_plate_already_exists",
                    user_id=user_id,
                    plate_number=pn,
                )
                return

            self._client.table("plates").insert(
                {
                    "user_id": user_id,
                    "plate_number": pn,
                    "state": self.DEFAULT_PLATE_STATE,
                    "is_active": True,
                }
            ).execute()
        except Exception as exc:
            logger.exception("plate_insert_failed", user_id=user_id)
            raise OnboardError("Failed to create plate") from exc

    def process_waitlist_signup(
        self,
        first_name: str,
        last_name: str,
        email: str,
        phone: Optional[str],
        plate_number: Optional[str],
    ) -> str:
        """
        Idempotent waitlist onboarding: Auth user (create or existing), profile upsert,
        optional plate if new. Returns auth user UUID string.
        """
        email_clean = email.strip().lower()
        fn = first_name.strip()
        ln = last_name.strip()
        full_name = f"{fn} {ln}".strip()

        user_id = self.find_auth_user_id_by_email(email_clean)
        if user_id:
            logger.info("onboard_using_existing_auth_user", email=email_clean)
        else:
            user_id = self.create_auth_user(email_clean, fn, ln)

        self.upsert_profile(user_id, email_clean, full_name, phone)

        if plate_number and plate_number.strip():
            self.ensure_plate(user_id, plate_number)

        return user_id
