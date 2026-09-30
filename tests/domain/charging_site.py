"""docs/07 §0 — tahakkuk testlerinin ortak test sitesi (saf veri, veritabanı yok)."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from site_yonetim.domain.charging.engine import (
    AccountData,
    ChargeInput,
    ComponentData,
    ItemData,
    PartyData,
    RuleData,
    UnitData,
)
from site_yonetim.domain.finance import AllocationKind, AreaBasis, Frequency, PayerRule, ScopeKind
from site_yonetim.domain.structure import AccountKind, PartyRole

CHARGE_DATE = date(2027, 1, 1)
DUE_DATE = date(2027, 1, 15)
BLOCK_A, BLOCK_B = uuid.uuid7(), uuid.uuid7()
TYPE_11, TYPE_21 = uuid.uuid7(), uuid.uuid7()
TYPE_WEIGHTS = {TYPE_11: Decimal("1.0"), TYPE_21: Decimal("1.3")}


def _unit(block: str, number: int) -> UnitData:
    if block == "A":
        big = number % 2 == 0
        area, share = (Decimal(110), 45) if big else (Decimal(75), 33)
    else:
        big = number % 3 == 0
        area, share = (Decimal(105), 42) if big else (Decimal(72), 31)
    unit_type = TYPE_21 if big else TYPE_11
    return UnitData(
        id=uuid.uuid7(),
        name=f"{block}-{number}",
        block_id=BLOCK_A if block == "A" else BLOCK_B,
        unit_type_id=unit_type,
        unit_type_weight=TYPE_WEIGHTS[unit_type],
        gross_area=area,
        net_area=area - 10,
        land_share_numerator=share,
        land_share_denominator=1000,
    )


@dataclass
class ChargingSite:
    units: list[UnitData]
    parties: list[PartyData]
    accounts: list[AccountData]
    items: list[ItemData] = field(default_factory=list)

    def unit(self, name: str) -> UnitData:
        return next(u for u in self.units if u.name == name)

    def replace_unit(self, name: str, **changes: object) -> None:
        self.units = [replace(u, **changes) if u.name == name else u for u in self.units]  # type: ignore[arg-type]

    def parties_of(self, name: str) -> list[PartyData]:
        unit_id = self.unit(name).id
        return [p for p in self.parties if p.unit_id == unit_id]

    def end_parties(self, name: str, end: date, role: PartyRole | None = None) -> None:
        unit_id = self.unit(name).id
        self.parties = [
            replace(p, end_date=end)
            if p.unit_id == unit_id and (role is None or p.role is role)
            else p
            for p in self.parties
        ]

    def input(self, *items: ItemData) -> ChargeInput:
        return ChargeInput(
            charge_date=CHARGE_DATE,
            due_date=DUE_DATE,
            items=list(items),
            units=self.units,
            parties=self.parties,
            accounts=self.accounts,
        )


def build_site() -> ChargingSite:
    units = [_unit(block, n) for block in ("A", "B") for n in range(1, 13)]
    parties: list[PartyData] = []
    accounts: list[AccountData] = []
    for unit in units:
        number = int(unit.name.split("-")[1])
        base = unit.name.replace("-", "")
        owner = uuid.uuid7()
        parties.append(
            PartyData(unit.id, owner, f"{unit.name} MALİK", PartyRole.OWNER, date(2020, 1, 1))
        )
        accounts.append(AccountData(uuid.uuid7(), unit.id, owner, AccountKind.OWNER, f"{base}-M"))
        if number % 2 == 0:
            tenant = uuid.uuid7()
            parties.append(
                PartyData(
                    unit.id, tenant, f"{unit.name} KİRACI", PartyRole.TENANT, date(2025, 6, 1)
                )
            )
            accounts.append(
                AccountData(uuid.uuid7(), unit.id, tenant, AccountKind.OCCUPANT, f"{base}-K")
            )
        else:
            accounts.append(
                AccountData(uuid.uuid7(), unit.id, owner, AccountKind.OCCUPANT, f"{base}-O")
            )
    return ChargingSite(units, parties, accounts)


def rule(kind: AllocationKind, **fields: object) -> RuleData:
    return RuleData(id=uuid.uuid7(), name=kind.value, kind=kind, **fields)  # type: ignore[arg-type]


def item(
    name: str,
    annual: str,
    kind: AllocationKind | RuleData,
    *,
    frequency: Frequency = Frequency.MONTHLY,
    payer: PayerRule = PayerRule.OCCUPANT,
    **fields: object,
) -> ItemData:
    allocation = kind if isinstance(kind, RuleData) else rule(kind)
    return ItemData(
        id=uuid.uuid7(),
        name=name,
        annual_amount=Decimal(annual),
        frequency=frequency,
        rule=allocation,
        payer_rule=payer,
        **fields,  # type: ignore[arg-type]
    )


def composite(*parts: tuple[AllocationKind, str]) -> RuleData:
    return rule(
        AllocationKind.COMPOSITE,
        components=tuple(
            ComponentData(kind, Decimal(percent), AreaBasis.GROSS) for kind, percent in parts
        ),
    )


# Sık kullanılan kalemler (docs/07 §2)
def aidat() -> ItemData:
    return item("Kapıcı maaşı", "240000", AllocationKind.EQUAL)


def cati() -> ItemData:
    return item(
        "Çatı onarımı",
        "180000",
        AllocationKind.BY_LAND_SHARE,
        frequency=Frequency.ONE_TIME,
        payer=PayerRule.OWNER,
    )


def isitma() -> ItemData:
    return item("Isıtma sabit payı", "600000", AllocationKind.BY_AREA)


def yonetim() -> ItemData:
    return item("Yönetim ücreti", "96000", AllocationKind.BY_UNIT_TYPE_WEIGHT)


def asansor(block_ids: frozenset[uuid.UUID]) -> ItemData:
    return item(
        "Asansör bakımı",
        "36000",
        AllocationKind.EQUAL,
        scope_kind=ScopeKind.BLOCKS,
        scope_block_ids=block_ids,
    )
