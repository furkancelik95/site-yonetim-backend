"""Tek hata biçimi (docs/06-api-sozlesmesi.md §1.4).

Her hata şu gövdeyi döner:
    {"error": {"code": "...", "message": "Türkçe mesaj", "fields": {...} | null}}

Yığın izi (stack trace) yanıta asla girmez; ayrıntı yalnız sunucu logunda.
"""

import logging
from http import HTTPStatus
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Kullanıcıya gösterilecek, beklenen bir hata.

    `code` makine içindir (İngilizce snake_case, sabit); `message` Türkçedir ve kullanıcıya
    ne yapması gerektiğini söyler.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        fields: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields


class NotFoundError(ApiError):
    def __init__(self, message: str = "Aradığınız kayıt bulunamadı.") -> None:
        super().__init__(HTTPStatus.NOT_FOUND, "not_found", message)


class ForbiddenError(ApiError):
    def __init__(self, message: str = "Bu işlem için yetkiniz yok.") -> None:
        super().__init__(HTTPStatus.FORBIDDEN, "forbidden", message)


class ConflictError(ApiError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(HTTPStatus.CONFLICT, code, message)


# Framework'ün ürettiği HTTP hataları için Türkçe karşılıklar.
_HTTP_DEFAULTS: dict[int, tuple[str, str]] = {
    400: ("bad_request", "İstek biçimi hatalı. Gönderilen veriyi kontrol edin."),
    401: ("unauthorized", "Oturumunuz yok ya da süresi doldu. Lütfen yeniden giriş yapın."),
    403: ("forbidden", "Bu işlem için yetkiniz yok."),
    404: ("not_found", "Aradığınız kayıt bulunamadı."),
    405: ("method_not_allowed", "Bu adres bu işlem türünü desteklemiyor."),
    406: ("not_acceptable", "İstenen yanıt biçimi desteklenmiyor."),
    413: ("payload_too_large", "Gönderilen veri çok büyük."),
    415: ("unsupported_media_type", "Gönderilen içerik türü desteklenmiyor."),
    429: ("too_many_requests", "Çok fazla istek gönderildi. Lütfen biraz bekleyip tekrar deneyin."),
}

_INTERNAL_ERROR = (
    "internal_error",
    "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin; sorun sürerse destek ekibine bildirin.",
)

# Pydantic doğrulama hatası türleri → Türkçe alan mesajı.
_VALIDATION_MESSAGES: dict[str, str] = {
    "missing": "Bu alan zorunludur.",
    "string_too_short": "En az {min_length} karakter olmalı.",
    "string_too_long": "En fazla {max_length} karakter olmalı.",
    "string_type": "Metin olmalı.",
    "int_parsing": "Tam sayı olmalı.",
    "int_type": "Tam sayı olmalı.",
    "decimal_parsing": "Geçerli bir tutar olmalı.",
    "decimal_type": "Geçerli bir tutar olmalı.",
    "bool_parsing": "Doğru/yanlış değeri olmalı.",
    "date_from_datetime_parsing": "Tarih YYYY-AA-GG biçiminde olmalı.",
    "date_parsing": "Tarih YYYY-AA-GG biçiminde olmalı.",
    "uuid_parsing": "Geçerli bir kimlik olmalı.",
    "greater_than": "{gt} değerinden büyük olmalı.",
    "greater_than_equal": "En az {ge} olmalı.",
    "less_than": "{lt} değerinden küçük olmalı.",
    "less_than_equal": "En fazla {le} olmalı.",
    "enum": "Geçerli değerlerden biri olmalı: {expected}.",
    "literal_error": "Geçerli değerlerden biri olmalı: {expected}.",
    "value_error": "Geçersiz değer.",
    "extra_forbidden": "Bu alan tanınmıyor.",
    "json_invalid": "Gövde geçerli bir JSON değil.",
    "money_type": 'Tutar metin olarak gönderilmeli (ör. "1234.56").',
    "money_format": 'Geçerli bir tutar olmalı (ör. "1234.56"; binlik ayıraç kullanmayın).',
    "money_precision": "Tutar en fazla 2 ondalık (kuruş) içerebilir.",
    "money_range": "Tutar izin verilen aralığın dışında.",
}
_VALIDATION_FALLBACK = "Geçersiz değer."


def error_body(code: str, message: str, fields: dict[str, str] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "fields": fields}}


def _validation_message(error: dict[str, Any]) -> str:
    template = _VALIDATION_MESSAGES.get(error.get("type", ""), _VALIDATION_FALLBACK)
    try:
        return template.format(**(error.get("ctx") or {}))
    except KeyError, IndexError, ValueError:
        return _VALIDATION_FALLBACK


def _field_name(loc: tuple[Any, ...]) -> str:
    # ("body", "amount") → "amount"; ("query", "page") → "page"; ("body",) → "body"
    parts = [str(p) for p in loc[1:]] if len(loc) > 1 else [str(p) for p in loc]
    return ".".join(parts) or "body"


def validation_fields(exc: RequestValidationError) -> dict[str, str]:
    fields: dict[str, str] = {}
    for error in exc.errors():
        name = _field_name(tuple(error.get("loc", ())))
        fields.setdefault(name, _validation_message(error))
    return fields


async def _api_error_handler(_: Request, error: Exception) -> JSONResponse:
    exc = cast(ApiError, error)
    return JSONResponse(
        status_code=exc.status_code, content=error_body(exc.code, exc.message, exc.fields)
    )


async def _http_error_handler(_: Request, error: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, error)
    code, message = _HTTP_DEFAULTS.get(exc.status_code, _INTERNAL_ERROR)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, message),
        headers=exc.headers,
    )


async def _validation_error_handler(_: Request, error: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, error)
    return JSONResponse(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        content=error_body(
            "validation_error",
            "Gönderilen bilgilerde hata var. İşaretli alanları düzeltip tekrar deneyin.",
            validation_fields(exc),
        ),
    )


async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Beklenmeyen hata", exc_info=exc)
    return JSONResponse(status_code=500, content=error_body(*_INTERNAL_ERROR))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
