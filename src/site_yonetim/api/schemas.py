"""API şemalarında ortak tipler (docs/06-api-sozlesmesi.md §1.2).

`Money`: JSON'da **metin**, 2 ondalık (`"1234.56"`, `"-500.00"`). JavaScript sayıları kayan
noktalıdır; para sayı olarak gönderilirse frontend'de kuruş kayar. Bu yüzden girdide de JSON
sayısı (`1234.56`) **kabul edilmez**.
"""

import re
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema
from pydantic_core import PydanticCustomError

from site_yonetim.domain.money import CENT, ZERO

# NUMERIC(18,2): 16 tam hane + 2 ondalık.
MONEY_MAX = Decimal("9999999999999999.99")


_MONEY_TEXT = re.compile(r"^-?\d+(?:\.\d+)?$")


def _parse_money(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PydanticCustomError("money_format", "invalid money")
        amount = value
    elif isinstance(value, str):
        # Yalnız "1234.56" biçimi: binlik ayıraç, virgül, "1e3", "NaN", boşluk yok.
        if not _MONEY_TEXT.match(value):
            raise PydanticCustomError("money_format", "invalid money")
        amount = Decimal(value)
    else:
        # float, int, bool, None… — para yalnız metin olarak gelir.
        raise PydanticCustomError("money_type", "money must be a string")

    if amount != amount.quantize(CENT):
        raise PydanticCustomError("money_precision", "too many decimals")
    if abs(amount) > MONEY_MAX:
        raise PydanticCustomError("money_range", "out of range")
    return amount.quantize(CENT)


def format_money(amount: Decimal) -> str:
    """Decimal → "1234.56". Eksi sıfır "0.00" olarak yazılır."""
    quantized = amount.quantize(CENT)
    if quantized == ZERO:
        quantized = abs(quantized)
    return f"{quantized:f}"


Money = Annotated[
    Decimal,
    BeforeValidator(_parse_money),
    PlainSerializer(format_money, return_type=str, when_used="always"),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^-?\d{1,16}\.\d{2}$",
            "examples": ["1234.56"],
            "description": "Para: metin, 2 ondalık (TRY).",
        }
    ),
]
