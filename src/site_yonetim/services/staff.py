"""Site personeli — frontend servis isteği 18. Açık site kapsamında; transaction'ı çağıran
yönetir.

- **KVKK veri minimizasyonu:** yalnız iş için gerekenler; T.C. kimlik no, maaş, bordro, adres,
  sağlık bilgisi tutulmaz (bordro: açık karar K7).
- Silinmez; ayrılış tarihi girilir. Ayrılanın verisinin saklama süresi açık karar K8.
- Personel kaydı hesap açmaz; giriş yetkisi Kullanıcılar ekranından (istek 12).
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.management import Employer
from site_yonetim.domain.members import FormFieldError, check_full_name
from site_yonetim.domain.validation import normalize_tr_mobile
from site_yonetim.models import StaffMember

NAME_MAX = 60


def _clean(value: str | None, limit: int = 120) -> str:
    return " ".join((value or "").split())[:limit]


@dataclass(frozen=True, slots=True)
class StaffData:
    full_name: str | None
    position: str | None
    employer: str | None
    contractor_name: str | None
    phone: str | None
    start_date: date | None
    end_date: date | None
    shift: str | None


def check(data: StaffData) -> StaffData:
    errors: dict[str, str] = {}
    name = ""
    try:
        name = check_full_name(data.full_name)
    except ValueError as exc:
        errors["full_name"] = str(exc)
    if len(name) > NAME_MAX:
        errors["full_name"] = f"Ad soyad en çok {NAME_MAX} karakter olmalı."
    position = _clean(data.position)
    if len(position) < 2:
        errors["position"] = "Görevini yazın."
    if data.employer not in {e.value for e in Employer}:
        errors["employer"] = "Kadroyu seçin."
    contractor = _clean(data.contractor_name, 200) or None
    if data.employer == Employer.CONTRACTOR.value and contractor is None:
        errors["contractor_name"] = "Taşeron firmanın adını yazın."
    if data.employer == Employer.SITE.value:
        contractor = None
    phone = None
    if (data.phone or "").strip():
        try:
            phone = normalize_tr_mobile(data.phone or "")
        except ValueError as exc:
            errors["phone"] = str(exc)
    if data.start_date is None:
        errors["start_date"] = "İşe başlama tarihini seçin."
    elif data.end_date is not None and data.end_date < data.start_date:
        errors["end_date"] = "Ayrılış tarihi başlangıçtan önce olamaz."
    if errors:
        raise FormFieldError(errors)
    return StaffData(
        full_name=name,
        position=position,
        employer=data.employer,
        contractor_name=contractor,
        phone=phone,
        start_date=data.start_date,
        end_date=data.end_date,
        shift=_clean(data.shift) or None,
    )


def current(item: StaffMember) -> StaffData:
    return StaffData(
        full_name=item.full_name,
        position=item.position,
        employer=item.employer,
        contractor_name=item.contractor_name,
        phone=item.phone,
        start_date=item.start_date,
        end_date=item.end_date,
        shift=item.shift,
    )


def _apply(item: StaffMember, data: StaffData) -> None:
    item.full_name = data.full_name or ""
    item.position = data.position or ""
    item.employer = data.employer or ""
    item.contractor_name = data.contractor_name
    item.phone = data.phone
    item.start_date = data.start_date or date.min
    item.end_date = data.end_date
    item.shift = data.shift


async def create(session: AsyncSession, data: StaffData) -> StaffMember:
    item = StaffMember(id=uuid.uuid7())
    _apply(item, check(data))
    session.add(item)
    await session.flush()
    return item


async def update(session: AsyncSession, item: StaffMember, data: StaffData) -> StaffMember:
    _apply(item, check(data))
    await session.flush()
    return item


async def get(session: AsyncSession, staff_id: uuid.UUID) -> StaffMember | None:
    found: StaffMember | None = await session.scalar(
        select(StaffMember).where(StaffMember.id == staff_id).with_for_update()
    )
    return found


def list_query(active: bool | None, today: date) -> Select[StaffMember]:
    query = select(StaffMember)
    working = or_(StaffMember.end_date.is_(None), StaffMember.end_date >= today)
    if active is True:
        query = query.where(working)
    elif active is False:
        query = query.where(StaffMember.end_date < today)
    return query.order_by(StaffMember.full_name, StaffMember.id)
