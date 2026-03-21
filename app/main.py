"""
PlateGuard Worker — FastAPI application.

Endpoints:
- GET  /api/health                     Health check
- POST /api/test-alert                 Send sample violation alert email (Resend)
- POST /api/onboard                    Public waitlist signup (CORS + rate limit; no Bearer)
- POST /api/check-plate                Check a single plate across all portals
- POST /api/run-batch                  Check all active plates (placeholder)
- POST /api/create-checkout-session    Stripe Checkout (Supabase JWT Bearer)
- POST /api/create-billing-portal-session  Stripe Billing Portal (Supabase JWT Bearer)
- POST /api/stripe-webhook             Stripe webhooks (signature only; no Bearer)
"""
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .routers import billing, health, monitor, onboard


logger = structlog.get_logger()
security = HTTPBearer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hook. Keeps Playwright installation optional."""
    logger.info("plateguard_worker_starting", environment=settings.environment)

    if settings.browser_mode == "local":
        # Best-effort Playwright install; failures shouldn't kill the app in dev.
        try:
            import subprocess

            subprocess.run(["playwright", "install", "chromium"], check=True)
        except Exception as exc:  # pragma: no cover - dev convenience
            logger.warning("playwright_install_failed", error=str(exc))

    yield

    logger.info("plateguard_worker_shutting_down")


app = FastAPI(
    title="PlateGuard Worker",
    description="Violation monitoring service for license plates",
    version="0.1.0",
    lifespan=lifespan,
)

# Public waitlist form on plateguard.io
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://plateguard.io",
        "https://www.plateguard.io",
        "https://app.plateguard.io",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
):
    if credentials.credentials != settings.worker_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


# Routers
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(
    monitor.router,
    prefix="/api",
    tags=["monitor"],
    dependencies=[Depends(verify_api_key)],
)
app.include_router(
    onboard.router,
    prefix="/api",
    tags=["onboard"],
)
app.include_router(
    billing.router,
    prefix="/api",
    tags=["billing"],
)

