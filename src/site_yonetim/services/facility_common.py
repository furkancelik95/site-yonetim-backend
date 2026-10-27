"""Operasyon ekleri ve teknik varlık için ortak yardımcılar: saha fotoğrafı, bildirim alıcıları,
site ayarları ve konu belgesi (belge arşivine bağlı). Açık site kapsamında çalışır.

Saha fotoğrafı (sayaç, denetim, görev): ekran küçültülmüş görseli **veri adresi** olarak gönderir
(`data:image/jpeg;base64,…`). İçerik imzasıyla doğrulanır (JPEG/PNG/WebP, ≤ 2 MB), dosya deposuna
(web kökü dışı) yazılır, `stored_files`'a kaydedilir; yanıtta yalnız kimliği döner ve yetkili uçtan
indirilir. İşlem geri alınırsa yazılan dosyalar silinir (`PhotoSink.discard`).
"""

import base64
import binascii
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.access import ROLE_PERMISSIONS, Permission
from site_yonetim.domain.facility import ANOMALY_PERCENT_DEFAULT
from site_yonetim.domain.files import kind_from_content
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import OperationSettings, Site, StoredFile
from site_yonetim.services import members as members_svc
from site_yonetim.services.files import FileStore

MAX_PHOTO_BYTES = 2 * 1024 * 1024
_DATA_URL = re.compile(r"^data:image/(?:jpeg|jpg|png|webp);base64,(?P<data>[A-Za-z0-9+/=\s]+)$")
PHOTO_ERROR = "Fotoğraf okunamadı; JPEG, PNG ya da WebP (en çok 2 MB) ekleyin."
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


@dataclass
class PhotoSink:
    """Bir istekte yazılan fotoğraflar; işlem başarısızsa diskten silinir."""

    store: FileStore
    written: list[str] = field(default_factory=list)

    def discard(self) -> None:
        for path in self.written:
            self.store.delete(path)
        self.written.clear()


def decode_photo(value: str) -> bytes | None:
    match = _DATA_URL.match(value.strip())
    if match is None:
        return None
    try:
        data = base64.b64decode("".join(match["data"].split()), validate=True)
    except binascii.Error, ValueError:
        return None
    if not data or len(data) > MAX_PHOTO_BYTES:
        return None
    kind = kind_from_content(data)
    return data if kind is not None and kind.content_type in _IMAGE_TYPES else None


async def save_photo(
    session: AsyncSession, sink: PhotoSink, value: str | None, *, by: str, field_name: str
) -> uuid.UUID | None:
    """Veri adresini depoya yazar; boşsa `None`. Geçersizse alan hatası."""
    if not value:
        return None
    data = decode_photo(value)
    kind = kind_from_content(data) if data is not None else None
    if data is None or kind is None:
        raise FormFieldError({field_name: PHOTO_ERROR})
    scope = current_scope()
    if scope is None or scope.site_id is None:  # pragma: no cover - programlama hatası
        raise RuntimeError("Fotoğraf site kapsamında yazılır.")
    file_id = uuid.uuid7()
    storage_path, digest = sink.store.write(scope.site_id, file_id, kind, data)
    sink.written.append(storage_path)
    session.add(
        StoredFile(
            id=file_id,
            file_name=f"foto-{file_id}.{kind.extension}",
            content_type=kind.content_type,
            byte_size=len(data),
            storage_path=storage_path,
            sha256=digest,
            uploaded_by_name=by,
        )
    )
    await session.flush()
    return file_id


async def stored_file(session: AsyncSession, file_id: uuid.UUID) -> StoredFile | None:
    found: StoredFile | None = await session.scalar(
        select(StoredFile).where(StoredFile.id == file_id)
    )
    return found


async def holders(session: AsyncSession, site: Site, permission: Permission) -> set[uuid.UUID]:
    """İzne sahip etkin site personeli (bildirim alıcıları)."""
    return {
        m.user.id
        for m in await members_svc.list_members(session, site)
        if m.is_active and m.user.is_active and permission in ROLE_PERMISSIONS[m.role]
    }


async def settings(session: AsyncSession) -> OperationSettings:
    """Site ayarı; yoksa varsayılanla oluşturur (eşzamanlı ikinci istek kısıtla korunur)."""
    found: OperationSettings | None = await session.scalar(select(OperationSettings))
    if found is not None:
        return found
    scope = current_scope()
    if scope is None or scope.site_id is None:  # pragma: no cover - programlama hatası
        raise RuntimeError("Ayar site kapsamında okunur.")
    await session.execute(
        insert(OperationSettings)
        .values(
            id=uuid.uuid7(),
            site_id=scope.site_id,
            meter_anomaly_percent=ANOMALY_PERCENT_DEFAULT,
            meter_photo_required=False,
        )
        .on_conflict_do_nothing(index_elements=["site_id"])
    )
    created: OperationSettings = (await session.scalars(select(OperationSettings))).one()
    return created
