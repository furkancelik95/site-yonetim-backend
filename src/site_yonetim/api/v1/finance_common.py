"""Finans uçlarının ortak parçaları: izinler, iş kuralı hatası → HTTP, Idempotency-Key."""

from http import HTTPStatus
from typing import Annotated, Any

from fastapi import Depends, Header, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.api.deps import CurrentUserDep, SiteContext, require_permission
from site_yonetim.core.errors import ApiError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.services.idempotency import HEADER, Idempotency, check_key, fingerprint

FinanceRead = Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_READ))]
BudgetManage = Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_BUDGET_MANAGE))]
ChargePost = Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_CHARGE_POST))]


def finance_error(exc: FinanceRuleError) -> ApiError:
    if exc.conflict:
        return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)
    fields = {exc.field: exc.message} if exc.field else None
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, fields)


async def _request_text(request: Request) -> str:
    """İsteğin parmak izi metni. Form isteklerinde ham akış FastAPI'nin ayrıştırmasında tükenir;
    önbelleğe alınmış form alanları (dosyada ad + boyut) kullanılır."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        form = await request.form()
        parts = []
        for key, value in sorted(form.multi_items(), key=lambda item: item[0]):
            if isinstance(value, UploadFile):
                parts.append(f"{key}=file:{value.filename}:{value.size}")
            else:
                parts.append(f"{key}={value}")
        return "&".join(parts)
    return (await request.body()).decode("utf-8", errors="replace")


async def idempotency(
    request: Request,
    current: CurrentUserDep,
    key: Annotated[
        str | None,
        Header(
            alias=HEADER,
            description="Aynı işlemin tekrarında çift kayıt olmasın diye istemcinin ürettiği "
            "anahtar (ör. UUID). 24 saat geçerli.",
        ),
    ] = None,
) -> Idempotency:
    try:
        checked = check_key(key) if key is not None else None
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    body = await _request_text(request)
    return Idempotency(
        current.user.id, checked, fingerprint(request.method, request.url.path, body)
    )


IdempotencyDep = Annotated[Idempotency, Depends(idempotency)]


def replayed(status_code: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=body, headers={"Idempotent-Replayed": "true"}
    )


async def commit_with_key(
    session: AsyncSession, idem: Idempotency, status_code: int, result: BaseModel
) -> None:
    """Sonucu anahtarla birlikte saklar ve commit eder (hepsi ya da hiçbiri)."""
    idem.remember(session, status_code, result.model_dump(mode="json"))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            HTTPStatus.CONFLICT,
            "request_in_progress",
            "Aynı işlem şu anda işleniyor ya da az önce işlendi. Birkaç saniye sonra listeyi "
            "yenileyin; tekrar göndermeyin.",
        ) from exc
