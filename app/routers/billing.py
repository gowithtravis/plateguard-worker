"""
Stripe Checkout, Billing Portal, and webhooks for PlateGuard subscriptions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr

import stripe

from ..config import settings
from ..deps.supabase_client import supabase_client
from ..deps.supabase_jwt import verify_supabase_jwt
from ..limiter import get_authed_rate_limit_key, limiter

logger = structlog.get_logger()
router = APIRouter()

SUCCESS_URL = "https://app.plateguard.io/dashboard?upgraded=true"
CANCEL_URL = "https://app.plateguard.io/upgrade"


class CreateCheckoutSessionRequest(BaseModel):
    user_id: str
    email: EmailStr
    price_id: str


class CreateCheckoutSessionResponse(BaseModel):
    url: str


class CreateBillingPortalRequest(BaseModel):
    user_id: str


class CreateBillingPortalResponse(BaseModel):
    url: str


def _require_stripe_configured() -> None:
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured (STRIPE_SECRET_KEY)",
        )


def _supabase_admin():
    if not supabase_client:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured",
        )
    return supabase_client


@router.post(
    "/create-checkout-session",
    response_model=CreateCheckoutSessionResponse,
)
@limiter.limit(
    "20/minute",
    key_func=get_authed_rate_limit_key,
    error_message=(
        "Too many checkout session requests for your account. Please try again in about a minute."
    ),
)
async def create_checkout_session(
    request: Request,
    body: CreateCheckoutSessionRequest,
    auth_user_id: str = Depends(verify_supabase_jwt),
):
    """
    Create a Stripe Checkout Session (subscription mode). Requires a valid
    Supabase access token; `user_id` in the body must match the token subject.
    """
    if body.user_id != auth_user_id:
        raise HTTPException(
            status_code=403,
            detail="user_id does not match authenticated user",
        )
    _require_stripe_configured()
    stripe.api_key = settings.stripe_secret_key

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": body.price_id, "quantity": 1}],
            customer_email=str(body.email),
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
            metadata={"user_id": body.user_id},
            subscription_data={"metadata": {"user_id": body.user_id}},
        )
    except stripe.StripeError as exc:
        logger.error("stripe_checkout_session_failed", error=str(exc))
        msg = getattr(exc, "user_message", None) or str(exc)
        raise HTTPException(status_code=502, detail=f"Stripe error: {msg}") from exc

    url = session.get("url") if isinstance(session, dict) else getattr(session, "url", None)
    if not url:
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")

    return CreateCheckoutSessionResponse(url=url)


@router.post(
    "/create-billing-portal-session",
    response_model=CreateBillingPortalResponse,
)
@limiter.limit(
    "20/minute",
    key_func=get_authed_rate_limit_key,
    error_message=(
        "Too many billing portal requests for your account. Please try again in about a minute."
    ),
)
async def create_billing_portal_session(
    request: Request,
    body: CreateBillingPortalRequest,
    auth_user_id: str = Depends(verify_supabase_jwt),
):
    """
    Open Stripe Customer Billing Portal. Looks up `stripe_customer_id` from profiles.
    """
    if body.user_id != auth_user_id:
        raise HTTPException(
            status_code=403,
            detail="user_id does not match authenticated user",
        )
    _require_stripe_configured()
    stripe.api_key = settings.stripe_secret_key

    def fetch_customer_id() -> Optional[str]:
        sb = _supabase_admin()
        res = (
            sb.table("profiles")
            .select("stripe_customer_id")
            .eq("id", body.user_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return None
        row = rows[0]
        if isinstance(row, dict):
            cid = row.get("stripe_customer_id")
        else:
            cid = getattr(row, "stripe_customer_id", None)
        return str(cid) if cid else None

    customer_id = await asyncio.to_thread(fetch_customer_id)
    if not customer_id:
        raise HTTPException(
            status_code=400,
            detail="No Stripe customer on file for this account",
        )

    return_url = "https://app.plateguard.io/settings"
    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
    except stripe.StripeError as exc:
        logger.error("stripe_billing_portal_failed", error=str(exc))
        msg = getattr(exc, "user_message", None) or str(exc)
        raise HTTPException(status_code=502, detail=f"Stripe error: {msg}") from exc

    url = portal.get("url") if isinstance(portal, dict) else getattr(portal, "url", None)
    if not url:
        raise HTTPException(status_code=502, detail="Stripe did not return a portal URL")

    return CreateBillingPortalResponse(url=url)


@router.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """
    Stripe webhook (no Bearer auth). Handles checkout.session.completed to
    set plan=annual and stripe_customer_id on the profile.
    """
    if not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="Stripe webhook secret is not configured (STRIPE_WEBHOOK_SECRET)",
        )
    _require_stripe_configured()
    stripe.api_key = settings.stripe_secret_key

    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not sig:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig,
            settings.stripe_webhook_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc
    except stripe.error.SignatureVerificationError as exc:  # type: ignore[attr-defined]
        raise HTTPException(status_code=400, detail="Invalid signature") from exc

    if event["type"] != "checkout.session.completed":
        return {"received": True, "handled": False}

    session_obj = event["data"]["object"]
    session_id, customer_id, user_id = _parse_checkout_session_for_webhook(session_obj)

    if not user_id:
        logger.warning(
            "stripe_webhook_checkout_no_user_id",
            session_id=session_id,
        )
        return {"received": True, "handled": False, "reason": "no user_id in session"}

    if not customer_id:
        logger.warning(
            "stripe_webhook_checkout_no_customer",
            session_id=session_id,
            user_id=user_id,
        )
        return {"received": True, "handled": False, "reason": "no customer on session"}

    def update_profile() -> None:
        sb = _supabase_admin()
        now = datetime.now(timezone.utc).isoformat()
        sb.table("profiles").update(
            {
                "plan": "annual",
                "stripe_customer_id": customer_id,
                "updated_at": now,
            }
        ).eq("id", user_id).execute()

    try:
        await asyncio.to_thread(update_profile)
    except Exception as exc:
        logger.exception(
            "stripe_webhook_profile_update_failed",
            user_id=user_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to update profile in Supabase",
        ) from exc

    logger.info(
        "stripe_webhook_profile_updated",
        user_id=user_id,
        customer_id=customer_id,
    )
    return {"received": True, "handled": True}


def _parse_checkout_session_for_webhook(
    session_obj: Any,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Return (session_id, customer_id, user_id from metadata).
    Accepts dict or StripeObject from construct_event.
    """
    if isinstance(session_obj, dict):
        sid = session_obj.get("id")
        cust = session_obj.get("customer")
        meta = session_obj.get("metadata") or {}
        uid = meta.get("user_id") if isinstance(meta, dict) else None
        return (
            str(sid) if sid else None,
            str(cust) if cust else None,
            str(uid) if uid else None,
        )

    sid = getattr(session_obj, "id", None)
    cust = getattr(session_obj, "customer", None)
    meta = getattr(session_obj, "metadata", None) or {}
    uid = None
    if isinstance(meta, dict):
        uid = meta.get("user_id")
    elif hasattr(meta, "get"):
        uid = meta.get("user_id")
    return (
        str(sid) if sid else None,
        str(cust) if cust else None,
        str(uid) if uid else None,
    )
