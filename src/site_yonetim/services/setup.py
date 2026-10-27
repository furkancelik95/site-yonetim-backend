"""Tesis kurulum durumu ve canlıya geçiş kontrol listesi (servis isteği 26, YAP-01).

Liste her çağrıda gerçek veriden hesaplanır: yapı özeti (istek 19), kasa hesapları, otomatik
tahakkuk, aktarım geçmişi, üyeler, onay kuralları, kayıt ayarları. `ready` = zorunlu maddelerin
hepsi tamam. Canlıya alma geri alınmaz; denetim kaydına yazılır.

Finans modu yalnız `platform` (Mod K / KiviERP ortak kararı A-01 bekliyor). İki adımlı doğrulama
henüz yok: madde `ok=false`, zorunlu değil.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.access import Permission
from site_yonetim.domain.invitations import SiteLifecycle
from site_yonetim.models import (
    ApprovalRule,
    CashAccount,
    ChargeSchedule,
    ImportBatch,
    Site,
)
from site_yonetim.services import roles as roles_svc
from site_yonetim.services.structure import structure_summary
from site_yonetim.services.vehicles import read_settings

FINANCE_MODE: Literal["platform"] = "platform"
LIST_MAX = 5


class SetupRuleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    key: str
    label: str
    ok: bool
    detail: str
    link: str | None
    required: bool


def _names(items: list[str]) -> str:
    shown = ", ".join(items[:LIST_MAX])
    return shown + ("…" if len(items) > LIST_MAX else "")


async def _manager_count(session: AsyncSession, site: Site) -> int:
    """Kullanıcı yönetme izni (`members.manage`) olan etkin kişiler: açık rol ataması ya da
    yönetim şirketinden gelen erişim (rol adına değil izne bakılır — P18)."""
    return len(await roles_svc.permission_holders(session, site, Permission.MEMBERS_MANAGE))


async def checklist(session: AsyncSession, site: Site, today: date) -> list[ChecklistItem]:
    summary = await structure_summary(session, today)
    config = await read_settings(session)
    bank = await session.scalar(
        select(func.count())
        .select_from(CashAccount)
        .where(CashAccount.kind == "bank", CashAccount.iban.is_not(None), CashAccount.is_active)
    )
    schedule = await session.scalar(select(ChargeSchedule.enabled).limit(1))
    opening_import = await session.scalar(
        select(func.count())
        .select_from(ImportBatch)
        .where(ImportBatch.type == "opening_balances", ImportBatch.status == "imported")
    )
    rules = await session.scalar(
        select(func.count()).select_from(ApprovalRule).where(ApprovalRule.is_active)
    )
    managers = await _manager_count(session, site)
    no_owner = [u.display_name for u in summary.units_without_owner]
    gaps = [
        f"{u.display_name} (%{u.total:.2f})".replace(".", ",")
        for u in summary.units_shares_incomplete
    ]
    methods = [
        name
        for name, on in (
            ("davet", config.invite_enabled),
            ("QR", config.qr_enabled),
            ("site kodu", config.code_enabled),
        )
        if on
    ]
    if config.opening_reconciled_at is not None:
        opening_detail = f"Mutabakat işaretlendi ({config.opening_reconciled_by or 'yönetici'})"
    elif opening_import:
        opening_detail = "Açılış bakiyesi aktarımı eski sistem toplamıyla tutturuldu"
    else:
        opening_detail = "Eski sistemdeki bakiyeleri aktarın ya da mutabakatı işaretleyin"
    land_detail = (
        f"{summary.land_share_sum} / {summary.land_share_denominator}"
        if summary.land_share_denominator
        else "Arsa payı girilmemiş"
    )
    if summary.units_without_land_share and summary.land_share_denominator:
        land_detail += f" · {summary.units_without_land_share} bölümde arsa payı yok"
    return [
        ChecklistItem(
            "structure",
            "Yapı tanımlı (blok ve bölümler)",
            summary.block_count > 0 and summary.active_unit_count > 0,
            f"{summary.block_count} blok, {summary.active_unit_count} bölüm",
            "daireler",
            True,
        ),
        ChecklistItem(
            "owners",
            "Her bölümün en az bir maliki var",
            not no_owner and summary.active_unit_count > 0,
            f"{len(no_owner)} bölümde malik yok: {_names(no_owner)}" if no_owner else "Tamam",
            "daireler",
            True,
        ),
        ChecklistItem(
            "shares",
            "Malik hisseleri tam %100",
            not gaps,
            f"{len(gaps)} bölümde hisse toplamı %100 değil: {_names(gaps)}" if gaps else "Tamam",
            "daireler",
            True,
        ),
        ChecklistItem(
            "land_share",
            "Arsa payları toplamı paydaya eşit",
            not summary.land_share_mismatch
            and summary.units_without_land_share == 0
            and summary.land_share_denominator is not None,
            land_detail,
            "daireler",
            False,
        ),
        ChecklistItem(
            "bank",
            "Site adına banka hesabı (IBAN) tanımlı",
            bool(bank),
            "Tamam" if bank else "Kasa ve Banka'dan IBAN'lı banka hesabı ekleyin",
            "kasa",
            True,
        ),
        ChecklistItem(
            "charge_rule",
            "Aidat kuralı ve otomatik tahakkuk tanımlı",
            bool(schedule),
            "Otomatik aylık tahakkuk açık"
            if schedule
            else "İşletme projesi kesinleştirilip otomatik tahakkuk açılmalı",
            "tahakkuk",
            True,
        ),
        ChecklistItem(
            "opening",
            "Açılış bakiyesi mutabakatı yapıldı",
            bool(opening_import) or config.opening_reconciled_at is not None,
            opening_detail,
            "iceri-aktar",
            True,
        ),
        ChecklistItem(
            "manager",
            "En az bir etkin site yöneticisi",
            managers > 0,
            f"{managers} etkin site yöneticisi",
            "kullanicilar",
            True,
        ),
        ChecklistItem(
            "mfa",
            "Site yöneticisi iki adımlı doğrulama kurdu",
            False,
            "İki adımlı doğrulama henüz yok (P5: SMS sağlayıcısı seçilince eklenecek)",
            None,
            False,
        ),
        ChecklistItem(
            "kvkk",
            "KVKK aydınlatma metni yayında",
            config.kvkk_published,
            "Yayında"
            if config.kvkk_published
            else "Hukuk onayı bekliyor (K8); Kayıt yöntemleri'nden işaretlenir",
            "kayit-basvurulari",
            True,
        ),
        ChecklistItem(
            "approvals",
            "Onay kuralları gözden geçirildi",
            bool(rules),
            f"{rules or 0} yürürlükte kural",
            "onay-kurallari",
            False,
        ),
        ChecklistItem(
            "registration",
            "Kayıt yöntemleri ayarlandı",
            bool(methods),
            ", ".join(methods) if methods else "Hiçbir yöntem açık değil",
            "kayit-basvurulari",
            False,
        ),
    ]


def is_ready(items: list[ChecklistItem]) -> bool:
    return all(i.ok for i in items if i.required)


async def go_live(session: AsyncSession, site: Site, *, today: date, now: datetime) -> None:
    if site.lifecycle_status == SiteLifecycle.LIVE.value:
        raise SetupRuleError("already_live", "Tesis zaten canlı.")
    missing = [i.label for i in await checklist(session, site, today) if i.required and not i.ok]
    if missing:
        raise SetupRuleError("checklist_incomplete", "Eksik maddeler var: " + "; ".join(missing))
    site.lifecycle_status = SiteLifecycle.LIVE.value
    site.went_live_at = now
    await session.flush()
