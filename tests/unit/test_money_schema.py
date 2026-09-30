"""API'de para metin olarak alınır ve verilir — docs/06-api-sozlesmesi.md §1.2."""

from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter, ValidationError

from site_yonetim.api.schemas import Money

money = TypeAdapter(Money)


class _Payment(BaseModel):
    amount: Money


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234.56", Decimal("1234.56")),
        ("-500", Decimal("-500.00")),
        ("0.1", Decimal("0.10")),
        ("9999999999999999.99", Decimal("9999999999999999.99")),
    ],
)
def test_gecerli_para_metni(raw: str, expected: Decimal) -> None:
    assert money.validate_python(raw) == expected


@pytest.mark.parametrize(
    ("raw", "error_type"),
    [
        (1234.56, "money_type"),
        (100, "money_type"),
        (True, "money_type"),
        (None, "money_type"),
        ("1.234,56", "money_format"),
        ("1,234.56", "money_format"),
        ("1e3", "money_format"),
        ("NaN", "money_format"),
        ("Infinity", "money_format"),
        (" 12.00", "money_format"),
        ("", "money_format"),
        ("12.345", "money_precision"),
        ("10000000000000000.00", "money_range"),
    ],
)
def test_gecersiz_para_reddedilir(raw: Any, error_type: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        money.validate_python(raw)

    assert exc_info.value.errors()[0]["type"] == error_type


def test_decimal_nan_reddedilir() -> None:
    with pytest.raises(ValidationError):
        money.validate_python(Decimal("NaN"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1234.5"), "1234.50"),
        (Decimal("-500"), "-500.00"),
        (Decimal("-0.00"), "0.00"),
        (Decimal("0"), "0.00"),
    ],
)
def test_para_json_metin_iki_ondalik(value: Decimal, expected: str) -> None:
    assert _Payment(amount=value).model_dump(mode="json") == {"amount": expected}
    assert _Payment(amount=value).model_dump()["amount"] == expected


def test_uc_noktada_para_sayi_gelirse_turkce_422(app: FastAPI) -> None:
    @app.post("/_test/payment")
    async def _create(body: _Payment) -> _Payment:
        return body

    with TestClient(app) as client:
        bad = client.post("/_test/payment", json={"amount": 1234.56})
        good = client.post("/_test/payment", json={"amount": "1234.56"})

    assert bad.status_code == 422
    assert bad.json()["error"]["fields"] == {
        "amount": 'Tutar metin olarak gönderilmeli (ör. "1234.56").'
    }
    assert good.json() == {"amount": "1234.56"}


def test_openapi_semasinda_para_metin(app: FastAPI) -> None:
    @app.post("/_test/payment")
    async def _create(body: _Payment) -> _Payment:
        return body

    schema = app.openapi()["components"]["schemas"]["_Payment"]["properties"]["amount"]

    assert schema["type"] == "string"
    assert "pattern" in schema
