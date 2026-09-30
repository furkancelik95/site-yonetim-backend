from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["sistem"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


@router.get("/health", summary="Canlılık kontrolü")
async def health() -> HealthResponse:
    """Süreç ayakta mı? Kimlik gerektirmez, hiçbir veri döndürmez."""
    return HealthResponse(status="ok")
