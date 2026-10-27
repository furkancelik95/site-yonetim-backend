"""Aidat kuralları — saf kurallar (Yazılım Dokümanı FIN-02, §8.7; PM K23).

Site tahakkuk kaynağını seçer (finans parametresi `charge_source`): işletme projesi olan site
projeyle, olmayan aidat kurallarıyla çalışır. Kural formül yazmadan tanımlanır:

- Kural tipi: eşit · m² (brüt/net) · arsa payı · bölüm tipi (katsayılarla) · bölüm başına sabit ·
  bileşik (ör. %70 arsa payı + %30 eşit). Tüketim ve blok/sabit+oran tipleri sonraki adım.
- Tutar: bölüm başına sabitte bölüm başına tutar, diğerlerinde dönemde dağıtılacak toplam.
- Kapsam: tesis / blok / bölüm tipi / kullanım. Aynı kalem tipinde kapsamlar çakışamaz.
- Bölüm tipi katsayıları (dükkan 1,5 · daire 1,0), bölüm muafiyetleri (indirim oranı + gerekçe),
  malik kalemi (malike tahakkuk eder, kiracıya görünmez).
- Versiyonlu: değişiklik yeni sürümdür ve yalnız yürürlüğünden sonraki dönemleri etkiler.
"""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from site_yonetim.domain.charging.engine import (
    ComponentData,
    Exemption,
    ItemData,
    ItemSource,
    RuleData,
)
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import (
    AllocationKind,
    AreaBasis,
    Frequency,
    PayerRule,
    ScopeKind,
)
from site_yonetim.domain.money import ZERO, round_money

HUNDRED = Decimal(100)
NAME_MIN, NAME_MAX = 2, 120
NOTE_MIN, NOTE_MAX = 3, 300
REASON_MIN, REASON_MAX = 2, 100
AMOUNT_MAX = Decimal("100000000")

RULE_KINDS = frozenset(
    {
        AllocationKind.EQUAL,
        AllocationKind.BY_AREA,
        AllocationKind.BY_LAND_SHARE,
        AllocationKind.BY_UNIT_TYPE_WEIGHT,
        AllocationKind.FIXED_PER_UNIT,
        AllocationKind.COMPOSITE,
    }
)
RULE_COMPONENT_KINDS = frozenset(
    {
        AllocationKind.EQUAL,
        AllocationKind.BY_AREA,
        AllocationKind.BY_LAND_SHARE,
        AllocationKind.BY_UNIT_TYPE_WEIGHT,
    }
)
RULE_FREQUENCIES = frozenset({Frequency.MONTHLY, Frequency.QUARTERLY, Frequency.YEARLY})


@dataclass(frozen=True, slots=True)
class VersionFields:
    effective: YearMonth
    kind: AllocationKind
    amount: Decimal
    basis_note: str
    area_basis: AreaBasis = AreaBasis.GROSS
    frequency: Frequency = Frequency.MONTHLY
    components: tuple[ComponentData, ...] = ()
    type_coefficients: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    exemptions: dict[uuid.UUID, Exemption] = field(default_factory=dict)
    scope_kind: ScopeKind = ScopeKind.WHOLE_SITE
    scope_block_ids: frozenset[uuid.UUID] = frozenset()
    scope_unit_type_ids: frozenset[uuid.UUID] = frozenset()
    scope_usage: str | None = None
    discontinued: bool = False

    @property
    def amount_mode(self) -> str:
        return "per_unit" if self.kind is AllocationKind.FIXED_PER_UNIT else "total"


def check_name(name: str) -> str | None:
    text = " ".join(name.split())
    if not NAME_MIN <= len(text) <= NAME_MAX:
        return f"Ad {NAME_MIN}–{NAME_MAX} karakter olmalı."
    return None


def check_version(v: VersionFields) -> dict[str, str]:
    """Alan hataları (422). Boşsa sürüm kaydedilebilir."""
    errors: dict[str, str] = {}
    if v.kind not in RULE_KINDS:
        errors["kind"] = (
            "Kural tipi eşit, m², arsa payı, bölüm tipi, bölüm başına sabit ya da bileşik olmalı."
        )
    if v.frequency not in RULE_FREQUENCIES:
        errors["frequency"] = "Dönem aylık, 3 aylık ya da yıllık olmalı."
    if v.amount != round_money(v.amount) or v.amount < ZERO or v.amount > AMOUNT_MAX:
        errors["amount"] = "Tutar 0 ile 100.000.000 arasında, en fazla 2 ondalık olmalı."
    elif v.amount == ZERO and not v.discontinued:
        errors["amount"] = "Tutar sıfırdan büyük olmalı."
    note = " ".join(v.basis_note.split())
    if not NOTE_MIN <= len(note) <= NOTE_MAX:
        errors["basis_note"] = (
            "Dayanak zorunlu (ör. 'Yönetim planı md. 12' ya da kurul kararı tarihi ve no)."
        )
    if v.kind is AllocationKind.COMPOSITE:
        if not v.components:
            errors["components"] = "Bileşik kuralda en az iki bileşen olmalı."
        elif any(c.kind not in RULE_COMPONENT_KINDS for c in v.components):
            errors["components"] = "Bileşen tipi eşit, m², arsa payı ya da bölüm tipi olmalı."
        elif any(not ZERO < c.percent <= HUNDRED for c in v.components):
            errors["components"] = "Bileşen yüzdesi 0'dan büyük, en fazla 100 olmalı."
        elif sum((c.percent for c in v.components), ZERO) != HUNDRED:
            errors["components"] = "Bileşen yüzdelerinin toplamı 100 olmalı."
    if any(not ZERO < c <= HUNDRED for c in v.type_coefficients.values()):
        errors["type_coefficients"] = "Katsayı 0'dan büyük, en fazla 100 olmalı."
    for exemption in v.exemptions.values():
        reason = " ".join(exemption.reason.split())
        if not ZERO < exemption.percent <= HUNDRED:
            errors["exemptions"] = "Muafiyet oranı %0'dan büyük, en fazla %100 olmalı."
        elif not REASON_MIN <= len(reason) <= REASON_MAX:
            errors["exemptions"] = "Her muafiyete gerekçe yazın (ör. boş daire, personel dairesi)."
    if v.scope_kind is ScopeKind.BLOCKS and not v.scope_block_ids:
        errors["scope_block_ids"] = "Kapsamdaki blokları seçin."
    if v.scope_kind is ScopeKind.UNIT_TYPES and not v.scope_unit_type_ids:
        errors["scope_unit_type_ids"] = "Kapsamdaki bölüm tiplerini seçin."
    if v.scope_kind is ScopeKind.USAGE and not v.scope_usage:
        errors["scope_usage"] = "Kapsamdaki kullanım tipini seçin."
    return errors


def scopes_overlap(a: VersionFields, b: VersionFields) -> bool:
    """İki kapsam ortak bölüm içerebilir mi? Farklı türlerde (blok × tip) kesişim olası sayılır."""
    if ScopeKind.WHOLE_SITE in (a.scope_kind, b.scope_kind) or a.scope_kind != b.scope_kind:
        return True
    match a.scope_kind:
        case ScopeKind.BLOCKS:
            return bool(a.scope_block_ids & b.scope_block_ids)
        case ScopeKind.UNIT_TYPES:
            return bool(a.scope_unit_type_ids & b.scope_unit_type_ids)
        case _:
            return a.scope_usage == b.scope_usage


def is_due(frequency: Frequency, period: YearMonth) -> bool:
    """Aylık her ay; 3 aylık Ocak/Nisan/Temmuz/Ekim; yıllık Ocak."""
    if frequency is Frequency.QUARTERLY:
        return period.month in (1, 4, 7, 10)
    if frequency is Frequency.YEARLY:
        return period.month == 1
    return True


def to_item(
    version_id: uuid.UUID,
    name: str,
    v: VersionFields,
    *,
    payer_rule: PayerRule,
    owner_item: bool,
) -> ItemData:
    """Motor girdisi. Malik kalemi her durumda malike yazılır."""
    fixed = v.kind is AllocationKind.FIXED_PER_UNIT
    return ItemData(
        id=version_id,
        name=name,
        annual_amount=ZERO,
        frequency=v.frequency,
        rule=RuleData(
            id=version_id,
            name=name,
            kind=v.kind,
            area_basis=v.area_basis,
            fixed_amount=v.amount if fixed else None,
            components=v.components,
            type_coefficients=v.type_coefficients,
        ),
        payer_rule=PayerRule.OWNER if owner_item else payer_rule,
        scope_kind=v.scope_kind,
        scope_block_ids=v.scope_block_ids,
        scope_unit_type_ids=v.scope_unit_type_ids,
        scope_usage=v.scope_usage,
        source=ItemSource.BILLING_RULE,
        pool=ZERO if fixed else v.amount,
        exemptions=v.exemptions,
    )


def periods_per_year(frequency: Frequency) -> int:
    return {Frequency.MONTHLY: 12, Frequency.QUARTERLY: 4}.get(frequency, 1)
