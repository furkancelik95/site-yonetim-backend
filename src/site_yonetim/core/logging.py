"""Yapılandırılmış (JSON) log.

- Her satırda `request_id`, `site_id`, `user_id` (docs/02-mimari.md §9).
- Kişisel veri (e-posta, telefon, TC kimlik) loga yazılmaz; son savunma hattı olarak
  mesajlar maskelenir (docs/09-guvenlik-kvkk.md §5). Asıl kural: kişisel veriyi loglamamak.
"""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
site_id_var: ContextVar[str | None] = ContextVar("site_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# +90 / 0 önekli ya da öneksiz 10 haneli Türkiye cep/sabit telefonu (boşluk, tire, parantezli)
_PHONE = re.compile(
    r"(?<!\d)(?:\+?90[\s-]?|0)?\(?[2-5]\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)"
)
# TC kimlik numarası: 11 hane, ilk hane 0 değil
_NATIONAL_ID = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")


def redact_pii(text: str) -> str:
    text = _EMAIL.sub("[e-posta]", text)
    text = _NATIONAL_ID.sub("[tc-kimlik]", text)
    return _PHONE.sub("[telefon]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_pii(record.getMessage()),
            "request_id": request_id_var.get(),
            "site_id": site_id_var.get(),
            "user_id": user_id_var.get(),
        }
        if record.exc_info:
            payload["exc"] = redact_pii(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn'un kendi erişim logu istemci IP'si ve sorgu dizesi yazar; biz kendi
    # erişim logumuzu (middleware) yazıyoruz.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
