"""Demo verisinin tanımı — docs/10-demo-veri.md §1–2. Saf veri; veritabanına dokunmaz.

Rastgele ama **sabit tohumlu**: her kurulumda aynı daireler, aynı m² ve arsa payları çıkar.
"""

import math
import random
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import PropertyKind, UnitUsage

# docs/10 §2: herkesçe bilinen parola; yalnız geliştirme ortamında kullanılır.
DEMO_PASSWORD = "Demo1234!"  # noqa: S105  # nosec B105
SEED = 20260930


@dataclass(frozen=True, slots=True)
class PlanSpec:
    name: str
    max_units: int | None
    max_storage_mb: int | None
    modules: tuple[ModuleKey, ...]
    sort_order: int


_BASIC = (ModuleKey.FINANCE, ModuleKey.ANNOUNCEMENTS, ModuleKey.REQUESTS)
_STANDARD = (*_BASIC, ModuleKey.DOCUMENTS, ModuleKey.VISITORS, ModuleKey.SURVEYS)

# docs/01 "Planlar"
PLANS: tuple[PlanSpec, ...] = (
    PlanSpec("Başlangıç", 30, 500, _BASIC, 1),
    PlanSpec("Standart", 150, 5000, _STANDARD, 2),
    PlanSpec("Pro", None, 50000, tuple(k for k in ModuleKey if k is not ModuleKey.PORTFOLIO), 3),
    PlanSpec("Yönetim Şirketi", None, None, tuple(ModuleKey), 4),
)

ORGANIZATION_NAME = "Kent Yönetim A.Ş."
ORGANIZATION_TAX_NUMBER = "1234567890"
ORGANIZATION_PLAN = "Yönetim Şirketi"


@dataclass(frozen=True, slots=True)
class BlockSpec:
    name: str  # tek bloklu sitede boş
    has_elevator: bool
    floor_count: int
    unit_count: int


@dataclass(frozen=True, slots=True)
class SiteSpec:
    name: str
    slug: str
    city: str
    district: str
    property_kind: PropertyKind
    plan: str
    blocks: tuple[BlockSpec, ...]
    extra_modules: tuple[ModuleKey, ...]
    iban: str


# docs/10 §1.3
SITES: tuple[SiteSpec, ...] = (
    SiteSpec(
        "Aksu Konakları", "aksu-konaklari", "İstanbul", "Beykoz", PropertyKind.MIXED, "Pro",
        (BlockSpec("A", True, 8, 16), BlockSpec("B", True, 8, 16), BlockSpec("C", False, 4, 16)),
        (ModuleKey.RESERVATIONS, ModuleKey.VISITORS, ModuleKey.PACKAGES, ModuleKey.VALET),
        "TR330006100519786457841326",
    ),
    SiteSpec(
        "Yıldız Sitesi", "yildiz-sitesi", "Ankara", "Çankaya", PropertyKind.RESIDENTIAL, "Standart",
        (BlockSpec("A", True, 6, 12), BlockSpec("B", False, 5, 12)),
        (ModuleKey.VISITORS,),
        "TR620001000222334455667788",
    ),
    SiteSpec(
        "Mimoza Apartmanı", "mimoza-apartmani", "İzmir", "Karşıyaka", PropertyKind.RESIDENTIAL,
        "Başlangıç",
        (BlockSpec("", False, 4, 12),),
        (),
        "TR110011100000000012345678",
    ),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class UnitSpec:
    block: str
    number: str
    floor: int
    unit_type: str
    gross_area: Decimal
    net_area: Decimal
    land_share_numerator: int
    usage: UnitUsage
    commercial_title: str | None


# (tip, olasılık ağırlığı, brüt m² aralığı)
_UNIT_MIX = (("1+1", 30, (65, 80)), ("2+1", 45, (95, 120)), ("3+1", 25, (130, 160)))
_SHOPS = ("Aksu Market", "Beykoz Eczanesi", "Konak Kuaför", "Boğaz Fırın")
_CENT = Decimal("0.01")


def units_for(site: SiteSpec) -> tuple[tuple[UnitSpec, ...], int]:
    """Sitenin bağımsız bölümleri ve arsa payı paydası (paylar toplamı = payda)."""
    # Güvenlik amaçlı değil: tekrarlanabilir demo verisi için sabit tohum.
    rng = random.Random(f"{SEED}:{site.slug}")  # noqa: S311  # nosec B311
    units: list[UnitSpec] = []
    shops = iter(_SHOPS)
    for block in site.blocks:
        per_floor = math.ceil(block.unit_count / block.floor_count)
        for index in range(block.unit_count):
            type_name, _, (low, high) = rng.choices(
                _UNIT_MIX, weights=[w for _, w, _ in _UNIT_MIX]
            )[0]
            gross = Decimal(rng.randint(low * 10, high * 10)) / 10
            # Karma kullanımlı sitede C bloğun zemin katı dükkan
            is_shop = site.property_kind is PropertyKind.MIXED and block.name == "C" and index < 4
            units.append(
                UnitSpec(
                    block=block.name,
                    number=str(index + 1),
                    floor=index // per_floor + (0 if is_shop else 1),
                    unit_type=type_name,
                    gross_area=gross.quantize(_CENT),
                    net_area=(gross * Decimal("0.85")).quantize(_CENT, rounding=ROUND_HALF_UP),
                    land_share_numerator=int((gross * 10).to_integral_value(ROUND_HALF_UP)),
                    usage=UnitUsage.COMMERCIAL if is_shop else UnitUsage.RESIDENTIAL,
                    commercial_title=next(shops) if is_shop else None,
                )
            )
    denominator = sum(unit.land_share_numerator for unit in units)
    return tuple(units), denominator


@dataclass(frozen=True, slots=True)
class AccountSpec:
    email: str
    full_name: str
    is_platform_admin: bool = False
    organization_role: str | None = None
    site_slug: str | None = None
    site_role: str | None = None


# docs/10 §2. Sakin hesabı (en borçlu oturan hesabın kişisi) kişi ve finans tabloları gelince.
ACCOUNTS: tuple[AccountSpec, ...] = (
    AccountSpec("platform@demo.local", "Deniz Aksoy", is_platform_admin=True),
    AccountSpec("yonetici@demo.local", "Kerem Yıldırım", organization_role="Sahip"),
    AccountSpec("muhasebe@demo.local", "Selin Arı", organization_role="Muhasebe"),
    AccountSpec(
        "mimoza@demo.local", "Hakan Tunç", site_slug="mimoza-apartmani", site_role="Yönetici"
    ),
    AccountSpec(
        "guvenlik@demo.local", "Recep Er", site_slug="aksu-konaklari", site_role="Güvenlik"
    ),
    AccountSpec("denetci@demo.local", "Nuray Şen", site_slug="aksu-konaklari", site_role="Denetçi"),
    AccountSpec(
        "teknik@demo.local", "Ergün Kılıç", site_slug="aksu-konaklari", site_role="Teknik Personel"
    ),
)
