"""
Singleton Supabase client (``service_role``) for server-side database and Auth admin API.

Initialized once at import time. If ``SUPABASE_URL`` / ``SUPABASE_SERVICE_KEY`` are missing,
``supabase_client`` is ``None`` and callers should no-op or return errors as before.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import structlog

from ..config import settings

logger = structlog.get_logger()

if TYPE_CHECKING:
    from supabase import Client  # type: ignore[import-untyped]

try:
    from supabase import create_client  # type: ignore
except Exception:  # pragma: no cover
    create_client = None  # type: ignore[assignment]

supabase_client: Optional["Client"] = None

if settings.supabase_url and settings.supabase_service_key and create_client:
    try:
        supabase_client = create_client(
            settings.supabase_url.rstrip("/"),
            settings.supabase_service_key,  # type: ignore[arg-type]
        )
    except Exception as exc:  # pragma: no cover
        logger.error("supabase_singleton_client_init_failed", error=str(exc))
        supabase_client = None
else:
    logger.warning("supabase_singleton_client_not_configured")
