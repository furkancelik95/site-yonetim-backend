"""Excel hücresinden sayı okuma — kültür tuzağına karşı (docs/11 §3.1, docs/07 §5.3).

`"104.50"` tr-TR'de 10450, İngilizcede 104,5'tir. Tahmin edilmez; ayracın konumuna bakılır.
"""

import math
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

CellValue = str | int | float | Decimal | bool | date | datetime | time | None

_PLAIN_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")
_THOUSANDS_GROUPS = re.compile(r"^[+-]?\d{1,3}(\.\d{3})+$")


def parse_decimal(text: str) -> Decimal:
    """Metni sayıya çevirir; sayı değilse `ValueError`.

    1. Boşluklar (bölünmez dahil) silinir.
    2. Hem virgül hem nokta → sondaki ondalık, diğeri binlik: `1.234,56` · `1,234.56`.
    3. Yalnız virgül → ondalık: `104,50`.
    4. Yalnız nokta → birden çoksa binlik (`1.234.567`); tek nokta, arkasında tam 3 hane ve
       tam kısım 0 değilse binlik (`1.234`); diğer → ondalık (`104.50`, `0.250`).
    """
    s = "".join(text.split())
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if s.count(",") > 1:
            raise ValueError(text)
        s = s.replace(",", ".")
    elif s.count(".") > 1:
        if not _THOUSANDS_GROUPS.match(s):
            raise ValueError(text)
        s = s.replace(".", "")
    elif "." in s:
        whole, fraction = s.split(".")
        if len(fraction) == 3 and whole.lstrip("+-") not in ("", "0"):
            s = whole + fraction
    if not _PLAIN_NUMBER.match(s):
        raise ValueError(text)
    try:
        return Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover - düzenli ifade zaten eler
        raise ValueError(text) from exc


def cell_text(value: CellValue) -> str:
    """Hücre → kırpılmış, içteki boşlukları teke inmiş metin. `5.0` gibi tam sayılar `5` olur."""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return " ".join(str(value).split())


def cell_decimal(value: CellValue) -> Decimal | None:
    """Hücre → sayı; boşsa `None`, sayı değilse `ValueError`.

    Excel'in sayı olarak sakladığı hücrede kültür sorunu yoktur; yalnız metin hücreler
    `parse_decimal` ile okunur.
    """
    if isinstance(value, bool):
        raise ValueError(str(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(str(value))
        return Decimal(repr(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(str(value))
        return value
    text = cell_text(value)
    if not text:
        return None
    if isinstance(value, (datetime, date, time)):
        raise ValueError(text)
    return parse_decimal(text)
