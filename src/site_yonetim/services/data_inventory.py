"""VERBİS için kişisel veri envanteri dökümü (Yazılım Dokümanı RAP-04 `GET …/data-inventory`,
§18.2). Site = veri sorumlusu, platform = veri işleyen (Ürün Dokümanı §11).

Satırlar sabit envanter (platformun tuttuğu kişisel veri grupları) + sitenin özel alanları
(her özel alan bir KVKK kategorisiyle kaydedilir, S-05). Kayıt sayıları veritabanında sayılır.
Amaç ve hukuki sebep metinleri öneridir; VERBİS kaydı öncesi hukukçu tarafından gözden
geçirilmelidir (A-04).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.base import Base
from site_yonetim.domain.retention import ACTION_LABELS, BY_KEY, UNIT_LABELS, RetentionCategory
from site_yonetim.models import (
    AuditLog,
    CustomField,
    HouseholdMember,
    LedgerAccount,
    Person,
    Registration,
    SiteMembership,
    StaffMember,
    Vehicle,
    Visitor,
)
from site_yonetim.services import retention as retention_svc

CATEGORY_LABELS = {
    "identity": "Kimlik",
    "contact": "İletişim",
    "finance": "Finans",
    "location_plate": "Konum / plaka",
    "employment": "Özlük",
    "image": "Görsel kayıt",
    "transaction_security": "İşlem güvenliği",
    "other": "Diğer",
}
ENTITY_LABELS = {"unit": "Bölüm kartı", "person": "Kişi kartı"}

_RESIDENTS = "Malikler, kiracılar, hane bireyleri"
_KMK = "KVKK m.5/2-ç (hukuki yükümlülük: 634 sayılı KMK), m.5/2-c (sözleşmenin ifası)"
_NO_TRANSFER = "Aktarım yok; yalnız yetkili kamu kurumlarına kanuni zorunlulukla"


@dataclass(frozen=True, slots=True)
class InventoryRow:
    category: str
    category_label: str
    data: str
    subjects: str
    purpose: str
    legal_basis: str
    retention: str
    recipients: str
    source: str
    records: int


@dataclass(frozen=True, slots=True)
class _Static:
    category: str
    data: str
    subjects: str
    purpose: str
    legal_basis: str
    retention_key: str | None  # domain/retention kategorisi
    retention_text: str | None
    source: str
    model: type[Base]


_STATIC: tuple[_Static, ...] = (
    _Static("identity", "Ad, soyad; malik/kiracı/hane ilişkisi", _RESIDENTS,
            "Kat mülkiyeti yönetimi, aidat ve ortak gider takibi, duyuru ve talep hizmetleri",
            _KMK, "finance", None, "Kişi kartı (persons)", Person),
    _Static("contact", "Cep telefonu, e-posta", _RESIDENTS,
            "Bildirim, duyuru, borç ve talep iletişimi", _KMK, "finance", None,
            "Kişi kartı (persons)", Person),
    _Static("finance", "Cari hesap, tahakkuk, tahsilat, iade, makbuz", "Malikler, kiracılar",
            "Aidat ve ortak gider tahsilatı, muhasebe", "KVKK m.5/2-ç (213 VUK, 6102 TTK)",
            "finance", None, "Cari hesaplar (ledger_accounts)", LedgerAccount),
    _Static("location_plate", "Araç plakası, marka/model, renk", _RESIDENTS,
            "Site güvenliği, otopark yönetimi", "KVKK m.5/2-f (meşru menfaat)", None,
            "Araç pasifleşince hukuki teyide kadar saklanır", "Araçlar (vehicles)", Vehicle),
    _Static("identity", "Ziyaretçi adı, telefonu, plakası", "Ziyaretçiler",
            "Site güvenliği, giriş-çıkış kaydı", "KVKK m.5/2-f (meşru menfaat)", "visitors",
            None, "Ziyaretçi kayıtları (visitors)", Visitor),
    _Static("identity", "Hane bireyi adı, telefonu, yetkileri", "Hane bireyleri",
            "Ziyaretçi/kargo/talep yetkisi", "KVKK m.5/2-c (sözleşmenin ifası)", None,
            "İlişki bitince hukuki teyide kadar saklanır", "Hane (household_members)",
            HouseholdMember),
    _Static("identity", "Başvuranın adı, telefonu, e-postası, IP adresi", "Başvuranlar",
            "Sakin kaydının doğrulanması", "KVKK m.5/2-c (sözleşmenin kurulması)",
            "registrations", None, "Kayıt başvuruları (registrations)", Registration),
    _Static("employment", "Personel kartı, vardiya, izin, puantaj", "Personel",
            "İş ilişkisinin yürütülmesi, puantaj", "KVKK m.5/2-ç, m.5/2-c (4857 İş K.)",
            "employment", None, "Personel (staff_members)", StaffMember),
    _Static("transaction_security", "Kullanıcı hesabı, rol, oturum, IP, denetim kaydı",
            "Kullanıcılar (yönetim, personel, sakin)", "Bilgi güvenliği, yetki ve denetim izi",
            "KVKK m.5/2-ç (5651 s. K., KVKK m.12)", None,
            "Denetim kaydı değiştirilemez; süre sonunda politika ile arşivlenir",
            "Üyelikler ve denetim kaydı (site_memberships, audit_log)", SiteMembership),
)  # fmt: skip


async def _count(session: AsyncSession, model: type[Base]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def inventory(session: AsyncSession, today: date) -> list[InventoryRow]:
    values = {r.category.key: r.value for r in await retention_svc.policy(session, today)}
    rows: list[InventoryRow] = []
    audit_count = await _count(session, AuditLog)
    for item in _STATIC:
        if item.retention_key is not None:
            category = BY_KEY[item.retention_key]
            retention = _retention_label(category, values.get(category.key))
        else:
            retention = item.retention_text or "—"
        records = await _count(session, item.model)
        if item.model is SiteMembership:
            records += audit_count
        rows.append(
            InventoryRow(
                item.category,
                CATEGORY_LABELS[item.category],
                item.data,
                item.subjects,
                item.purpose,
                item.legal_basis,
                retention,
                _NO_TRANSFER,
                item.source,
                records,
            )
        )
    fields = await session.scalars(
        select(CustomField)
        .where(CustomField.is_archived.is_(False))
        .order_by(CustomField.entity, CustomField.sort_order, CustomField.label)
    )
    for f in fields:
        rows.append(
            InventoryRow(
                f.kvkk_category,
                CATEGORY_LABELS.get(f.kvkk_category, f.kvkk_category),
                f"Özel alan: {f.label}",
                _RESIDENTS,
                "Sitenin tanımladığı ek bilgi",
                "Site tarafından belirlenmeli",
                "Bağlı kartla birlikte",
                _NO_TRANSFER,
                ENTITY_LABELS.get(f.entity, f.entity),
                0,
            )
        )
    return rows


def _retention_label(category: RetentionCategory, value: int | None) -> str:
    amount = value if value is not None else category.default_value
    return f"{amount} {UNIT_LABELS[category.unit]}; {ACTION_LABELS[category.action].lower()}"


HEADER: tuple[str, ...] = (
    "KVKK kategorisi", "Veri", "İlgili kişi grubu", "İşleme amacı", "Hukuki sebep",
    "Saklama süresi", "Alıcı / aktarım", "Kaynak", "Kayıt sayısı",
)  # fmt: skip


def as_rows(rows: Sequence[InventoryRow]) -> list[tuple[str | int, ...]]:
    return [
        (r.category_label, r.data, r.subjects, r.purpose, r.legal_basis, r.retention,
         r.recipients, r.source, r.records)
        for r in rows
    ]  # fmt: skip
