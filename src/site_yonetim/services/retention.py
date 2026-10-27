"""KVKK saklama politikası ve gece işi (Yazılım Dokümanı RAP-04, §4.4, §4.6; PM kararı K8).

Kategori tanımları `domain/retention.py`'de. Her ayarlanabilir kategorinin bir **deposu** vardır
(süre ve otomatik uygulama nerede tutulur) ve bir **uygulayıcısı** (süresi dolanı ne yapar):

- `visitors`: kaynak `security_settings.retention_months/auto_anonymize` (P06) — burada tekrar
  tutulmaz; anonimleştirmeyi güvenlik gece işi (03:00) yapar, rapora burada yazılır.
- `registrations`: kaynak `retention_policies`; reddedilen başvurunun kişi alanları silinir.
- Diğer modüller kendi kategorisini `register_store` ile bağlar (ör. acil durum yoklaması, P12:
  `emergency_settings.retention_days`). Deposu kayıtlı olmayan kategori ekranda görünmez.

Yasal saklama kategorileri (`keep`: finans, personel) yalnız bilgi satırıdır; otomatik işlem
yoktur. Her uygulama `retention_runs`'a silme/anonimleştirme raporu olarak yazılır.
"""

import dataclasses
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.retention import (
    BY_KEY,
    CATEGORIES,
    REGISTRATIONS,
    VISITORS,
    RetentionAction,
    RetentionCategory,
    RunTrigger,
    cutoff,
)
from site_yonetim.models import Registration, RetentionPolicy, RetentionRun, Site, Visitor
from site_yonetim.models.audit import Actor, AuditAction, record, set_actor
from site_yonetim.services import gatehouse as gatehouse_svc
from site_yonetim.services.charging import BUSINESS_TZ

logger = logging.getLogger(__name__)
ANONYMOUS = "Anonim"


@dataclass(frozen=True, slots=True)
class PolicyValue:
    value: int
    auto_apply: bool


Loader = Callable[[AsyncSession], Awaitable[PolicyValue]]
Saver = Callable[[AsyncSession, PolicyValue, str], Awaitable[None]]
Applier = Callable[[AsyncSession, date, datetime], Awaitable[int]]  # cutoff → etkilenen
Counter = Callable[[AsyncSession, date], Awaitable[int]]  # cutoff → bekleyen kayıt


@dataclass(frozen=True, slots=True)
class CategoryStore:
    load: Loader
    save: Saver
    apply: Applier
    pending: Counter
    in_nightly_job: bool = True  # False: başka bir gece işi uygular (ziyaretçi → güvenlik işi)


_STORES: dict[str, CategoryStore] = {}


def register_store(key: str, store: CategoryStore) -> None:
    """Modül kendi saklama kategorisini bağlar (kategori `domain/retention.CATEGORIES`'te)."""
    if key not in BY_KEY or not BY_KEY[key].adjustable:
        raise ValueError(f"Tanımsız ya da ayarlanamaz saklama kategorisi: {key}")
    _STORES[key] = store


def store_of(key: str) -> CategoryStore | None:
    return _STORES.get(key)


def listed_categories() -> list[RetentionCategory]:
    """Ekrandaki satırlar: yasal saklama satırları + deposu bağlı ayarlanabilir kategoriler."""
    return [c for c in CATEGORIES if not c.adjustable or c.key in _STORES]


# --- Politika tablosundaki kategoriler (varsayılan dokümandan, otomatik kapalı) ---------------


def _policy_loader(key: str) -> Loader:
    async def load(session: AsyncSession) -> PolicyValue:
        row = await session.scalar(select(RetentionPolicy).where(RetentionPolicy.category == key))
        if row is None:
            return PolicyValue(BY_KEY[key].default_value, False)
        return PolicyValue(row.value, row.auto_apply)

    return load


def _policy_saver(key: str) -> Saver:
    async def save(session: AsyncSession, value: PolicyValue, actor: str) -> None:
        row = await session.scalar(
            select(RetentionPolicy).where(RetentionPolicy.category == key).with_for_update()
        )
        if row is None:
            session.add(
                RetentionPolicy(
                    category=key, value=value.value, auto_apply=value.auto_apply, updated_by=actor
                )
            )
        else:
            row.value = value.value
            row.auto_apply = value.auto_apply
            row.updated_by = actor
        await session.flush()

    return save


# --- Ziyaretçi ve plaka: kaynak güvenlik ayarı -----------------------------------------------


async def _visitors_load(session: AsyncSession) -> PolicyValue:
    data = (await gatehouse_svc.load_settings(session)).data
    return PolicyValue(data.retention_months, data.auto_anonymize)


async def _visitors_save(session: AsyncSession, value: PolicyValue, actor: str) -> None:
    data = (await gatehouse_svc.load_settings(session)).data
    if (data.retention_months, data.auto_anonymize) == (value.value, value.auto_apply):
        return
    await gatehouse_svc.save_settings(
        session,
        dataclasses.replace(data, retention_months=value.value, auto_anonymize=value.auto_apply),
        actor=actor,
    )


async def _visitors_apply(session: AsyncSession, limit: date, now: datetime) -> int:
    from site_yonetim.services.security_jobs import anonymize_before  # döngüsel içe aktarma

    return await anonymize_before(session, limit, now=now)


async def _visitors_pending(session: AsyncSession, limit: date) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Visitor)
            .where(Visitor.visit_date < limit, Visitor.anonymized_at.is_(None))
        )
        or 0
    )


# --- Reddedilen kayıt başvuruları ------------------------------------------------------------


def _expired_registrations(limit: date) -> Select[uuid.UUID]:
    return select(Registration.id).where(
        Registration.status == "rejected",
        Registration.decided_at < datetime.combine(limit, time.min, BUSINESS_TZ),
        Registration.first_name != ANONYMOUS,
    )


async def _registrations_apply(session: AsyncSession, limit: date, now: datetime) -> int:
    result = await session.execute(
        update(Registration)
        .where(Registration.id.in_(_expired_registrations(limit)))
        .values(first_name=ANONYMOUS, last_name="", phone="", email=None, ip=None)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _registrations_pending(session: AsyncSession, limit: date) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(_expired_registrations(limit).subquery())
        )
        or 0
    )


register_store(
    VISITORS,
    CategoryStore(
        _visitors_load, _visitors_save, _visitors_apply, _visitors_pending, in_nightly_job=False
    ),
)
register_store(
    REGISTRATIONS,
    CategoryStore(
        _policy_loader(REGISTRATIONS),
        _policy_saver(REGISTRATIONS),
        _registrations_apply,
        _registrations_pending,
    ),
)


# --- Okuma / yazma ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyRow:
    category: RetentionCategory
    value: int
    auto_apply: bool
    cutoff: date | None
    pending: int | None  # süresi dolmuş, henüz işlenmemiş kayıt
    last_run: RetentionRun | None


async def policy(session: AsyncSession, today: date) -> list[PolicyRow]:
    rows: list[PolicyRow] = []
    for category in listed_categories():
        store = _STORES.get(category.key)
        if store is None:
            rows.append(PolicyRow(category, category.default_value, False, None, None, None))
            continue
        value = await store.load(session)
        limit = cutoff(category, value.value, today)
        last: RetentionRun | None = await session.scalar(
            select(RetentionRun)
            .where(RetentionRun.category == category.key)
            .order_by(RetentionRun.created_at.desc())
            .limit(1)
        )
        rows.append(
            PolicyRow(
                category,
                value.value,
                value.auto_apply,
                limit,
                await store.pending(session, limit),
                last,
            )
        )
    return rows


async def save(session: AsyncSession, key: str, value: PolicyValue, *, actor: str) -> None:
    store = _STORES[key]
    await store.save(session, value, actor)


def runs_query() -> Select[RetentionRun]:
    return select(RetentionRun).order_by(RetentionRun.created_at.desc(), RetentionRun.id)


async def apply(
    session: AsyncSession,
    key: str,
    *,
    today: date,
    now: datetime,
    trigger: RunTrigger,
    actor: str,
) -> RetentionRun:
    """Süresi dolanı işler ve rapor satırı yazar (etkilenen 0 olsa da: kanıt)."""
    category = BY_KEY[key]
    store = _STORES[key]
    value = await store.load(session)
    limit = cutoff(category, value.value, today)
    affected = await store.apply(session, limit, now)
    return log_run(session, category, limit, affected, trigger=trigger, actor=actor)


def log_run(
    session: AsyncSession,
    category: RetentionCategory,
    limit: date,
    affected: int,
    *,
    trigger: RunTrigger,
    actor: str,
) -> RetentionRun:
    run = RetentionRun(
        category=category.key,
        action=category.action.value,
        trigger=trigger.value,
        cutoff=limit,
        affected=affected,
        actor_name=actor,
    )
    session.add(run)
    if affected:
        record(
            session,
            AuditAction.DELETE if category.action is RetentionAction.DELETE else AuditAction.UPDATE,
            "retention_runs",
            None,
            {"category": category.key, "affected": affected, "cutoff": limit.isoformat()},
            reason="Saklama süresi doldu (KVKK, K8)",
        )
    return run


# --- Gece işi (KVKK saklama, günlük 03:30) ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobResult:
    site_id: uuid.UUID
    category: str
    affected: int


async def run_nightly(
    factory: async_sessionmaker[AsyncSession], *, today: date, now: datetime
) -> list[JobResult]:
    """Otomatik uygulaması **açık** kategorileri işler (varsayılan kapalı). Site başına ayrı
    transaction; bir sitenin hatası diğerlerini durdurmaz."""
    actor = "KVKK saklama işi"
    set_actor(Actor(None, actor, None))
    with all_sites_scope():
        async with factory() as session:
            sites = list(await session.scalars(select(Site).order_by(Site.id)))
    results: list[JobResult] = []
    for site in sites:
        for key, store in _STORES.items():
            if not store.in_nightly_job:
                continue
            try:
                with site_scope(site.id):
                    async with factory() as session, session.begin():
                        if not (await store.load(session)).auto_apply:
                            continue
                        run = await apply(
                            session, key, today=today, now=now, trigger=RunTrigger.JOB,
                            actor=actor,
                        )  # fmt: skip
                results.append(JobResult(site.id, key, run.affected))
            except Exception:
                logger.exception("KVKK saklama işi başarısız (site %s, %s)", site.id, key)
    return results
