"""Belge arşivi — Yazılım Dokümanı RAP-03, 18.2 (frontend servis isteği 78).

- Yükleme: alanlar + dosya birlikte doğrulanır; dosya önce doğrulanır, sonra diske yazılır
  (reddedilen dosya diske hiç yazılmaz). Aynı ad + kapsam → yeni sürüm (işlem kilidi altında;
  eşzamanlı iki yükleme iki ayrı belge açamaz). Son sürümle aynı SHA-256 → `same_file`.
- Görünürlük SQL'de süzülür (liste sayfalı): personel gizli dışını görür, gizliyi yalnız
  `documents.confidential`; sakin sakinlere açık (kapsamındaysa) ve bölümüne özel belgeleri.
- Gizli belgenin her açılışı `document_access_logs`'a (değişmez) yazılır; her indirme ayrıca
  denetim kaydına (`download`).
- Geçerlilik uyarısı günlük iş: 60 / 30 gün kala ve bitince **bir kez** (bildirim tekil anahtarı
  belge + bitiş tarihi + eşik; yeni sürümle bitiş değişirse uyarılar yeniden başlar).
- Virüs taraması yok (ilk sürüm) — hiçbir yanıt "tarandı" demez.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, current_scope, site_scope
from site_yonetim.domain.access import ROLE_PERMISSIONS, Permission
from site_yonetim.domain.documents import (
    WARN_FIRST,
    CheckedDocument,
    CheckedFile,
    Confidentiality,
    DocumentFieldError,
    DocumentInput,
    DocumentScope,
    ExpiryWarning,
    check_document,
    check_file,
    days_left,
    expiry_message,
    expiry_warning,
    same_file,
    title_key,
)
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.models import (
    Block,
    Document,
    DocumentAccessLog,
    DocumentVersion,
    Site,
    StoredFile,
    Unit,
    UnitParty,
)
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import members as members_svc
from site_yonetim.services import notifications
from site_yonetim.services.files import FileStore
from site_yonetim.services.security import unit_names

_TITLE_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
EXPIRED_NOTICE_DAYS = 30  # bitişten sonra bu kadar gün içinde "süresi doldu" bildirimi
JOB_ACTOR = "Belge geçerlilik işi"


# --- Yükleme ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Upload:
    title: str | None
    kind: str | None
    scope: str | None
    scope_id: uuid.UUID | None
    confidentiality: str | None
    valid_until: date | None
    retention_years: int | None
    file_name: str | None
    data: bytes
    form_errors: dict[str, str] = field(default_factory=dict)  # biçim hataları (tarih, sayı)


@dataclass(frozen=True, slots=True)
class Uploader:
    user_id: uuid.UUID | None
    name: str
    can_confidential: bool


@dataclass(frozen=True, slots=True)
class Subject:
    """Belgeyi başka modülün kaydına bağlamak için (ör. `contracts`, id)."""

    type: str
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Uploaded:
    document: Document
    version: int
    is_new: bool
    storage_path: str


def _validate(data: Upload) -> tuple[CheckedDocument, CheckedFile]:
    """Alan ve dosya hataları tek seferde."""
    errors: dict[str, str] = dict(data.form_errors)
    checked = file = None
    try:
        checked = check_document(
            DocumentInput(
                title=data.title,
                kind=data.kind,
                scope=data.scope,
                has_scope_target=data.scope_id is not None,
                confidentiality=data.confidentiality,
                retention_years=data.retention_years,
            )
        )
    except DocumentFieldError as exc:
        errors = {**exc.fields, **errors}
    try:
        file = check_file(data.file_name, data.data)
    except DocumentFieldError as exc:
        errors.update(exc.fields)
    if errors or checked is None or file is None:
        raise DocumentFieldError(errors)
    return checked, file


async def _target(
    session: AsyncSession, scope: DocumentScope, scope_id: uuid.UUID | None
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """(block_id, unit_id); kayıt bu sitede yoksa alan hatası."""
    if scope is DocumentScope.BLOCK:
        if await session.scalar(select(Block.id).where(Block.id == scope_id)) is None:
            raise DocumentFieldError({"scope": "Blok bulunamadı."})
        return scope_id, None
    if scope is DocumentScope.UNIT:
        if await session.scalar(select(Unit.id).where(Unit.id == scope_id)) is None:
            raise DocumentFieldError({"scope": "Bölüm bulunamadı."})
        return None, scope_id
    return None, None


async def upload(
    session: AsyncSession,
    data: Upload,
    *,
    uploader: Uploader,
    store: FileStore,
    subject: Subject | None = None,
) -> Uploaded:
    """Yeni belge ya da mevcut belgeye yeni sürüm. Dosya yazıldıktan sonra işlem geri alınırsa
    çağıran `storage_path`'i silmelidir (`FileStore.delete`)."""
    checked, file = _validate(data)
    block_id, unit_id = await _target(session, checked.scope, data.scope_id)
    if checked.confidentiality is Confidentiality.SECRET and not uploader.can_confidential:
        raise DocumentFieldError(
            {"confidentiality": "Gizli belgeyi yalnız gizli belge yetkisi olanlar yükler."}
        )
    scope = current_scope()
    if scope is None or scope.site_id is None:  # pragma: no cover - programlama hatası
        raise RuntimeError("Belge site kapsamında yüklenir.")
    key = title_key(checked.title)
    await session.execute(_TITLE_LOCK, {"key": f"document:{scope.site_id}:{key}"})
    document = await session.scalar(
        select(Document)
        .where(
            Document.title_key == key,
            Document.scope == checked.scope.value,
            Document.block_id.is_not_distinct_from(block_id),
            Document.unit_id.is_not_distinct_from(unit_id),
        )
        .with_for_update()
    )
    if (
        document is not None
        and document.confidentiality == Confidentiality.SECRET.value
        and not uploader.can_confidential
    ):
        raise DocumentFieldError(
            {"title": "Bu ad ve kapsamda bir belge var; yeni sürümünü yönetici yükler."}
        )
    digest = FileStore.digest(data.data)
    if document is not None:
        last = await session.scalar(
            select(StoredFile.sha256)
            .join(
                DocumentVersion,
                and_(
                    DocumentVersion.stored_file_id == StoredFile.id,
                    DocumentVersion.site_id == StoredFile.site_id,
                ),
            )
            .where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == document.current_version,
            )
        )
        if last == digest:
            raise same_file()

    is_new = document is None
    if document is None:
        document = Document(
            id=uuid.uuid7(),
            title=checked.title,
            title_key=key,
            kind=checked.kind.value,
            scope=checked.scope.value,
            block_id=block_id,
            unit_id=unit_id,
            confidentiality=checked.confidentiality.value,
            valid_until=data.valid_until,
            retention_years=checked.retention_years,
            subject_type=subject.type if subject else None,
            subject_id=subject.id if subject else None,
            current_version=1,
            created_by_name=uploader.name,
        )
        session.add(document)
        await session.flush()
    else:
        document.title = checked.title
        document.kind = checked.kind.value
        document.confidentiality = checked.confidentiality.value
        if data.valid_until is not None:
            document.valid_until = data.valid_until
        if checked.retention_years is not None:
            document.retention_years = checked.retention_years
        document.current_version += 1

    file_id = uuid.uuid7()
    storage_path, _ = store.write(scope.site_id, file_id, file.kind, data.data)
    session.add(
        StoredFile(
            id=file_id,
            file_name=file.file_name,
            content_type=file.kind.content_type,
            byte_size=len(data.data),
            storage_path=storage_path,
            sha256=digest,
            uploaded_by_name=uploader.name,
        )
    )
    await session.flush()
    session.add(
        DocumentVersion(
            document_id=document.id,
            version=document.current_version,
            stored_file_id=file_id,
            uploaded_by_id=uploader.user_id,
            uploaded_by_name=uploader.name,
        )
    )
    await session.flush()
    return Uploaded(document, document.current_version, is_new, storage_path)


# --- Okuma --------------------------------------------------------------------------


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def staff_query(
    *,
    can_confidential: bool,
    today: date,
    q: str | None = None,
    kind: str | None = None,
    expiring: bool = False,
) -> Select[Document]:
    """Personel listesi: gizli belge yalnız yetkiliye; `expiring` → 60 gün içinde biten ya da
    bitmiş. Son yüklenen üstte."""
    query = select(Document)
    if not can_confidential:
        query = query.where(Document.confidentiality != Confidentiality.SECRET.value)
    term = title_key(q or "")
    if term:
        query = query.where(Document.title_key.like(_like(term)))
    if kind:
        query = query.where(Document.kind == kind)
    if expiring:
        query = query.where(
            Document.valid_until.is_not(None),
            Document.valid_until <= today + timedelta(days=WARN_FIRST),
        )
    return query.order_by(
        func.coalesce(Document.updated_at, Document.created_at).desc(), Document.id.desc()
    )


@dataclass(frozen=True, slots=True)
class MyPlaces:
    units: frozenset[uuid.UUID]
    blocks: frozenset[uuid.UUID]


async def my_places(session: AsyncSession, person_id: uuid.UUID, today: date) -> MyPlaces:
    """Sakinin bugün ilişkili olduğu bölümler ve blokları."""
    rows = await session.execute(
        select(Unit.id, Unit.block_id)
        .join(UnitParty, and_(UnitParty.unit_id == Unit.id, UnitParty.site_id == Unit.site_id))
        .where(
            UnitParty.person_id == person_id,
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
    )
    pairs = list(rows)
    return MyPlaces(frozenset(u for u, _ in pairs), frozenset(b for _, b in pairs))


def resident_query(places: MyPlaces) -> Select[Document]:
    """Sakinlere açık: kapsamdaki sakin (site → herkes, blok → o bloktaki, bölüm → o bölümün);
    bölüme özel: o bölümün sakinleri. Yönetim ve gizli belge hiç görünmez."""
    public = Document.confidentiality == Confidentiality.PUBLIC.value
    visible = or_(
        and_(public, Document.scope == DocumentScope.SITE.value),
        and_(public, Document.block_id.in_(places.blocks)),
        and_(
            Document.confidentiality.in_(
                [Confidentiality.PUBLIC.value, Confidentiality.UNIT.value]
            ),
            Document.unit_id.in_(places.units),
        ),
    )
    return (
        select(Document)
        .where(visible)
        .order_by(
            func.coalesce(Document.updated_at, Document.created_at).desc(), Document.id.desc()
        )
    )


@dataclass(frozen=True, slots=True)
class VersionRow:
    version: int
    file_name: str
    content_type: str
    size: int
    sha256: str
    uploaded_by: str
    at: datetime


async def versions(
    session: AsyncSession, document_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[VersionRow]]:
    """Belge başına sürümler (eskiden yeniye)."""
    out: dict[uuid.UUID, list[VersionRow]] = {d: [] for d in document_ids}
    if not document_ids:
        return out
    rows = await session.execute(
        select(DocumentVersion, StoredFile)
        .join(
            StoredFile,
            and_(
                StoredFile.id == DocumentVersion.stored_file_id,
                StoredFile.site_id == DocumentVersion.site_id,
            ),
        )
        .where(DocumentVersion.document_id.in_(document_ids))
        .order_by(DocumentVersion.document_id, DocumentVersion.version)
    )
    for version, stored in rows:
        out[version.document_id].append(
            VersionRow(
                version=version.version,
                file_name=stored.file_name,
                content_type=stored.content_type,
                size=stored.byte_size,
                sha256=stored.sha256 or "",
                uploaded_by=version.uploaded_by_name,
                at=version.created_at,
            )
        )
    return out


async def scope_names(session: AsyncSession, documents: Sequence[Document]) -> dict[uuid.UUID, str]:
    """Blok ve bölüm adları (`A Blok`, `A-12`) — id → ad."""
    block_ids = {d.block_id for d in documents if d.block_id is not None}
    names: dict[uuid.UUID, str] = {}
    if block_ids:
        rows = await session.execute(select(Block.id, Block.name).where(Block.id.in_(block_ids)))
        names.update(dict(rows.all()))
    names.update(await unit_names(session, {d.unit_id for d in documents if d.unit_id}))
    return names


async def get(session: AsyncSession, document_id: uuid.UUID) -> Document | None:
    row: Document | None = await session.scalar(select(Document).where(Document.id == document_id))
    return row


async def version_file(
    session: AsyncSession, document: Document, version: int
) -> StoredFile | None:
    row: StoredFile | None = await session.scalar(
        select(StoredFile)
        .join(
            DocumentVersion,
            and_(
                DocumentVersion.stored_file_id == StoredFile.id,
                DocumentVersion.site_id == StoredFile.site_id,
            ),
        )
        .where(DocumentVersion.document_id == document.id, DocumentVersion.version == version)
    )
    return row


def log_secret_access(
    session: AsyncSession,
    document: Document,
    version: int,
    *,
    user_id: uuid.UUID | None,
    user_name: str,
) -> None:
    """Gizli belge açıldı — değişmez kayıt (aynı transaction'da)."""
    if document.confidentiality == Confidentiality.SECRET.value:
        session.add(
            DocumentAccessLog(
                document_id=document.id, version=version, user_id=user_id, user_name=user_name
            )
        )


def access_log_query(document_id: uuid.UUID) -> Select[DocumentAccessLog]:
    return (
        select(DocumentAccessLog)
        .where(DocumentAccessLog.document_id == document_id)
        .order_by(DocumentAccessLog.created_at.desc(), DocumentAccessLog.id.desc())
    )


# --- Geçerlilik uyarısı (günlük iş) ------------------------------------------------


def _threshold(warning: ExpiryWarning) -> str:
    return {ExpiryWarning.DAYS_60: "60", ExpiryWarning.DAYS_30: "30"}.get(warning, "expired")


def _due(today: date) -> Select[Document]:
    return select(Document).where(
        Document.valid_until.is_not(None),
        Document.valid_until <= today + timedelta(days=WARN_FIRST),
        Document.valid_until >= today - timedelta(days=EXPIRED_NOTICE_DAYS),
    )


async def _responsible(session: AsyncSession, site: Site) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """(belge yöneticileri, bunlardan gizli belge yetkisi olanlar) — aktif kullanıcılar."""
    managers: set[uuid.UUID] = set()
    confidential: set[uuid.UUID] = set()
    for member in await members_svc.list_members(session, site):
        if not (member.is_active and member.user.is_active):
            continue
        permissions = ROLE_PERMISSIONS[member.role]
        if Permission.DOCUMENTS_MANAGE in permissions:
            managers.add(member.user.id)
            if Permission.DOCUMENTS_CONFIDENTIAL in permissions:
                confidential.add(member.user.id)
    return managers, confidential


async def warn_expiring(session: AsyncSession, site: Site, *, today: date) -> int:
    """Sitedeki süreli belgeler için uyarı; yazılan bildirim sayısı (tekrarlar sayılmaz)."""
    documents = list(await session.scalars(_due(today)))
    if not documents:
        return 0
    managers, confidential = await _responsible(session, site)
    written = 0
    for document in documents:
        left = days_left(document.valid_until, today)
        warning = expiry_warning(left)
        if warning is None or left is None:
            continue
        recipients = (
            confidential if document.confidentiality == Confidentiality.SECRET.value else managers
        )
        written += await notifications.notify(
            session,
            recipients,
            notifications.Message(
                category=NotificationCategory.OPERATIONS,
                kind="document.expired"
                if warning is ExpiryWarning.EXPIRED
                else "document.expiring",
                title=expiry_message(document.title, warning, left),
                body=f"Geçerlilik bitişi: {document.valid_until:%d.%m.%Y}.",
                link_entity="documents",
                link_id=document.id,
            ),
            dedupe_key=(
                f"document.expiry:{document.id}:{document.valid_until}:{_threshold(warning)}"
            ),
        )
    return written


async def run_expiry_warnings(
    factory: async_sessionmaker[AsyncSession], *, today: date
) -> list[tuple[uuid.UUID, int]]:
    """Günlük iş (Yazılım Dokümanı 4.6 "Belge geçerlilik bitişi"): site başına ayrı
    transaction — bir sitedeki hata diğerini durdurmaz. Tekrar güvenli."""
    with all_sites_scope():
        async with factory() as session:
            site_ids = sorted(
                set(await session.scalars(_due(today).with_only_columns(Document.site_id)))
            )
    set_actor(Actor(None, JOB_ACTOR, None))
    results: list[tuple[uuid.UUID, int]] = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                site = await session.get(Site, site_id)
                if site is None:  # pragma: no cover
                    continue
                results.append((site_id, await warn_expiring(session, site, today=today)))
    return results
