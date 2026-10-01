"""Yüklenen belge kuralları — saf (docs/09 §3; docs/07 §4.13–4.14).

Uzantıya güvenilmez: içerik imzası doğrulanır. Disk adı sunucu tarafından üretilir; kullanıcının
verdiği ad yalnız indirme başlığında, temizlenmiş olarak kullanılır.
"""

import re
from dataclasses import dataclass

from site_yonetim.domain.finance import FinanceRuleError

MAX_FILE_BYTES = 10 * 1024 * 1024
FILE_NAME_MAX = 120
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


@dataclass(frozen=True, slots=True)
class FileKind:
    extension: str
    content_type: str


_KINDS = {
    "pdf": FileKind("pdf", "application/pdf"),
    "jpg": FileKind("jpg", "image/jpeg"),
    "jpeg": FileKind("jpg", "image/jpeg"),
    "png": FileKind("png", "image/png"),
    "webp": FileKind("webp", "image/webp"),
}


def _signature(data: bytes) -> str | None:
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def clean_file_name(name: str | None) -> str:
    """Yol ayıraçları ve geçersiz karakterler `_`; yalnız son parça; en fazla 120 karakter.

    `../../gizli fatura.pdf` → `gizli fatura.pdf`
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = " ".join(_UNSAFE.sub("_", base).split()).strip(". ")
    return (cleaned or "belge")[:FILE_NAME_MAX]


def check_document(name: str | None, data: bytes) -> tuple[str, FileKind]:
    """Uzantı beyaz listesi + içerik imzası + boyut. Temiz ad ve dosya türü döner."""
    file_name = clean_file_name(name)
    extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    kind = _KINDS.get(extension)
    if kind is None:
        raise FinanceRuleError(
            "invalid_file_type",
            "Belge PDF, JPG, PNG ya da WEBP olmalı.",
            field="document",
        )
    if not data:
        raise FinanceRuleError("empty_file", "Belge boş.", field="document")
    if len(data) > MAX_FILE_BYTES:
        raise FinanceRuleError("file_too_large", "Belge en fazla 10 MB olabilir.", field="document")
    if _signature(data) != kind.content_type:
        raise FinanceRuleError(
            "invalid_file_content",
            "Belgenin içeriği uzantısıyla uyuşmuyor; dosyayı yeniden kaydedip yükleyin.",
            field="document",
        )
    return file_name, kind
