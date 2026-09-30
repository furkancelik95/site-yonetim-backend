"""HTTP ara katmanları: istek kimliği, erişim logu, güvenlik başlıkları, gövde boyutu sınırı.

Saf ASGI olarak yazıldı (BaseHTTPMiddleware değil) — akış yanıtlarını bozmaz, contextvar'lar
istek boyunca doğru taşınır.
"""

import json
import logging
import re
import time
import uuid
from collections.abc import Sequence

from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from site_yonetim.core.errors import error_body
from site_yonetim.core.logging import request_id_var

logger = logging.getLogger("site_yonetim.access")

REQUEST_ID_HEADER = "X-Request-ID"
# İstemcinin gönderdiği kimliği yalnız güvenli biçimdeyse kabul et (log enjeksiyonuna karşı).
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class RequestContextMiddleware:
    """Her isteğe bir `request_id` bağlar, yanıta yazar ve erişim logu atar.

    Erişim logunda sorgu dizesi **yazılmaz** (`?q=ayşe` gibi kişisel veri içerebilir).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, REQUEST_ID_HEADER.lower())
        request_id = incoming if incoming and _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info("%s %s %s %.1fms", scope["method"], scope["path"], status_code, elapsed_ms)
            request_id_var.reset(token)


_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class SecurityHeadersMiddleware:
    """JSON API için sıkı güvenlik başlıkları (docs/09-guvenlik-kvkk.md §4).

    `/docs` (Swagger arayüzü) CDN'den betik yüklediği için CSP'den muaftır; bu arayüz
    üretimde kapalıdır (`API_DOCS_ENABLED=false`).
    """

    def __init__(self, app: ASGIApp, *, hsts: bool, docs_paths: frozenset[str]) -> None:
        self.app = app
        self.hsts = hsts
        self.docs_paths = docs_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_docs = scope["path"] in self.docs_paths

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Resource-Policy"] = "same-origin"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                if not is_docs:
                    headers["Content-Security-Policy"] = _API_CSP
                    headers.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _BodyTooLargeError(HTTPException):
    """HTTPException olmalı: FastAPI gövde okurken çıkan diğer hataları 400'e çevirir."""

    def __init__(self) -> None:
        super().__init__(status_code=413)


_TOO_LARGE_BODY = json.dumps(
    error_body("payload_too_large", "Gönderilen veri çok büyük."), ensure_ascii=False
).encode()


class BodySizeLimitMiddleware:
    """İstek gövdesini sınırlar; aşılırsa 413 döner (docs/09 §3).

    Starlette çok parçalı (multipart) dosyaları sınırsız diske yazar; sınır ayrıştırmadan önce,
    akış okunurken uygulanır. `Content-Length` yoksa (chunked) da sayılır. Yol kalıbına göre
    daha yüksek sınır verilebilir (Excel yükleme).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        overrides: Sequence[tuple[re.Pattern[str], int]] = (),
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = tuple(overrides)

    def _limit(self, path: str) -> int:
        for pattern, limit in self.overrides:
            if pattern.fullmatch(path):
                return limit
        return self.max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope["path"])
        declared = _header(scope, "content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await _send_too_large(send)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLargeError:
            if response_started:  # pragma: no cover - gövde yanıt başladıktan sonra okunmaz
                raise
            await _send_too_large(send)


async def _send_too_large(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_TOO_LARGE_BODY)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _TOO_LARGE_BODY})


def _header(scope: Scope, name: str) -> str | None:
    target = name.encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == target:
            decoded: str = value.decode("latin-1")
            return decoded
    return None
