"""İstek gövdesi boyut sınırı — ayrıştırmadan önce 413 (docs/09 §3)."""

import re
from collections.abc import AsyncIterator
from typing import Annotated

import httpx2
import pytest
from fastapi import FastAPI, File, UploadFile

from site_yonetim.core.errors import install_error_handlers
from site_yonetim.core.middleware import BodySizeLimitMiddleware


def make_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/json")
    async def echo(body: dict[str, str]) -> dict[str, int]:
        return {"keys": len(body)}

    @app.post("/upload")
    async def upload(file: Annotated[UploadFile, File()]) -> dict[str, int]:
        return {"size": len(await file.read())}

    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=100, overrides=[(re.compile("/upload"), 2000)]
    )
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=make_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_small_body_passes(client: httpx2.AsyncClient) -> None:
    response = await client.post("/json", json={"a": "b"})
    assert response.status_code == 200


async def test_declared_length_over_limit_is_rejected(client: httpx2.AsyncClient) -> None:
    response = await client.post("/json", json={"a": "x" * 200})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.json()["error"]["message"] == "Gönderilen veri çok büyük."


async def test_streamed_body_without_length_is_counted(client: httpx2.AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"x" * 50

    response = await client.post(
        "/json", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_multipart_over_limit_is_413_not_400(client: httpx2.AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        boundary = b"--sinir\r\n"
        yield boundary + b'Content-Disposition: form-data; name="file"; filename="a.xlsx"\r\n\r\n'
        for _ in range(100):
            yield b"x" * 100

    response = await client.post(
        "/upload",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=sinir"},
    )
    assert response.status_code == 413


async def test_path_override_allows_bigger_upload(client: httpx2.AsyncClient) -> None:
    response = await client.post("/upload", files={"file": ("a.xlsx", b"x" * 1500)})
    assert response.status_code == 200
    assert response.json() == {"size": 1500}


async def test_invalid_content_length_is_rejected() -> None:
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:  # pragma: no cover - okunmamalı
        raise AssertionError

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    middleware = BodySizeLimitMiddleware(make_app(), max_bytes=100)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/json",
        "headers": [(b"content-length", b"-1")],
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]
    assert sent[0]["status"] == 413


async def test_non_http_scope_passes_through() -> None:
    called: list[str] = []

    async def app(scope: object, receive: object, send: object) -> None:
        called.append("app")

    middleware = BodySizeLimitMiddleware(app, max_bytes=1)
    await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert called == ["app"]
