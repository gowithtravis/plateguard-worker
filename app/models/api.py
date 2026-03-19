"""
API-facing request/response models (currently unused; router defines inline models).
"""
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str

