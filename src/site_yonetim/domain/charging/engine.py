"""Tahakkuk motoru — saf (docs/04 §4; altın testler docs/07 §2).

Kalemleri kapsamdaki bölümlere dağıtım kuralıyla paylaştırır ve borcu doğru kişinin doğru cari
hesabına yazar. Veritabanına dokunmaz; aynı girdi her zaman aynı önizlemeyi verir. Eksik veri
koşuyu durdurmaz: uyarı üretir, o kalemi/bölümü atlar, diğerleri devam eder.
"""

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from site_yonetim.domain.charging.periods import period_amount
from site_yonetim.domain.finance import (
    AllocationKind,
    AreaBasis,
    Frequency,
    PayerRule,
    ScopeKind,
)
from site_yonetim.domain.money import ZERO, distribute, is_positive, round_money
from site_yonetim.domain.structure import AccountKind, PartyRole, is_active_on
from site_yonetim.domain.text import format_money_tr, format_ratio_tr

MAX_LISTED_UNITS = 5
HUNDRED = Decimal(100)
_RATIO_DISPLAY = Decimal("0.000001")


class WarningKind(StrEnum):
    NO_ACTIVE_PARTY = "no_active_party"
    MISSING_LEDGER_ACCOUNT = "missing_ledger_account"
    MISSING_WEIGHT_DATA = "missing_weight_data"
    EMPTY_SCOPE = "empty_scope"
    COMPOSITE_PERCENT_MISMATCH = "composite_percent_mismatch"


# --- Girdi --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitData:
    id: uuid.UUID
    name: str  # `A-12`
    block_id: uuid.UUID
    unit_type_id: uuid.UUID | None = None
    unit_type_weight: Decimal | None = None
    gross_area: Decimal | None = None
    net_area: Decimal | None = None
    land_share_numerator: int | None = None
    land_share_denominator: int | None = None
    usage: str = "residential"
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class PartyData:
    unit_id: uuid.UUID
    person_id: uuid.UUID
    person_name: str
    role: PartyRole
    start_date: date
    end_date: date | None = None
    share_percent: Decimal = HUNDRED


@dataclass(frozen=True, slots=True)
class AccountData:
    id: uuid.UUID
    unit_id: uuid.UUID
    person_id: uuid.UUID
    kind: AccountKind
    reference_code: str
    is_closed: bool = False


@dataclass(frozen=True, slots=True)
class ComponentData:
    kind: AllocationKind
    percent: Decimal
    area_basis: AreaBasis = AreaBasis.GROSS


@dataclass(frozen=True, slots=True)
class RuleData:
    id: uuid.UUID
    name: str
    kind: AllocationKind
    area_basis: AreaBasis = AreaBasis.GROSS
    fixed_amount: Decimal | None = None
    components: tuple[ComponentData, ...] = ()
    unit_weights: Mapping[uuid.UUID, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ItemData:
    id: uuid.UUID
    name: str
    annual_amount: Decimal
    frequency: Frequency
    rule: RuleData | None
    payer_rule: PayerRule = PayerRule.OCCUPANT  # tip bulunamazsa varsayılan (§4.5)
    scope_kind: ScopeKind = ScopeKind.WHOLE_SITE
    scope_block_ids: frozenset[uuid.UUID] = frozenset()
    scope_unit_type_ids: frozenset[uuid.UUID] = frozenset()
    scope_usage: str | None = None


@dataclass(frozen=True, slots=True)
class ChargeInput:
    charge_date: date
    due_date: date
    items: Sequence[ItemData]  # sort_order sırasıyla
    units: Sequence[UnitData]  # gösterim sırasıyla
    parties: Sequence[PartyData]  # tümü; motor tarihe göre süzer
    accounts: Sequence[AccountData]
    meter: Mapping[tuple[uuid.UUID, uuid.UUID], Decimal] = field(default_factory=dict)


# --- Çıktı --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreviewLine:
    budget_item_id: uuid.UUID
    description: str  # kalem adı
    amount: Decimal
    allocation_kind: AllocationKind
    weight: Decimal | None
    weight_total: Decimal | None
    source_amount: Decimal
    explanation: str


@dataclass(frozen=True, slots=True)
class PreviewCharge:
    unit_id: uuid.UUID
    unit_name: str
    ledger_account_id: uuid.UUID
    reference_code: str
    person_id: uuid.UUID
    person_name: str
    account_kind: AccountKind
    lines: tuple[PreviewLine, ...]

    @property
    def amount(self) -> Decimal:
        return sum((line.amount for line in self.lines), ZERO)


@dataclass(frozen=True, slots=True)
class PreviewWarning:
    kind: WarningKind
    message: str
    unit_id: uuid.UUID | None = None
    budget_item_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ItemTotal:
    budget_item_id: uuid.UUID
    name: str
    amount: Decimal  # kalemin bu dönemdeki tutarı
    distributed: Decimal  # bölümlere dağıtılan — `amount`'a kuruşu kuruşuna eşit olmalı


@dataclass(frozen=True, slots=True)
class Preview:
    charges: list[PreviewCharge]
    warnings: list[PreviewWarning]
    item_totals: list[ItemTotal]

    @property
    def total_amount(self) -> Decimal:
        return sum((c.amount for c in self.charges), ZERO)

    @property
    def unit_count(self) -> int:
        """Borç yazılan farklı bölüm sayısı."""
        return len({c.unit_id for c in self.charges})


# --- Ağırlıklar ---------------------------------------------------------------------

_DATA_LABELS = {
    AllocationKind.BY_LAND_SHARE: "arsa payı",
    AllocationKind.BY_UNIT_TYPE_WEIGHT: "daire tipi ağırlığı",
    AllocationKind.BY_CUSTOM_WEIGHT: "özel ağırlık",
    AllocationKind.BY_METER_CONSUMPTION: "sayaç tüketimi",
}
_COMPONENT_LABELS = {
    AllocationKind.EQUAL: "eşit",
    AllocationKind.BY_LAND_SHARE: "arsa payı",
    AllocationKind.BY_AREA: "metrekare",
    AllocationKind.BY_UNIT_TYPE_WEIGHT: "daire tipi ağırlığı",
    AllocationKind.BY_CUSTOM_WEIGHT: "özel ağırlık",
    AllocationKind.BY_METER_CONSUMPTION: "sayaç tüketimi",
}


def _data_label(kind: AllocationKind, basis: AreaBasis) -> str:
    if kind is AllocationKind.BY_AREA:
        return "brüt metrekare" if basis is AreaBasis.GROSS else "net metrekare"
    return _DATA_LABELS[kind]


def _weight(
    kind: AllocationKind,
    basis: AreaBasis,
    unit: UnitData,
    item: ItemData,
    rule: RuleData,
    meter: Mapping[tuple[uuid.UUID, uuid.UUID], Decimal],
) -> Decimal:
    """Bölümün ağırlığı; veri yoksa 0 (§4.4)."""
    match kind:
        case AllocationKind.EQUAL:
            return Decimal(1)
        case AllocationKind.BY_LAND_SHARE:
            if unit.land_share_numerator is None or not unit.land_share_denominator:
                return ZERO
            return Decimal(unit.land_share_numerator) / Decimal(unit.land_share_denominator)
        case AllocationKind.BY_AREA:
            area = unit.gross_area if basis is AreaBasis.GROSS else unit.net_area
            return area or ZERO
        case AllocationKind.BY_UNIT_TYPE_WEIGHT:
            return unit.unit_type_weight or ZERO
        case AllocationKind.BY_CUSTOM_WEIGHT:
            return rule.unit_weights.get(unit.id, ZERO)
        case AllocationKind.BY_METER_CONSUMPTION:
            return meter.get((item.id, unit.id), ZERO)
        case _:  # pragma: no cover - sabit tutar ve bileşik ağırlıkla çalışmaz
            raise ValueError(kind)


def _number_tr(value: Decimal) -> str:
    """`2.140,00` — metrekare ve tüketim gösterimi."""
    return format_money_tr(value).removesuffix(" TL")


def _ratio_tr(value: Decimal) -> str:
    """Oran gösterimi; `1/3` gibi sonsuz ondalıklar 6 haneye yuvarlanır: `0,333333`."""
    return format_ratio_tr(value.quantize(_RATIO_DISPLAY, rounding=ROUND_HALF_UP))


def _explanation(
    kind: AllocationKind,
    basis: AreaBasis,
    weight: Decimal,
    total: Decimal,
    pool: Decimal,
    count: int,
) -> str:
    money = format_money_tr(pool)
    match kind:
        case AllocationKind.EQUAL:
            return f"{money}, {count} bağımsız bölüme eşit paylaştırıldı"
        case AllocationKind.BY_LAND_SHARE:
            return f"Arsa payı {_ratio_tr(weight)} / {_ratio_tr(total)} × {money}"
        case AllocationKind.BY_AREA:
            label = "Brüt" if basis is AreaBasis.GROSS else "Net"
            return f"{label} {_number_tr(weight)} m² / {_number_tr(total)} m² × {money}"
        case AllocationKind.BY_UNIT_TYPE_WEIGHT:
            return f"Daire tipi ağırlığı {_ratio_tr(weight)} / {_ratio_tr(total)} × {money}"
        case AllocationKind.BY_CUSTOM_WEIGHT:
            return f"Özel ağırlık {_ratio_tr(weight)} / {_ratio_tr(total)} × {money}"
        case AllocationKind.BY_METER_CONSUMPTION:
            return f"Sayaç tüketimi {_number_tr(weight)} / {_number_tr(total)} × {money}"
        case _:  # pragma: no cover
            raise ValueError(kind)


def _names(units: Sequence[UnitData]) -> str:
    listed = ", ".join(u.name for u in units[:MAX_LISTED_UNITS])
    return listed + (" …" if len(units) > MAX_LISTED_UNITS else "")


# --- Motor --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Share:
    amount: Decimal
    weight: Decimal | None
    weight_total: Decimal | None
    explanation: str


class _Engine:
    def __init__(self, data: ChargeInput) -> None:
        self.data = data
        self.warnings: list[PreviewWarning] = []

    def warn(
        self,
        kind: WarningKind,
        message: str,
        *,
        unit_id: uuid.UUID | None = None,
        item: ItemData | None = None,
    ) -> None:
        self.warnings.append(PreviewWarning(kind, message, unit_id, item.id if item else None))

    def in_scope(self, item: ItemData, units: Sequence[UnitData]) -> list[UnitData]:
        match item.scope_kind:
            case ScopeKind.WHOLE_SITE:
                return list(units)
            case ScopeKind.BLOCKS:
                return [u for u in units if u.block_id in item.scope_block_ids]
            case ScopeKind.UNIT_TYPES:
                return [u for u in units if u.unit_type_id in item.scope_unit_type_ids]
            case ScopeKind.USAGE:
                return [u for u in units if u.usage == item.scope_usage]

    def weighted(
        self,
        item: ItemData,
        rule: RuleData,
        kind: AllocationKind,
        basis: AreaBasis,
        units: list[UnitData],
        pool: Decimal,
        *,
        label_prefix: str = "",
    ) -> dict[uuid.UUID, _Share] | None:
        """Havuzu ağırlıkla dağıtır. Hiç veri yoksa uyarı + `None`; bazı bölümde yoksa uyarı."""
        weights = [_weight(kind, basis, u, item, rule, self.data.meter) for u in units]
        total = sum(weights, ZERO)
        if kind is not AllocationKind.EQUAL:
            label = _data_label(kind, basis)
            missing = [u for u, w in zip(units, weights, strict=True) if w <= ZERO]
            if total <= ZERO:
                self.warn(
                    WarningKind.MISSING_WEIGHT_DATA,
                    f"'{item.name}'{label_prefix}: {label} verisi hiçbir bağımsız bölümde yok; "
                    "kalem dağıtılamadı.",
                    item=item,
                )
                return None
            if missing:
                self.warn(
                    WarningKind.MISSING_WEIGHT_DATA,
                    f"'{item.name}'{label_prefix}: {len(missing)} bağımsız bölümde {label} verisi "
                    f"yok ({_names(missing)}); bu bölümler kalemden pay almadı.",
                    item=item,
                )
        amounts = distribute(pool, weights)
        count = sum(1 for w in weights if w > ZERO)
        shares: dict[uuid.UUID, _Share] = {}
        for unit, weight, amount in zip(units, weights, amounts, strict=True):
            if weight <= ZERO:
                continue
            shares[unit.id] = _Share(
                amount,
                weight if kind is not AllocationKind.EQUAL else Decimal(1),
                total,
                _explanation(kind, basis, weight, total, pool, count),
            )
        return shares

    def composite(
        self, item: ItemData, rule: RuleData, units: list[UnitData], pool: Decimal
    ) -> dict[uuid.UUID, _Share] | None:
        """§4.4.1: yüzdelerle bileşenlere böl, her bileşeni kendi türüyle dağıt, topla.

        Verisi hiçbir bölümde olmayan bileşen atlanır; tutar kalan bileşenlere yüzdeleriyle
        oranlanır — kalem toplamı her zaman kalem tutarına eşit kalır.
        """
        components = [c for c in rule.components if c.percent > ZERO]
        percent_total = sum((c.percent for c in components), ZERO)
        if not components:
            self.warn(
                WarningKind.MISSING_WEIGHT_DATA,
                f"'{item.name}': bileşik kuralın bileşeni yok; kalem dağıtılamadı.",
                item=item,
            )
            return None
        if percent_total != HUNDRED:
            self.warn(
                WarningKind.COMPOSITE_PERCENT_MISMATCH,
                f"'{item.name}' bileşenlerinin yüzdeleri {format_ratio_tr(percent_total)} ediyor, "
                "100 olmalı. Tutar mevcut yüzdelere oranlanarak dağıtıldı.",
                item=item,
            )
        usable = []
        for component in components:
            weights = [
                _weight(component.kind, component.area_basis, u, item, rule, self.data.meter)
                for u in units
            ]
            if sum(weights, ZERO) > ZERO:
                usable.append(component)
            else:
                label = (
                    _data_label(component.kind, component.area_basis)
                    if (component.kind is not AllocationKind.EQUAL)
                    else "eşit"
                )
                self.warn(
                    WarningKind.MISSING_WEIGHT_DATA,
                    f"'{item.name}': %{format_ratio_tr(component.percent)} {label} bileşeninin "
                    "verisi hiçbir bağımsız bölümde yok; bileşen atlandı.",
                    item=item,
                )
        if not usable:
            return None
        parts = distribute(pool, [c.percent for c in usable])
        totals: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
        for component, part in zip(usable, parts, strict=True):
            prefix = f" (%{format_ratio_tr(component.percent)} bileşen)"
            shares = self.weighted(
                item, rule, component.kind, component.area_basis, units, part, label_prefix=prefix
            )
            for unit_id, share in (shares or {}).items():
                totals[unit_id] += share.amount
        explanation = (
            " + ".join(
                f"%{format_ratio_tr(c.percent)} {_COMPONENT_LABELS[c.kind]}" for c in components
            )
            + " bileşimi"
        )
        return {
            unit_id: _Share(amount, None, None, explanation)
            for unit_id, amount in totals.items()
            if amount != ZERO
        }

    def allocate(
        self, item: ItemData, units: list[UnitData], pool: Decimal
    ) -> dict[uuid.UUID, _Share] | None:
        rule = item.rule
        if rule is None:
            self.warn(
                WarningKind.MISSING_WEIGHT_DATA,
                f"'{item.name}' kaleminin dağıtım kuralı bulunamadı; kalem atlandı.",
                item=item,
            )
            return None
        if rule.kind is AllocationKind.FIXED_PER_UNIT:
            fixed = round_money(rule.fixed_amount or ZERO)
            if fixed == ZERO:
                self.warn(
                    WarningKind.MISSING_WEIGHT_DATA,
                    f"'{item.name}': sabit tutar tanımlı değil; kalem atlandı.",
                    item=item,
                )
                return None
            return {
                u.id: _Share(fixed, None, None, "Bağımsız bölüm başına sabit tutar") for u in units
            }
        if rule.kind is AllocationKind.COMPOSITE:
            return self.composite(item, rule, units, pool)
        return self.weighted(item, rule, rule.kind, rule.area_basis, units, pool)

    def payer(
        self, unit: UnitData, rule: PayerRule, parties: Sequence[PartyData]
    ) -> tuple[PartyData, AccountKind] | None:
        """§4.5: oturan kuralı → aktif kiracı, yoksa malik (oturan hesabı); malik kuralı → malik.

        Birden çok aktif malik/kiracı varsa hissesi büyük, sonra başlangıcı eski olan ödeyendir.
        """
        day = self.data.charge_date
        active = [p for p in parties if is_active_on(p.start_date, p.end_date, day)]

        def first(role: PartyRole) -> PartyData | None:
            candidates = [p for p in active if p.role is role]
            candidates.sort(key=lambda p: (-p.share_percent, p.start_date, p.person_id))
            return candidates[0] if candidates else None

        owner = first(PartyRole.OWNER)
        if rule is PayerRule.OWNER:
            return (owner, AccountKind.OWNER) if owner else None
        tenant = first(PartyRole.TENANT)
        chosen = tenant or owner
        return (chosen, AccountKind.OCCUPANT) if chosen else None

    def run(self) -> Preview:
        data = self.data
        units = [u for u in data.units if u.is_active]  # pasif bölümler tamamen dışarıda
        lines_by_unit: dict[uuid.UUID, dict[PayerRule, list[PreviewLine]]] = defaultdict(
            lambda: defaultdict(list)
        )
        item_totals: list[ItemTotal] = []

        for item in data.items:
            pool = period_amount(item.annual_amount, item.frequency)
            scoped = self.in_scope(item, units)
            if not scoped:
                self.warn(
                    WarningKind.EMPTY_SCOPE,
                    f"'{item.name}' kaleminin kapsamında aktif bağımsız bölüm yok; kalem atlandı.",
                    item=item,
                )
                item_totals.append(ItemTotal(item.id, item.name, pool, ZERO))
                continue
            shares = self.allocate(item, scoped, pool) or {}
            distributed = ZERO
            kind = item.rule.kind if item.rule else AllocationKind.EQUAL
            fixed = kind is AllocationKind.FIXED_PER_UNIT
            for unit in scoped:
                share = shares.get(unit.id)
                if share is None or share.amount == ZERO:
                    continue
                distributed += share.amount
                lines_by_unit[unit.id][item.payer_rule].append(
                    PreviewLine(
                        budget_item_id=item.id,
                        description=item.name,
                        amount=share.amount,
                        allocation_kind=kind,
                        weight=share.weight,
                        weight_total=share.weight_total,
                        source_amount=share.amount if fixed else pool,
                        explanation=share.explanation,
                    )
                )
            # Sabit tutarda havuz yoktur: beklenen = bölüm başına tutar × bölüm sayısı.
            expected = distributed if fixed else pool
            item_totals.append(ItemTotal(item.id, item.name, expected, distributed))

        parties_by_unit: dict[uuid.UUID, list[PartyData]] = defaultdict(list)
        for party in data.parties:
            parties_by_unit[party.unit_id].append(party)
        accounts = {(a.unit_id, a.person_id, a.kind): a for a in data.accounts if not a.is_closed}
        charges: list[PreviewCharge] = []
        for unit in units:
            groups = lines_by_unit.get(unit.id)
            if not groups:
                continue
            for payer_rule in (PayerRule.OCCUPANT, PayerRule.OWNER):
                lines = groups.get(payer_rule)
                if not lines:
                    continue
                amount = sum((line.amount for line in lines), ZERO)
                found = self.payer(unit, payer_rule, parties_by_unit[unit.id])
                if found is None:
                    who = "malik" if payer_rule is PayerRule.OWNER else "malik veya kiracı"
                    self.warn(
                        WarningKind.NO_ACTIVE_PARTY,
                        f"{unit.name}: tahakkuk tarihinde etkin {who} yok; "
                        f"{format_money_tr(amount)} borç yazılmadı.",
                        unit_id=unit.id,
                    )
                    continue
                party, account_kind = found
                account = accounts.get((unit.id, party.person_id, account_kind))
                if account is None:
                    label = "malik" if account_kind is AccountKind.OWNER else "oturan"
                    self.warn(
                        WarningKind.MISSING_LEDGER_ACCOUNT,
                        f"{unit.name}: {party.person_name} için açık {label} hesabı yok; "
                        f"{format_money_tr(amount)} borç yazılmadı.",
                        unit_id=unit.id,
                    )
                    continue
                if not is_positive(abs(amount)):
                    continue
                charges.append(
                    PreviewCharge(
                        unit_id=unit.id,
                        unit_name=unit.name,
                        ledger_account_id=account.id,
                        reference_code=account.reference_code,
                        person_id=party.person_id,
                        person_name=party.person_name,
                        account_kind=account_kind,
                        lines=tuple(lines),
                    )
                )
        return Preview(charges, self.warnings, item_totals)


def build_preview(data: ChargeInput) -> Preview:
    return _Engine(data).run()
