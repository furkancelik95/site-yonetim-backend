"""Sigorta poliçeleri ve hasar dosyaları — Yazılım Dokümanı OPS-04 (servis isteği 56). Açık site
kapsamında; transaction'ı çağıran yönetir.

- Tür, sigortacı, poliçe no zorunlu; bitiş başlangıçtan sonra; prim ≥ 0.
- Bitişe 60 ve 30 gün kala ve bitince yöneticilere (gider yönetimi izni) bildirim — günlük iş,
  her eşik bir kez (tekil anahtar: poliçe + bitiş + eşik; bitiş değişirse uyarılar yeniden başlar).
- Hasar dosyası: açık → sigortacıya iletildi → tazminat ödendi / reddedildi. Ödendi → tutar ve
  tarih zorunlu. Tazminatın muhasebe fişine (Mod P gelir kaydı) aktarımı muhasebe aktarımıyla
  gelecek (istek 32) — burada yalnız kayıt.
- Poliçe ve hasar belgeleri belge arşivine bağlanır (`subject_documents`).
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.facility import (
    CLAIM_TRANSITIONS,
    POLICY_EXPIRED_NOTICE_DAYS,
    ClaimStatus,
    PolicyKind,
    PolicyWarning,
    clean,
    conflict,
    policy_warning,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.models import InsuranceClaim, InsurancePolicy, Site
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import notifications
from site_yonetim.services.facility_common import holders

JOB_ACTOR = "Poliçe bitiş uyarısı işi"
POLICY_KIND_LABELS = {
    PolicyKind.BUILDING: "Bina (deprem/yangın)",
    PolicyKind.LIABILITY: "Üçüncü şahıs sorumluluk",
    PolicyKind.ELEVATOR: "Asansör",
    PolicyKind.PERSONNEL: "Personel kaza",
    PolicyKind.GLASS: "Cam kırılması",
    PolicyKind.OTHER: "Diğer",
}


@dataclass(frozen=True, slots=True)
class PolicyData:
    kind: str | None
    insurer: str | None
    policy_no: str | None
    coverage: str | None
    premium: Decimal | None
    start: date | None
    end: date | None
    area: str | None
    note: str | None


def check_policy(data: PolicyData) -> PolicyData:
    errors: dict[str, str] = {}
    if data.kind not in {k.value for k in PolicyKind}:
        errors["kind"] = "Tip seçin."
    insurer = clean(data.insurer, 120)
    if len(insurer) < 2:
        errors["insurer"] = "Sigortacıyı yazın."
    number = clean(data.policy_no, 60)
    if not number:
        errors["policy_no"] = "Poliçe no yazın."
    if data.premium is None or data.premium < 0:
        errors["premium"] = "Primi girin (sıfır ya da daha büyük)."
    if data.start is None:
        errors["start"] = "Başlangıç tarihini seçin."
    if data.end is None or (data.start is not None and data.end <= data.start):
        errors["end"] = "Bitiş başlangıçtan sonra olmalı."
    if errors:
        raise FormFieldError(errors)
    return PolicyData(
        kind=data.kind,
        insurer=insurer,
        policy_no=number,
        coverage=clean(data.coverage, 300) or None,
        premium=data.premium,
        start=data.start,
        end=data.end,
        area=clean(data.area, 120) or None,
        note=clean(data.note, 1000) or None,
    )


def current_policy(p: InsurancePolicy) -> PolicyData:
    return PolicyData(
        kind=p.kind,
        insurer=p.insurer,
        policy_no=p.policy_no,
        coverage=p.coverage,
        premium=p.premium,
        start=p.start_date,
        end=p.end_date,
        area=p.area,
        note=p.note,
    )


async def save_policy(
    session: AsyncSession, data: PolicyData, current: InsurancePolicy | None
) -> InsurancePolicy:
    clean_data = check_policy(data)
    policy = current or InsurancePolicy(id=uuid.uuid7())
    policy.kind = str(clean_data.kind)
    policy.insurer = str(clean_data.insurer)
    policy.policy_no = str(clean_data.policy_no)
    policy.coverage = clean_data.coverage
    policy.premium = clean_data.premium or Decimal(0)
    policy.start_date = clean_data.start or date.min
    policy.end_date = clean_data.end or date.min
    policy.area = clean_data.area
    policy.note = clean_data.note
    if current is None:
        session.add(policy)
    await session.flush()
    return policy


async def get_policy(session: AsyncSession, policy_id: uuid.UUID) -> InsurancePolicy | None:
    found: InsurancePolicy | None = await session.scalar(
        select(InsurancePolicy).where(InsurancePolicy.id == policy_id).with_for_update()
    )
    return found


def policies_query() -> Select[InsurancePolicy]:
    return select(InsurancePolicy).order_by(InsurancePolicy.end_date, InsurancePolicy.id)


def policy_label(p: InsurancePolicy) -> str:
    return f"{POLICY_KIND_LABELS[PolicyKind(p.kind)]} · {p.insurer} · {p.policy_no}"


async def open_claim(
    session: AsyncSession,
    *,
    policy: InsurancePolicy | None,
    source: str | None,
    description: str | None,
    incident_date: date | None,
    claimed: Decimal | None,
    by: str,
    today: date,
) -> InsuranceClaim:
    errors: dict[str, str] = {}
    if policy is None:
        errors["policy_id"] = "Poliçe seçin."
    text = clean(description, 300)
    if len(text) < 5:
        errors["description"] = "Hasarı kısaca anlatın."
    if incident_date is None or incident_date > today:
        errors["incident_date"] = "Hasar tarihi (bugün ya da önce)."
    if claimed is not None and claimed < 0:
        errors["claimed"] = "Talep edilen tutar sıfır ya da daha büyük olmalı."
    if errors or policy is None or incident_date is None:
        raise FormFieldError(errors)
    claim = InsuranceClaim(
        id=uuid.uuid7(),
        policy_id=policy.id,
        source=clean(source, 120) or None,
        description=text,
        incident_date=incident_date,
        status=ClaimStatus.OPEN.value,
        claimed=claimed,
        created_by_name=by,
    )
    session.add(claim)
    await session.flush()
    return claim


async def get_claim(session: AsyncSession, claim_id: uuid.UUID) -> InsuranceClaim | None:
    found: InsuranceClaim | None = await session.scalar(
        select(InsuranceClaim).where(InsuranceClaim.id == claim_id).with_for_update()
    )
    return found


async def update_claim(
    session: AsyncSession,
    claim: InsuranceClaim,
    *,
    status: str | None,
    paid: Decimal | None,
    paid_on: date | None,
    today: date,
) -> InsuranceClaim:
    if status is None:
        return claim
    if status not in {s.value for s in ClaimStatus}:
        raise FormFieldError({"status": "Durum seçin."})
    target, now_status = ClaimStatus(status), ClaimStatus(claim.status)
    if target is now_status:
        return claim
    if target not in CLAIM_TRANSITIONS[now_status]:
        raise conflict("invalid_status", "Hasar dosyası bu duruma geçirilemez.")
    if target is ClaimStatus.PAID:
        errors: dict[str, str] = {}
        if paid is None or paid <= 0:
            errors["paid"] = "Ödenen tazminatı girin."
        if paid_on is not None and paid_on > today:
            errors["paid_on"] = "Ödeme tarihi bugünden sonra olamaz."
        if errors:
            raise FormFieldError(errors)
        claim.paid = paid
        claim.paid_on = paid_on or today
    claim.status = target.value
    await session.flush()
    return claim


def claims_query() -> Select[InsuranceClaim]:
    return select(InsuranceClaim).order_by(
        InsuranceClaim.created_at.desc(), InsuranceClaim.id.desc()
    )


async def policies_by_id(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, InsurancePolicy]:
    if not ids:
        return {}
    rows = await session.scalars(select(InsurancePolicy).where(InsurancePolicy.id.in_(ids)))
    return {p.id: p for p in rows}


# --- Bitiş uyarısı (günlük iş) -------------------------------------------------------


def _due(today: date) -> Select[InsurancePolicy]:
    return select(InsurancePolicy).where(
        InsurancePolicy.end_date <= today + timedelta(days=60),
        InsurancePolicy.end_date >= today - timedelta(days=POLICY_EXPIRED_NOTICE_DAYS),
    )


async def warn_expiring(session: AsyncSession, site: Site, *, today: date) -> int:
    policies = list(await session.scalars(_due(today)))
    if not policies:
        return 0
    recipients = await holders(session, site, Permission.EXPENSES_MANAGE)
    written = 0
    for policy in policies:
        left = (policy.end_date - today).days
        warning = policy_warning(left)
        if warning is None:
            continue
        title = (
            f"Poliçenin süresi doldu: {policy_label(policy)}"
            if warning is PolicyWarning.EXPIRED
            else f"Poliçe bitişine {left} gün kaldı: {policy_label(policy)}"
        )
        written += await notifications.notify(
            session,
            recipients,
            notifications.Message(
                category=NotificationCategory.OPERATIONS,
                kind="insurance.expired"
                if warning is PolicyWarning.EXPIRED
                else "insurance.expiring",
                title=title,
                body=f"Bitiş: {policy.end_date:%d.%m.%Y}. Yenileme için sigortacıyla görüşün.",
                link_entity="insurance_policies",
                link_id=policy.id,
            ),
            dedupe_key=f"insurance.expiry:{policy.id}:{policy.end_date}:{warning.value}",
        )
    return written


async def run_expiry_warnings(
    factory: async_sessionmaker[AsyncSession], *, today: date
) -> list[tuple[uuid.UUID, int]]:
    with all_sites_scope():
        async with factory() as session:
            site_ids: list[uuid.UUID] = sorted(
                set(
                    await session.scalars(
                        _due(today).with_only_columns(func.distinct(InsurancePolicy.site_id))
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
                results.append((site_id, await warn_expiring(session, site, today=today)))
    return results
