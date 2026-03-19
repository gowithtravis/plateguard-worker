"""
Browser utilities (Playwright / Browserbase) — scaffold only.
"""
from __future__ import annotations

from typing import Literal, Optional

import structlog

from ..config import settings


logger = structlog.get_logger()


BrowserMode = Literal["local", "browserbase"]


def get_browser_mode() -> BrowserMode:
    mode: BrowserMode = "local" if settings.browser_mode not in ("local", "browserbase") else settings.browser_mode  # type: ignore[assignment]
    return mode


async def get_playwright_browser() -> Optional[object]:
    """
        Placeholder factory for a Playwright browser instance.
        Wire this up once you add the actual Boston portal Playwright scraper.
        """
    logger.info("get_playwright_browser_not_implemented")
    return None

