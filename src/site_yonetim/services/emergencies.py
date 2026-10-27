"""Acil durum modu, durum panosu ve toplanma alanı yoklaması — Yazılım Dokümanı ILT-04, ILT-06,
16.2, 16.4; servis isteği 73. Açık site kapsamında; commit çağırana ait.

- Aynı anda tek aktif acil durum (kısmi benzersiz dizin; eşzamanlı ikinci başlatma 409).
  Başlatma **onay gerektirmez**, denetim kaydına yazılır.
- Kapsamdaki bölümler başlatma anında sabitlenir (bölüm/blok adı kopyalanır).
- Kapsamdaki herkese uygulama içi **acil** bildirim (`urgent`; tercih ve sessiz saat aşar).
  Push + SMS P16c gönderim katmanı bağlanınca aynı çağrıdan gider.
- Sakin yanıtı kişinin kapsamdaki tüm bölümlerine yazılır; son yanıt geçerlidir. "Yardım
  lazım" panoda en üste çıkar; yardım isteyen bölüm listeden yoklamayla kapanmaz.
- Bitişte özet saklanır; "güvendeyim"/yoklama ayrıntısı `retention_days` gün sonra silinir
  (gece işi; yalnız `auto_purge` açıksa — K8, süre avukat teyidine bağlı, A-04).
"""

import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.communication import (
    EMERGENCY_LABELS,
    MESSAGE_MAX,
    MESSAGE_MIN,
    NOTE_MAX,
    EmergencyKind,
    EmergencyScope,
    SafetyMethod,
    UnitSafety,
    purge_on,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.structure import PartyRole
from site_yonetim.models import Block, Emergency, EmergencyUnit, Site, Unit, UnitParty
from site_yonetim.models.audit import Actor, AuditAction, record, set_actor
from site_yonetim.services import communication as comm_svc
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.gatehouse import unit_user_ids

logger = logging.getLogger(__name__)
_PARTY_ROLES = [PartyRole.OWNER.value, PartyRole.TENANT.value, PartyRole.RESIDENT.value]


async def active(session: AsyncSession, *, lock: bool = False) -> Emergency | None:
    query = select(Emergency).where(Emergency.ended_at.is_(None))
    found: Emergency | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def get(session: AsyncSession, emergency_id: uuid.UUID) -> Emergency | None:
    found: Emergency | None = await session.scalar(
        select(Emergency).where(Emergency.id == emergency_id)
    )
    return found


async def start(
    session: AsyncSession,
    *,
    kind: str | None,
    scope: str | None,
    block_ids: list[uuid.UUID],
    message: str | None,
    by: str,
    now: datetime,
) -> tuple[Emergency, int]:
    """Acil durumu başlatır; (acil durum, bildirim giden kişi sayısı)."""
    if await active(session) is not None:
        raise OperationRuleError("already_active", "Zaten aktif bir acil durum var.", conflict=True)
    errors: dict[str, str] = {}
    text = " ".join((message or "").split())
    if kind not in {k.value for k in EmergencyKind}:
        errors["kind"] = "Tür seçin."
    if not MESSAGE_MIN <= len(text) <= MESSAGE_MAX:
        errors["message"] = f"Kısa bir mesaj yazın ({MESSAGE_MIN}–{MESSAGE_MAX} karakter)."
    chosen = EmergencyScope.BLOCKS if scope == EmergencyScope.BLOCKS.value else EmergencyScope.ALL
    blocks = list(dict.fromkeys(block_ids)) if chosen is EmergencyScope.BLOCKS else []
    if chosen is EmergencyScope.BLOCKS and not blocks:
        errors["block_ids"] = "En az bir blok seçin."
    if errors:
        raise FormFieldError(errors)
    query = (
        select(Unit.id, Unit.number, Block.id, Block.name)
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .where(Unit.is_active)
        .order_by(Block.name, Unit.number)
    )
    if blocks:
        query = query.where(Block.id.in_(blocks))
    units = (await session.execute(query)).all()
    if not units:
        raise FormFieldError({"block_ids": "Kapsamda bölüm yok."})
    emergency = Emergency(
        id=uuid.uuid7(),
        kind=kind,
        scope=chosen.value,
        block_ids=blocks,
        message=text,
        started_by=by,
        started_at=now,
    )
    session.add(emergency)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:  # eşzamanlı ikinci başlatma
        raise OperationRuleError(
            "already_active", "Zaten aktif bir acil durum var.", conflict=True
        ) from exc
    session.add_all(
        EmergencyUnit(
            emergency_id=emergency.id,
            unit_id=unit_id,
            unit_name=f"{block_name}-{number}",
            block_id=block_id,
            block_name=block_name,
        )
        for unit_id, number, block_id, block_name in units
    )
    await session.flush()
    users = await unit_user_ids(session, {u[0] for u in units}, now.astimezone(BUSINESS_TZ).date())
    label = EMERGENCY_LABELS[EmergencyKind(emergency.kind)]
    sent = await notify_svc.notify(
        session,
        users,
        notify_svc.Message(
            category=NotificationCategory.URGENT,
            kind="emergency.started",
            title=f"ACİL DURUM — {label}",
            body=f"{text} Güvende misiniz? Uygulamadan durumunuzu bildirin.",
            link_entity="emergencies",
            link_id=emergency.id,
        ),
        dedupe_key=f"emergency:{emergency.id}",
    )
    return emergency, sent


async def rows(session: AsyncSession, emergency_id: uuid.UUID) -> list[EmergencyUnit]:
    return list(
        await session.scalars(
            select(EmergencyUnit)
            .where(EmergencyUnit.emergency_id == emergency_id)
            .order_by(EmergencyUnit.block_name, EmergencyUnit.unit_name)
        )
    )


def totals(units: list[EmergencyUnit]) -> dict[str, int]:
    counts = Counter(u.status for u in units)
    found = {s.value: counts.get(s.value, 0) for s in UnitSafety}
    found["total"] = len(units)
    return found


async def muster_check(
    session: AsyncSession,
    emergency: Emergency,
    *,
    unit_ids: list[uuid.UUID],
    muster_point: str | None,
    by: str,
    now: datetime,
) -> tuple[int, int]:
    """Listeden yoklama: (işaretlenen, atlanan yardım isteyen)."""
    if emergency.ended_at is not None:
        raise OperationRuleError("not_active", "Acil durum aktif değil.", conflict=True)
    point = " ".join((muster_point or "").split())
    errors: dict[str, str] = {}
    picked = [u for u in await rows(session, emergency.id) if u.unit_id in set(unit_ids)]
    if not picked:
        errors["unit_ids"] = "Bölüm seçin."
    if not point:
        errors["muster_point"] = "Toplanma alanını seçin."
    elif point not in await _muster_points(session):
        errors["muster_point"] = "Toplanma alanı acil bilgi kartında yok."
    if errors:
        raise FormFieldError(errors)
    marked = skipped = 0
    for u in picked:
        if u.status == UnitSafety.HELP.value:  # ekip yönlendirilir; yoklama kapatmaz
            skipped += 1
            continue
        u.status, u.muster_point = UnitSafety.MUSTER.value, point
        u.responded_by, u.method, u.responded_at = by, SafetyMethod.LIST.value, now
        marked += 1
    await session.flush()
    return marked, skipped


async def _muster_points(session: AsyncSession) -> list[str]:
    return [str(p["name"]) for p in (await comm_svc.card(session)).muster_points]


async def end(session: AsyncSession, emergency: Emergency, *, by: str, now: datetime) -> Emergency:
    if emergency.ended_at is not None:
        raise OperationRuleError("not_active", "Acil durum aktif değil.", conflict=True)
    settings = await comm_svc.retention(session)
    emergency.ended_at, emergency.ended_by = now, by
    emergency.summary = totals(await rows(session, emergency.id))
    emergency.purge_on = purge_on(now.astimezone(BUSINESS_TZ).date(), settings.retention_days)
    await session.flush()
    return emergency


# --- Sakin --------------------------------------------------------------------------------


async def person_scope_units(
    session: AsyncSession, emergency: Emergency, person_id: uuid.UUID, today: date
) -> list[EmergencyUnit]:
    """Kişinin bugün bağlı olduğu, acil durum kapsamındaki bölümleri."""
    mine = select(UnitParty.unit_id).where(
        UnitParty.person_id == person_id,
        UnitParty.role.in_(_PARTY_ROLES),
        UnitParty.start_date <= today,
        or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
    )
    return list(
        await session.scalars(
            select(EmergencyUnit)
            .where(EmergencyUnit.emergency_id == emergency.id, EmergencyUnit.unit_id.in_(mine))
            .order_by(EmergencyUnit.unit_name)
        )
    )


async def resident_status(
    session: AsyncSession,
    emergency: Emergency,
    *,
    person_id: uuid.UUID,
    status: str | None,
    note: str | None,
    muster_point: str | None,
    by: str,
    now: datetime,
) -> tuple[UnitSafety, int]:
    """Kişinin kapsamdaki tüm bölümlerine yazar; (durum, bölüm sayısı)."""
    allowed = {UnitSafety.SAFE.value, UnitSafety.HELP.value, UnitSafety.MUSTER.value}
    if status not in allowed:
        raise FormFieldError({"status": "Durum seçin."})
    point = " ".join((muster_point or "").split()) or None
    text = " ".join((note or "").split()) or None
    if status == UnitSafety.MUSTER.value and not point:
        raise FormFieldError({"muster_point": "Toplanma alanını seçin."})
    if text and len(text) > NOTE_MAX:
        raise FormFieldError({"note": f"Not en fazla {NOTE_MAX} karakter."})
    units = await person_scope_units(
        session, emergency, person_id, now.astimezone(BUSINESS_TZ).date()
    )
    if not units:
        raise PermissionError("Bölümünüz bu acil durumun kapsamında değil.")
    for u in units:  # son yanıt geçerli
        u.status = status
        u.note = text
        u.muster_point = point if status == UnitSafety.MUSTER.value else None
        u.responded_by, u.method, u.responded_at = by, SafetyMethod.APP.value, now
    await session.flush()
    return UnitSafety(status), len(units)


# --- Gece işi: saklama süresi dolan yoklama verisi (K8) ------------------------------------


async def purge_due(session: AsyncSession, *, today: date, now: datetime) -> int:
    """`auto_purge` açıksa `purge_on` geçmiş acil durumların bölüm ayrıntısını siler; özet kalır."""
    if not (await comm_svc.retention(session)).auto_purge:
        return 0
    due = list(
        await session.scalars(
            select(Emergency).where(
                Emergency.ended_at.is_not(None),
                Emergency.purged_at.is_(None),
                Emergency.purge_on <= today,
            )
        )
    )
    for e in due:
        await session.execute(delete(EmergencyUnit).where(EmergencyUnit.emergency_id == e.id))
        e.purged_at = now
        record(
            session,
            AuditAction.DELETE,
            "emergency_units",
            e.id,
            {"emergency_id": str(e.id), "purge_on": e.purge_on.isoformat() if e.purge_on else None},
            reason="Saklama süresi doldu (KVKK, K8)",
        )
    await session.flush()
    return len(due)


@dataclass(frozen=True, slots=True)
class PurgeResult:
    site_id: uuid.UUID
    purged: int


async def run_purge(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> list[PurgeResult]:
    set_actor(Actor(None, "Acil durum verisi temizliği", None))
    today = now.astimezone(BUSINESS_TZ).date()
    with all_sites_scope():
        async with factory() as session:
            site_ids = list(await session.scalars(select(Site.id).order_by(Site.id)))
    results = []
    for site_id in site_ids:
        try:
            with site_scope(site_id):
                async with factory() as session, session.begin():
                    purged = await purge_due(session, today=today, now=now)
            if purged:
                results.append(PurgeResult(site_id, purged))
        except Exception:
            logger.exception("Acil durum verisi temizliği başarısız (site %s)", site_id)
    return results
