"""Yeni sitenin varsayılan finans kurulumu (docs/10 §3 adım 3–6). Açık site kapsamında çağrılır.

İdempotenttir: eksik olanı ekler, var olana dokunmaz — göçten önce açılmış siteler için de
güvenle çağrılabilir. Kasa hesapları (adım 8) kasa diliminde gelecek.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.finance import AllocationKind, ExpenseCategoryKind, PayerRule
from site_yonetim.models import (
    AllocationComponent,
    AllocationRule,
    ChargeType,
    ExpenseCategory,
    LateFeePolicy,
)

DEFAULT_EXPENSE_CATEGORIES = (
    ("İşletme Giderleri", ExpenseCategoryKind.OPERATING),
    ("Demirbaş ve Yatırım", ExpenseCategoryKind.CAPITAL_IMPROVEMENT),
)
DEFAULT_CHARGE_TYPES = (
    ("Aidat", PayerRule.OCCUPANT, "KMK m.20 — işletme gideri, oturan öder"),
    ("Demirbaş Katılım Payı", PayerRule.OWNER, "KMK m.22 — demirbaş/yatırım, malik öder"),
)
HEATING_RULE = "Merkezi Isıtma (%70 tüketim + %30 m²)"
HEATING_NOTE = "Tüketim payı sayaç okuması girilene kadar eşit dağıtılır."
DEFAULT_RULES = (
    ("Eşit Paylaşım", AllocationKind.EQUAL),
    ("Arsa Payı", AllocationKind.BY_LAND_SHARE),
    ("Brüt Metrekare", AllocationKind.BY_AREA),
    ("Daire Tipi Ağırlığı", AllocationKind.BY_UNIT_TYPE_WEIGHT),
)
# Sayaç okuma yok: tüketim bileşeni şimdilik `equal` (docs/10 §3 notu, docs/12 K14).
HEATING_COMPONENTS = ((AllocationKind.EQUAL, Decimal(70)), (AllocationKind.BY_AREA, Decimal(30)))


async def ensure_finance_setup(session: AsyncSession) -> None:
    categories = set(await session.scalars(select(ExpenseCategory.name)))
    for order, (name, category_kind) in enumerate(DEFAULT_EXPENSE_CATEGORIES):
        if name not in categories:
            session.add(ExpenseCategory(name=name, kind=category_kind.value, sort_order=order))

    types = set(await session.scalars(select(ChargeType.name)))
    for order, (name, payer, basis) in enumerate(DEFAULT_CHARGE_TYPES):
        if name not in types:
            session.add(
                ChargeType(name=name, payer_rule=payer.value, legal_basis=basis, sort_order=order)
            )

    rules = set(await session.scalars(select(AllocationRule.name)))
    for name, kind in DEFAULT_RULES:
        if name not in rules:
            session.add(AllocationRule(name=name, kind=kind.value))
    if HEATING_RULE not in rules:
        heating = AllocationRule(
            name=HEATING_RULE, kind=AllocationKind.COMPOSITE.value, note=HEATING_NOTE
        )
        session.add(heating)
        await session.flush()
        for order, (component_kind, percent) in enumerate(HEATING_COMPONENTS):
            session.add(
                AllocationComponent(
                    allocation_rule_id=heating.id,
                    kind=component_kind.value,
                    percent=percent,
                    sort_order=order,
                )
            )

    if await session.scalar(select(LateFeePolicy.id)) is None:
        session.add(LateFeePolicy())  # aylık %5, 5 gün tolerans, asgari 0, açık
    await session.flush()
