"""Operasyon ekleri ve teknik varlık uçlarının ortak parçaları (hata eşleme, izin)."""

from collections.abc import Awaitable, Callable
from http import HTTPStatus

from site_yonetim.api.deps import SiteContext, SiteContextDep
from site_yonetim.core.errors import ApiError, ForbiddenError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.facility import OpsError
from site_yonetim.domain.members import FORM_ERROR, FormFieldError


def ops_error(exc: OpsError) -> ApiError:
    return ApiError(HTTPStatus(exc.status), exc.code, exc.message, exc.fields)


def fields_error(exc: FormFieldError) -> ApiError:
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, exc.fields)


def any_permission(
    *permissions: Permission,
) -> Callable[[SiteContext], Awaitable[SiteContext]]:
    """İzinlerden biri yeterli (ör. anahtar: talep atama ya da güvenlik masası)."""

    async def dependency(ctx: SiteContextDep) -> SiteContext:
        if not any(ctx.access.can(p) for p in permissions):
            raise ForbiddenError
        return ctx

    return dependency
