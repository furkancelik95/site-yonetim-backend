from typing import Annotated, Any

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from site_yonetim.core.errors import ApiError, ConflictError, ForbiddenError, NotFoundError


class _Body(BaseModel):
    name: str = Field(min_length=3)
    count: int


@pytest.fixture
def error_client(app: FastAPI) -> TestClient:
    @app.get("/_test/not-found")
    async def _not_found() -> None:
        raise NotFoundError

    @app.get("/_test/forbidden")
    async def _forbidden() -> None:
        raise ForbiddenError

    @app.get("/_test/conflict")
    async def _conflict() -> None:
        raise ConflictError("period_already_charged", "Bu dönem için tahakkuk zaten kesilmiş.")

    @app.get("/_test/fields")
    async def _fields() -> None:
        raise ApiError(
            422, "validation_error", "Hatalı alan.", {"amount": "Tutar sıfırdan büyük olmalı."}
        )

    @app.get("/_test/crash")
    async def _crash() -> None:
        raise RuntimeError("gizli-ic-ayrinti /srv/app/secret.py")

    @app.post("/_test/validate")
    async def _validate(body: _Body, page: Annotated[int, Query(ge=1)] = 1) -> None:
        return None

    return TestClient(app, raise_server_exceptions=False)


def _error(body: Any) -> dict[str, Any]:
    assert set(body) == {"error"}
    error: dict[str, Any] = body["error"]
    assert set(error) == {"code", "message", "fields"}
    return error


def test_bulunamadi_404_ve_turkce_mesaj(error_client: TestClient) -> None:
    response = error_client.get("/_test/not-found")

    assert response.status_code == 404
    error = _error(response.json())
    assert error["code"] == "not_found"
    assert error["message"] == "Aradığınız kayıt bulunamadı."
    assert error["fields"] is None


def test_yetkisiz_islem_403(error_client: TestClient) -> None:
    response = error_client.get("/_test/forbidden")

    assert response.status_code == 403
    assert _error(response.json())["code"] == "forbidden"


def test_is_kurali_cakismasi_409_kodu_korur(error_client: TestClient) -> None:
    response = error_client.get("/_test/conflict")

    assert response.status_code == 409
    assert _error(response.json())["code"] == "period_already_charged"


def test_alan_hatalari_fields_icinde_doner(error_client: TestClient) -> None:
    error = _error(error_client.get("/_test/fields").json())

    assert error["fields"] == {"amount": "Tutar sıfırdan büyük olmalı."}


def test_tanimsiz_adres_turkce_404(error_client: TestClient) -> None:
    response = error_client.get("/api/v1/boyle-bir-adres-yok")

    assert response.status_code == 404
    assert _error(response.json())["message"] == "Aradığınız kayıt bulunamadı."


def test_desteklenmeyen_yontem_405(error_client: TestClient) -> None:
    response = error_client.delete("/api/v1/health")

    assert response.status_code == 405
    assert _error(response.json())["code"] == "method_not_allowed"


def test_dogrulama_hatasi_422_alan_bazinda_turkce(error_client: TestClient) -> None:
    response = error_client.post("/_test/validate?page=0", json={"name": "ab"})

    assert response.status_code == 422
    error = _error(response.json())
    assert error["code"] == "validation_error"
    assert error["fields"] == {
        "name": "En az 3 karakter olmalı.",
        "count": "Bu alan zorunludur.",
        "page": "En az 1 olmalı.",
    }


def test_bozuk_json_422(error_client: TestClient) -> None:
    response = error_client.post(
        "/_test/validate", content=b"{bozuk", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert "Gövde geçerli bir JSON değil." in _error(response.json())["fields"].values()


def test_beklenmeyen_hata_yigin_izi_sizdirmaz(error_client: TestClient) -> None:
    response = error_client.get("/_test/crash")

    assert response.status_code == 500
    assert _error(response.json())["code"] == "internal_error"
    assert "gizli-ic-ayrinti" not in response.text
    assert "Traceback" not in response.text
    assert "secret.py" not in response.text
