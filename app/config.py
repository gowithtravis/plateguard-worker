"""
Application configuration loaded from environment variables.
Uses pydantic-settings for validation and type coercion.
"""
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Supabase
    supabase_url: Optional[str] = None
    supabase_service_key: Optional[str] = None

    # Browserbase
    browserbase_api_key: Optional[str] = ""
    browserbase_project_id: Optional[str] = ""

    # Worker
    worker_api_key: str
    browser_mode: str = "local"  # "local" or "browserbase"
    check_interval_hours: int = 6
    max_concurrent_checks: int = 5
    request_delay_seconds: float = 2.0

    # Alerts
    resend_api_key: Optional[str] = ""
    alert_from_email: str = "alerts@plateguard.io"

    # Environment
    environment: str = "development"
    log_level: str = "INFO"
    port: int = 8000

    class Config:
        env_file = ".env"


settings = Settings()

