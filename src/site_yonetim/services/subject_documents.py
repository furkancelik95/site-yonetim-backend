"""Kayda bağlı belge — poliçe, hasar dosyası, teknik varlık ve muayene raporu belgeleri belge
arşivine (`documents`, RAP-03) yazılır ve `subject_type/subject_id` ile kayda bağlanır.

- Belge "yalnız yönetim" gizliliğinde, tüm site kapsamında açılır; ad kaydın kimliğini taşır
  (aynı ad → aynı belgenin yeni sürümü). Ad başka kayda bağlı bir belgeyle çakışırsa reddedilir.
- Okuma/indirme kaydın modül iznine bağlıdır (sigorta: gider okuma; varlık: envanter okuma) —
  belge modülü kapalı olsa da kayıttan erişilir.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.access import Permission
from site_yonetim.domain.documents import (
    TITLE_MAX,
    Confidentiality,
    DocumentKind,
    DocumentRuleError,
    DocumentScope,
)
from site_yonetim.models import (
    Document,
    StoredFile,
)
from site_yonetim.services import documents as documents_svc
from site_yonetim.services.files import FileStore


@dataclass(frozen=True, slots=True)
class SubjectSpec:
    read: Permission
    manage: Permission
    kind: DocumentKind


SUBJECTS: dict[str, SubjectSpec] = {
    "insurance_policies": SubjectSpec(
        Permission.EXPENSES_READ, Permission.EXPENSES_MANAGE, DocumentKind.INSURANCE
    ),
    "insurance_claims": SubjectSpec(
        Permission.EXPENSES_READ, Permission.EXPENSES_MANAGE, DocumentKind.INSURANCE
    ),
    "technical_assets": SubjectSpec(
        Permission.INVENTORY_READ, Permission.INVENTORY_MANAGE, DocumentKind.OTHER
    ),
    "asset_inspection_records": SubjectSpec(
        Permission.INVENTORY_READ, Permission.INVENTORY_MANAGE, DocumentKind.OTHER
    ),
}


def fit_title(text: str) -> str:
    title = " ".join(text.split())
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1].rstrip() + "…"


async def attach(
    session: AsyncSession,
    store: FileStore,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    title: str,
    file_name: str | None,
    data: bytes,
    uploader: documents_svc.Uploader,
    kind: DocumentKind | None = None,
    valid_until: date | None = None,
) -> documents_svc.Uploaded:
    """Belge arşivine yükler; yazılan dosya `Uploaded.storage_path` (geri almada silinir)."""
    spec = SUBJECTS[subject_type]
    uploaded = await documents_svc.upload(
        session,
        documents_svc.Upload(
            title=fit_title(title),
            kind=(kind or spec.kind).value,
            scope=DocumentScope.SITE.value,
            scope_id=None,
            confidentiality=Confidentiality.MANAGEMENT.value,
            valid_until=valid_until,
            retention_years=None,
            file_name=file_name,
            data=data,
        ),
        uploader=uploader,
        store=store,
        subject=documents_svc.Subject(subject_type, subject_id),
    )
    document = uploaded.document
    if (document.subject_type, document.subject_id) != (subject_type, subject_id):
        store.delete(uploaded.storage_path)
        raise DocumentRuleError(
            "title_taken", "Bu adla başka kayda bağlı bir belge var; farklı bir ad verin."
        )
    return uploaded


async def for_subjects(
    session: AsyncSession, subject_type: str, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Document]]:
    out: dict[uuid.UUID, list[Document]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = await session.scalars(
        select(Document)
        .where(Document.subject_type == subject_type, Document.subject_id.in_(list(ids)))
        .order_by(Document.created_at)
    )
    for document in rows:
        if document.subject_id is not None:
            out.setdefault(document.subject_id, []).append(document)
    return out


async def latest_file(
    session: AsyncSession, document_id: uuid.UUID
) -> tuple[Document, StoredFile] | None:
    document = await documents_svc.get(session, document_id)
    if document is None or document.subject_type not in SUBJECTS:
        return None
    stored = await documents_svc.version_file(session, document, document.current_version)
    return (document, stored) if stored is not None else None
