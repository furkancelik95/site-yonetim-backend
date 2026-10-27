"""Anahtar, kart ve emanet anahtar — Yazılım Dokümanı OPS-01 (servis isteği 53). Açık site
kapsamında; transaction'ı çağıran yönetir.

- Anahtar kodu site içinde benzersiz (Türkçe büyük harf). Anahtar aynı anda tek kişide: açık teslim
  tekil kısmi indeksle korunur (eşzamanlı iki teslimden ikincisi `key_out`).
- `authorized_roles` doluysa teslim yalnız o rollere (`not_authorized`).
- Emanet anahtar yalnız sakinin yetkili listesindeki kişiye (ad, Türkçe harf duyarsız) ve kimlik
  kontrolü işaretlenince verilir; süresi geçmişse verilmez. Verilince sakine bildirim; liste dışı
  kişi için reddedilir ve sakine bilgi gider (red bildirimi işlem geri alınsa da yazılır — uç
  bildirimi kaydedip sonra hatayı döner).
- İade gecikmesi saatlik işle yöneticilere bildirilir (teslim alan kişinin hesabı olmayabilir).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.facility import (
    KEY_ROLES,
    DepositStatus,
    KeyKind,
    OpsError,
    clean,
    conflict,
    deposit_status,
    same_name,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.text import tr_upper
from site_yonetim.domain.validation import normalize_tr_mobile
from site_yonetim.models import DepositedKey, FacilityKey, KeyLoan, Site
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import notifications
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.facility_common import holders

AUTHORIZED_MAX = 10
JOB_ACTOR = "Anahtar iade gecikmesi işi"


@dataclass(frozen=True, slots=True)
class KeyData:
    kind: str | None
    code: str | None
    name: str | None
    area: str | None
    authorized_roles: list[str] | None
    is_active: bool | None


async def save_key(
    session: AsyncSession, data: KeyData, current: FacilityKey | None
) -> FacilityKey:
    errors: dict[str, str] = {}
    name = clean(data.name, 60)
    if len(name) < 2:
        errors["name"] = "Ad yazın (ör. Toplantı salonu)."
    code = tr_upper(clean(data.code, 20))
    if not code:
        errors["code"] = "Kod yazın."
    else:
        taken = await session.scalar(
            select(FacilityKey.id).where(
                FacilityKey.code == code,
                FacilityKey.id != (current.id if current else uuid.UUID(int=0)),
            )
        )
        if taken is not None:
            errors["code"] = "Bu kodla anahtar var."
    if data.kind not in {k.value for k in KeyKind}:
        errors["kind"] = "Tip seçin."
    roles = list(dict.fromkeys(data.authorized_roles or []))
    if any(r not in KEY_ROLES for r in roles):
        errors["authorized_roles"] = "Yetkili rolleri listeden seçin."
    if errors:
        raise FormFieldError(errors)
    key = current or FacilityKey(id=uuid.uuid7())
    key.kind = str(data.kind)
    key.code = code
    key.name = name
    key.area = clean(data.area, 60) or None
    key.authorized_roles = roles
    key.is_active = True if data.is_active is None else data.is_active
    if current is None:
        session.add(key)
    await session.flush()
    return key


async def get_key(
    session: AsyncSession, key_id: uuid.UUID, *, lock: bool = False
) -> FacilityKey | None:
    query = select(FacilityKey).where(FacilityKey.id == key_id)
    found: FacilityKey | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def keys_with_loans(session: AsyncSession) -> list[tuple[FacilityKey, KeyLoan | None]]:
    keys = list(await session.scalars(select(FacilityKey).order_by(FacilityKey.code)))
    open_loans = {
        loan.key_id: loan
        for loan in await session.scalars(select(KeyLoan).where(KeyLoan.returned_at.is_(None)))
    }
    return [(k, open_loans.get(k.id)) for k in keys]


async def give(
    session: AsyncSession,
    key: FacilityKey,
    *,
    to_name: str | None,
    to_phone: str | None,
    role: str | None,
    due_at: datetime | None,
    by: str,
    now: datetime,
) -> KeyLoan:
    """`key` kilitli gelmeli (`get_key(lock=True)`)."""
    if not key.is_active:
        raise conflict("key_inactive", "Bu anahtar kullanım dışı.")
    if await session.scalar(
        select(KeyLoan.id).where(KeyLoan.key_id == key.id, KeyLoan.returned_at.is_(None))
    ):
        raise conflict("key_out", "Anahtar şu an başkasında; önce iadesini kaydedin.")
    errors: dict[str, str] = {}
    name = clean(to_name, 80)
    if len(name) < 3:
        errors["to_name"] = "Teslim alanın adını yazın."
    phone: str | None = None
    if to_phone and to_phone.strip():
        try:
            phone = normalize_tr_mobile(to_phone)
        except ValueError as exc:
            errors["to_phone"] = str(exc)
    if due_at is None or due_at <= now:
        errors["due_at"] = "İade zamanı ileri bir tarih olmalı."
    if errors or due_at is None:
        raise FormFieldError(errors)
    who = clean(role, 40) or None
    if key.authorized_roles and who not in key.authorized_roles:
        raise conflict(
            "not_authorized",
            f"Bu kişi {key.name} için yetkili değil (yetkili: {', '.join(key.authorized_roles)}).",
        )
    loan = KeyLoan(
        id=uuid.uuid7(),
        key_id=key.id,
        to_name=name,
        to_phone=phone,
        role=who,
        due_at=due_at,
        given_by_name=by,
    )
    session.add(loan)
    await session.flush()
    return loan


async def get_loan(session: AsyncSession, loan_id: uuid.UUID) -> KeyLoan | None:
    found: KeyLoan | None = await session.scalar(
        select(KeyLoan).where(KeyLoan.id == loan_id).with_for_update()
    )
    return found


async def return_loan(session: AsyncSession, loan: KeyLoan, *, by: str, now: datetime) -> KeyLoan:
    if loan.returned_at is not None:
        raise conflict("already_returned", "İade zaten kaydedilmiş.")
    loan.returned_at = now
    loan.returned_to_name = by
    await session.flush()
    return loan


async def open_loan(session: AsyncSession, key_id: uuid.UUID) -> KeyLoan | None:
    found: KeyLoan | None = await session.scalar(
        select(KeyLoan).where(KeyLoan.key_id == key_id, KeyLoan.returned_at.is_(None))
    )
    return found


def loans_query() -> Select[KeyLoan]:
    return select(KeyLoan).order_by(KeyLoan.created_at.desc(), KeyLoan.id.desc())


async def key_names(session: AsyncSession, key_ids: set[uuid.UUID]) -> dict[uuid.UUID, FacilityKey]:
    if not key_ids:
        return {}
    return {
        k.id: k
        for k in await session.scalars(select(FacilityKey).where(FacilityKey.id.in_(key_ids)))
    }


# --- Emanet anahtar ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Authorized:
    name: str | None
    phone: str | None


async def deposit(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    person_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    by: str,
    authorized: list[Authorized],
    valid_until: date | None,
    note: str | None,
    today: date,
) -> DepositedKey:
    errors: dict[str, str] = {}
    people: list[dict[str, str | None]] = []
    for item in authorized[:AUTHORIZED_MAX]:
        name = clean(item.name, 80)
        if not name:
            continue
        phone: str | None = None
        if item.phone and item.phone.strip():
            try:
                phone = normalize_tr_mobile(item.phone)
            except ValueError as exc:
                errors["authorized"] = f"{name}: {exc}"
        people.append({"name": name, "phone": phone})
    if not people:
        errors["authorized"] = "En az bir yetkili kişi yazın."
    if valid_until is None or valid_until < today:
        errors["valid_until"] = "Geçerlilik tarihi bugünden önce olamaz."
    if errors or valid_until is None:
        raise FormFieldError(errors)
    record = DepositedKey(
        id=uuid.uuid7(),
        unit_id=unit_id,
        deposited_by_person_id=person_id,
        deposited_by_user_id=user_id,
        deposited_by_name=by,
        authorized=people,
        valid_until=valid_until,
        note=clean(note, 300) or None,
        status=DepositStatus.REQUESTED.value,
    )
    session.add(record)
    await session.flush()
    return record


async def get_deposit(session: AsyncSession, deposit_id: uuid.UUID) -> DepositedKey | None:
    found: DepositedKey | None = await session.scalar(
        select(DepositedKey).where(DepositedKey.id == deposit_id).with_for_update()
    )
    return found


async def receive(session: AsyncSession, record: DepositedKey, *, by: str, now: datetime) -> None:
    if record.status != DepositStatus.REQUESTED.value:
        raise conflict("invalid_status", "Anahtar zaten teslim alınmış.")
    record.status = DepositStatus.HELD.value
    record.received_by_name = by
    record.received_at = now
    await session.flush()


async def _tell_resident(
    session: AsyncSession, record: DepositedKey, kind: str, title: str, key: str
) -> None:
    if record.deposited_by_user_id is None:
        return
    await notifications.notify(
        session,
        [record.deposited_by_user_id],
        notifications.Message(
            category=NotificationCategory.SECURITY,
            kind=kind,
            title=title,
            link_entity="deposited_keys",
            link_id=record.id,
        ),
        dedupe_key=f"deposited_key:{record.id}:{key}",
    )


class NotAuthorizedPersonError(OpsError):
    """Liste dışı kişi — uç, sakine giden bilgiyi kaydedip 409 döner."""


async def hand(
    session: AsyncSession,
    record: DepositedKey,
    *,
    name: str | None,
    id_checked: bool,
    by: str,
    today: date,
    now: datetime,
) -> str:
    status = deposit_status(record.status, record.valid_until, today)
    if status is DepositStatus.EXPIRED:
        raise conflict("expired", "Emanet süresi dolmuş; sakinle görüşün.")
    if status is not DepositStatus.HELD:
        raise conflict("invalid_status", "Anahtar emanette değil.")
    person = next((p for p in record.authorized if same_name(str(p["name"]), name or "")), None)
    if person is None:
        shown = clean(name, 80) or "adı yazılmayan kişi"
        await _tell_resident(
            session,
            record,
            "deposited_key.refused",
            f"Emanet anahtarınız {shown} kişisine verilmedi (yetkili listesinde değil).",
            f"refused:{now.isoformat()}",
        )
        raise NotAuthorizedPersonError(
            409,
            "not_authorized",
            "Bu kişi yetkili listesinde değil; anahtar verilmez, sakine bilgi gider.",
        )
    if not id_checked:
        raise FormFieldError({"id_checked": "Kimlik kontrolünü işaretleyin."})
    record.status = DepositStatus.HANDED.value
    record.handed_to = str(person["name"])
    record.handed_by_name = by
    record.handed_at = now
    await session.flush()
    await _tell_resident(
        session,
        record,
        "deposited_key.handed",
        f"Anahtarınız {record.handed_to} kişisine verildi.",
        "handed",
    )
    return str(person["name"])


async def give_back(session: AsyncSession, record: DepositedKey, *, now: datetime) -> None:
    if record.status not in (DepositStatus.HELD.value, DepositStatus.REQUESTED.value):
        raise conflict("invalid_status", "Anahtar emanette değil.")
    record.status = DepositStatus.RETURNED.value
    record.returned_at = now
    await session.flush()


def deposits_query() -> Select[DepositedKey]:
    return select(DepositedKey).order_by(DepositedKey.created_at.desc(), DepositedKey.id.desc())


# --- İade gecikmesi (saatlik iş) ------------------------------------------------------


async def warn_overdue(session: AsyncSession, site: Site, *, now: datetime) -> int:
    late = list(
        await session.scalars(
            select(KeyLoan).where(KeyLoan.returned_at.is_(None), KeyLoan.due_at < now)
        )
    )
    if not late:
        return 0
    keys = await key_names(session, {loan.key_id for loan in late})
    recipients = await holders(session, site, Permission.REQUESTS_ASSIGN)
    written = 0
    for loan in late:
        key = keys.get(loan.key_id)
        written += await notifications.notify(
            session,
            recipients,
            notifications.Message(
                category=NotificationCategory.OPERATIONS,
                kind="key.overdue",
                title=f"{key.name if key else 'Anahtar'} iadesi gecikti — {loan.to_name}",
                body=f"İade zamanı: {loan.due_at.astimezone(BUSINESS_TZ):%d.%m.%Y %H:%M}.",
                link_entity="key_loans",
                link_id=loan.id,
            ),
            dedupe_key=f"key_loan.overdue:{loan.id}",
        )
    return written


async def run_overdue_warnings(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> list[tuple[uuid.UUID, int]]:
    with all_sites_scope():
        async with factory() as session:
            site_ids: list[uuid.UUID] = sorted(
                set(
                    await session.scalars(
                        select(func.distinct(KeyLoan.site_id)).where(
                            KeyLoan.returned_at.is_(None), KeyLoan.due_at < now
                        )
                    )
                )
            )
    set_actor(Actor(None, JOB_ACTOR, None))
    results: list[tuple[uuid.UUID, int]] = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                site = await session.get(Site, site_id)
                if site is None:  # pragma: no cover
                    continue
                results.append((site_id, await warn_overdue(session, site, now=now)))
    return results
