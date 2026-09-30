import logging
from http import HTTPStatus
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from site_yonetim.core.errors import ApiError

router = APIRouter(tags=["sistem"])
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadyResponse(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]


@router.get("/health", summary="Canlılık kontrolü")
async def health() -> HealthResponse:
    """Süreç ayakta mı? Kimlik gerektirmez, hiçbir veri döndürmez."""
    return HealthResponse(status="ok")


@router.get(
    "/health/ready",
    summary="Hazırlık kontrolü",
    responses={503: {"description": "Veritabanına ulaşılamıyor"}},
)
async def ready(request: Request) -> ReadyResponse:
    """Uygulama istek almaya hazır mı? Veritabanına bağlanıp `SELECT 1` çalıştırır."""
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        raise ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_not_configured",
            "Veritabanı yapılandırılmamış.",
        )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Hazırlık kontrolü: veritabanına ulaşılamadı")
        raise ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_unavailable",
            "Veritabanına şu an ulaşılamıyor. Lütfen biraz sonra tekrar deneyin.",
        ) from None
    return ReadyResponse(status="ok", database="ok")
