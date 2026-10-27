"""Demo: sözleşme performansı (servis istekleri 68–70; SAT-06, 15.3).

Aksu Konakları'nda iki çerçeve sözleşme:
- **Temizlik** (20.000 TL/ay): denetim puanı + görev tamamlama KPI'ları, "denetim hedefin %10'undan
  fazla altındaysa %5 kesinti" kuralı. Geçen ayın karnesi kesinleşti; denetim 72 → 1.000,00 TL
  kesinti **onay bekliyor** (karneyi Kerem Yıldırım kesinleştirdi; Selin Arı onaylayabilir).
  TÜFE'ye bağlı yıllık artışın tarihi 20 gün sonra → "Fiyat artışı" sekmesinde karar bekler.
- **Güvenlik** (85.000 TL/ay): devriye + elle sakin memnuniyeti; geçen ay karnesi cezasız
  kesinleşti. Bir ay önce TÜFE (tavan %35) ile yapılmış **onaylı artış** ve bedel geçmişi
  (geçen ayın karnesi artış öncesi bedelle).

Platform: son 13 ayın TÜFE/ÜFE değerleri (bu ay dahil).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.contract_performance import (
    AmountSource,
    EscalationInput,
    EscalationStatus,
    KpiInput,
    PenaltyInput,
    PenaltyStatus,
    ScorecardStatus,
    capped,
    escalated_amount,
    index_period,
    period_key,
)
from site_yonetim.domain.facility import add_months
from site_yonetim.models import (
    ContractEscalation,
    ContractPenalty,
    ContractScorecard,
    IndexValue,
    Site,
)
from site_yonetim.services import contract_performance as perf_svc
from site_yonetim.services import contracts as contracts_svc
from site_yonetim.services.approvals import Person
from site_yonetim.services.charging import BUSINESS_TZ

D = Decimal
PATROL = "Devriye tamamlama"
# (yıllık TÜFE, ÜFE) — 13 ay, en eski önce; demo amaçlı yaklaşık değerler
INDEX_SERIES = (
    ("49.38", "41.20"), ("48.58", "38.90"), ("47.09", "35.60"), ("44.38", "32.10"),
    ("42.12", "30.40"), ("39.05", "27.80"), ("38.10", "27.10"), ("37.86", "26.90"),
    ("35.05", "24.50"), ("35.41", "24.20"), ("33.52", "22.60"), ("33.29", "22.10"),
    ("32.87", "21.70"),
)  # fmt: skip


async def seed_index_values(session: AsyncSession, today: date, entered_by: uuid.UUID) -> None:
    """GLOBAL: son 13 ayın endeks değerleri (bu ay dahil)."""
    month = YearMonth.of(today)
    periods = []
    for _ in INDEX_SERIES:
        periods.append(month)
        month = month.previous()
    for ym, (tufe, ufe) in zip(reversed(periods), INDEX_SERIES, strict=True):
        session.add(
            IndexValue(
                period=period_key(ym.year, ym.month), tufe=D(tufe), ufe=D(ufe),
                entered_by_id=entered_by, entered_by="Deniz Aksoy",
            )
        )  # fmt: skip
    await session.flush()


@dataclass(frozen=True, slots=True)
class _Row:
    name: str
    source: str
    target: str
    weight: int
    actual: str
    detail: str


def _frozen(kpis: list[uuid.UUID], rows: list[_Row]) -> list[dict[str, object]]:
    return [
        {"kpi_id": str(k), "name": r.name, "source": r.source, "target": r.target,
         "weight": r.weight, "actual": r.actual, "detail": r.detail}
        for k, r in zip(kpis, rows, strict=True)
    ]  # fmt: skip


async def seed_contracts(
    factory: async_sessionmaker[AsyncSession],
    site_id: uuid.UUID,
    manager: Person,
    today: date,
) -> None:
    last = YearMonth.of(today).previous()
    finalized_at = datetime.combine(today.replace(day=1) + timedelta(days=1), time(10), BUSINESS_TZ)
    with site_scope(site_id):
        async with factory() as session, session.begin():
            site = await session.get(Site, site_id)
            if site is None:  # pragma: no cover
                return
            # Temizlik: artış tarihi 20 gün sonra (başlangıç bir yıl önce)
            cleaning = await contracts_svc.create(
                session,
                contracts_svc.ContractData(
                    vendor="Parlak Temizlik Hizmetleri Ltd.",
                    subject="Ortak alan ve otopark temizliği (4 personel)",
                    category="cleaning",
                    start_date=add_months(today + timedelta(days=20), -12),
                    end_date=add_months(today + timedelta(days=20), 12) - timedelta(days=1),
                    amount=D("20000.00"),
                    period="monthly",
                    notice_days=60,
                    auto_renew=False,
                    note="Aylık hizmet karnesine bağlı ceza maddesi: md. 9.",
                ),
                actor=manager,
            )
            await perf_svc.save_definitions(
                session,
                cleaning,
                perf_svc.DefinitionsInput(
                    vendor_id=None,
                    vendor_given=False,
                    kpis=[
                        KpiInput("d", "Temizlik denetim puanı", "inspection", D(85), 60, None),
                        KpiInput("g", "Görev tamamlama", "tasks", D(90), 40, None),
                    ],
                    penalties=[PenaltyInput("d", D(10), "percent", D(5))],
                    escalation=EscalationInput("tufe", None, "yearly", None),
                ),
                today=today,
            )
            loaded = await perf_svc.load(session, cleaning.id)
            card = ContractScorecard(
                id=uuid.uuid7(), contract_id=cleaning.id,
                period=period_key(last.year, last.month), status=ScorecardStatus.FINALIZED.value,
                manual_values={}, comment="Denetim puanı iki aydır düşüyor; firmayla görüşülecek.",
                rows=_frozen([k.id for k in loaded.kpis], [
                    _Row("Temizlik denetim puanı", "inspection", "85", 60, "72",
                         "3 denetim ortalaması"),
                    _Row("Görev tamamlama", "tasks", "90", 40, "88", "82/93 görev"),
                ]),
                total=90, monthly_amount=D("20000.00"), finalized_by_id=manager.user_id,
                finalized_by=manager.name, finalized_at=finalized_at,
            )  # fmt: skip
            session.add(card)
            await session.flush()
            session.add(
                ContractPenalty(
                    contract_id=cleaning.id, scorecard_id=card.id,
                    period=card.period, amount=D("1000.00"),
                    reasons=["Temizlik denetim puanı: gerçekleşen 72, hedef 85 (sınır 76,5) → "
                             "%5 kesinti"],
                    status=PenaltyStatus.PENDING.value, requested_by_id=manager.user_id,
                )
            )  # fmt: skip

            # Güvenlik: bir ay önce onaylı TÜFE artışı (tavan %35)
            due = add_months(today, -1)
            start = add_months(due, -12)
            security = await contracts_svc.create(
                session,
                contracts_svc.ContractData(
                    vendor="Kale Güvenlik A.Ş.",
                    subject="7/24 güvenlik ve devriye (6 personel)",
                    category="security",
                    start_date=start,
                    end_date=add_months(start, 36) - timedelta(days=1),
                    amount=D("85000.00"),
                    period="monthly",
                    notice_days=90,
                    auto_renew=True,
                    note=None,
                ),
                actor=manager,
            )
            await perf_svc.save_definitions(
                session,
                security,
                perf_svc.DefinitionsInput(
                    vendor_id=None,
                    vendor_given=False,
                    kpis=[
                        KpiInput("p", PATROL, "patrol", D(95), 70, None),
                        KpiInput("m", "Sakin memnuniyeti", "manual", D(80), 30, None),
                    ],
                    penalties=[PenaltyInput("p", D(5), "fixed", D("2500.00"))],
                    escalation=EscalationInput("tufe", None, "yearly", D(35)),
                ),
                today=today,
            )
            idx = index_period(due)
            pos = 12 - (
                (YearMonth.of(today).year - int(idx[:4])) * 12
                + YearMonth.of(today).month
                - int(idx[5:])
            )
            rate = D(INDEX_SERIES[pos][0])
            limited = capped(rate, D(35))
            new = escalated_amount(D("85000.00"), limited)
            record = ContractEscalation(
                id=uuid.uuid7(), contract_id=security.id, due_date=due, kind="tufe",
                index_period=idx, rate=rate, capped_rate=limited, old_amount=D("85000.00"),
                new_amount=new, status=EscalationStatus.APPROVED.value,
                note="Firma yazıyla bilgilendirildi.", requested_by_id=manager.user_id,
                requested_by=manager.name, decided_by=manager.name,
                decided_at=datetime.combine(due - timedelta(days=12), time(11), UTC),
            )  # fmt: skip
            session.add(record)
            await session.flush()
            await contracts_svc.record_amount(
                session, security, amount=new, period="monthly", effective_from=due,
                source=AmountSource.ESCALATION, actor=manager,
                note=f"%{limited} artış", escalation_id=record.id,
            )  # fmt: skip
            loaded = await perf_svc.load(session, security.id)
            session.add(
                ContractScorecard(
                    contract_id=security.id, period=period_key(last.year, last.month),
                    status=ScorecardStatus.FINALIZED.value, manual_values={},
                    comment=None,
                    rows=_frozen([k.id for k in loaded.kpis], [
                        _Row(PATROL, "patrol", "95", 70, "97", "62 tur, 481/496 nokta"),
                        _Row("Sakin memnuniyeti", "manual", "80", 30, "92", "Elle girilir"),
                    ]),
                    total=100, monthly_amount=D("85000.00"), finalized_by_id=manager.user_id,
                    finalized_by=manager.name, finalized_at=finalized_at,
                )
            )  # fmt: skip
            await session.flush()
