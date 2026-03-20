"""
PlateGuard Worker — FastAPI application.

Endpoints:
- GET  /api/health          Health check
- POST /api/test-alert      Send sample violation alert email (Resend)
- POST /api/check-plate     Check a single plate across all portals
- POST /api/run-batch       Check all active plates (placeholder)
"""
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .routers import health, monitor


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

