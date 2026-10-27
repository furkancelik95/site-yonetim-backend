"""Finans Mod P uçlarının (servis istekleri 27–34) ortak parçaları."""

from http import HTTPStatus

from site_yonetim.api.deps import CurrentUser
from site_yonetim.core.errors import ApiError
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.fiscal import parse_period_key
from site_yonetim.domain.members import FORM_ERROR, FormFieldError
from site_yonetim.services.approvals import ApprovalError, Person

PENDING_MESSAGE = "onay kuralına tabi; talep onaya gönderildi, onaylanınca uygulanır."


def actor(current: CurrentUser) -> Person:
    return Person(current.user.id, current.user.full_name)


def form_error(exc: FormFieldError) -> ApiError:
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, exc.fields)


def approval_error(exc: ApprovalError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


def period_param(value: str, field: str = "period") -> YearMonth:
    parsed = parse_period_key(value)
    if parsed is None:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            FORM_ERROR,
            {field: "Dönem YYYY-AA biçiminde olmalı (ör. 2026-09)."},
        )
    return parsed
