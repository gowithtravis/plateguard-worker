"""
Healthcheck endpoint.
"""
from fastapi import APIRouter


router = APIRouter()


@router.get("/health")
async def healthcheck() -> dict:
    return {"status": "ok"}

