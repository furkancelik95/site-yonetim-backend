"""KVKK saklama politikası — saf kurallar (Yazılım Dokümanı RAP-04 "Saklama politikası", §4.4,
§10.2; PM kararı K8: "dokümandaki varsayılanlar — kanunda belirtilen süreler kadar").

Kategori × süre tablosu site ayarıdır (platform üst sınırlarıyla). Gece işi süresi dolan veriyi
siler ya da anonimleştirir ve **silme raporu** (`retention_runs`) üretir. Finansal kayıtlar yasal
saklama süresince tutulur — otomatik işleme girmez.

Hukuki teyit bekleyen kategorilerde (A-04) otomatik uygulama **varsayılan kapalıdır**; yönetici
süreyi görür, bekleyen kayıt sayısını izler, isterse elle uygular ya da otomatiği açar.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from site_yonetim.domain.gatehouse import months_before


class RetentionAction(StrEnum):
    ANONYMIZE = "anonymize"  # kayıt kalır, kişisel alanlar silinir
    DELETE = "delete"  # kayıt silinir
    KEEP = "keep"  # yasal saklama; otomatik işlem yok


ACTION_LABELS: dict[RetentionAction, str] = {
    RetentionAction.ANONYMIZE: "Anonimleştirilir",
    RetentionAction.DELETE: "Silinir",
    RetentionAction.KEEP: "Yasal süre boyunca saklanır (otomatik silinmez)",
}


class RetentionUnit(StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


UNIT_LABELS: dict[RetentionUnit, str] = {
    RetentionUnit.DAY: "gün",
    RetentionUnit.MONTH: "ay",
    RetentionUnit.YEAR: "yıl",
}


class RunTrigger(StrEnum):
    JOB = "job"  # gece işi
    MANUAL = "manual"  # yöneticinin "şimdi uygula" kararı


@dataclass(frozen=True, slots=True)
class RetentionCategory:
    key: str
    label: str
    data: str  # hangi kayıtlar, hangi alanlar
    kvkk_category: str  # §4.4: identity, contact, finance, location_plate, image, employment
    subjects: str  # ilgili kişi grubu
    default_value: int
    unit: RetentionUnit
    max_value: int | None
    action: RetentionAction
    legal_basis: str
    needs_legal_review: bool  # A-04: hukuki teyit bekliyor → otomatik varsayılan kapalı

    @property
    def adjustable(self) -> bool:
        return self.action is not RetentionAction.KEEP


VISITORS = "visitors"
REGISTRATIONS = "registrations"
EMERGENCY = "emergency"
FINANCE = "finance"
EMPLOYMENT = "employment"

CATEGORIES: tuple[RetentionCategory, ...] = (
    RetentionCategory(
        VISITORS,
        "Ziyaretçi ve plaka kayıtları",
        "Ziyaretçi adı, telefonu, plakası; ön davet, kapı günlüğü, park ihlali, kargo teslim "
        "yetkilisi",
        "location_plate",
        "Ziyaretçiler, sakinler",
        6,
        RetentionUnit.MONTH,
        24,
        RetentionAction.ANONYMIZE,
        "Yazılım Dokümanı §10.2: varsayılan 6 ay, platform sınırı en fazla 2 yıl. Güvenlik "
        "ayarlarındaki süreyle aynı ayardır.",
        True,
    ),
    RetentionCategory(
        REGISTRATIONS,
        "Reddedilen kayıt başvuruları",
        "Başvuranın adı, soyadı, telefonu, e-postası ve IP adresi (karar tarihinden itibaren)",
        "identity",
        "Başvuranlar",
        6,
        RetentionUnit.MONTH,
        24,
        RetentionAction.ANONYMIZE,
        "Dokümanda süre yok; ziyaretçi varsayılanı (6 ay) öneri olarak alındı — hukuki teyit "
        "bekliyor (A-04).",
        True,
    ),
    RetentionCategory(
        EMERGENCY,
        "Acil durum yoklama ayrıntısı",
        'Acil durumda kişi bazında "güvendeyim / yardım" yanıtları',
        "location_plate",
        "Sakinler, personel",
        30,
        RetentionUnit.DAY,
        365,
        RetentionAction.DELETE,
        "Yazılım Dokümanı §4.6 ve ILT-06: acil durum bitiminden 30 gün sonra silinir.",
        True,
    ),
    RetentionCategory(
        FINANCE,
        "Finansal kayıtlar",
        "Tahakkuk, tahsilat, iade, gider, kasa ve banka hareketleri, makbuz ve belgeler",
        "finance",
        "Malikler, kiracılar, tedarikçiler",
        10,
        RetentionUnit.YEAR,
        None,
        RetentionAction.KEEP,
        "Kanuni süre: 6102 sayılı TTK m.82 (10 yıl), 213 sayılı VUK m.253. İlgili kişinin "
        "silme başvurusunda bu kayıtlar saklanır, gerekçe yanıtta yazılır.",
        False,
    ),
    RetentionCategory(
        EMPLOYMENT,
        "Personel ve puantaj kayıtları",
        "Personel kartı, vardiya, izin, puantaj ve giriş-çıkış kayıtları",
        "employment",
        "Personel",
        10,
        RetentionUnit.YEAR,
        None,
        RetentionAction.KEEP,
        "Kanuni süre: iş ilişkisinin bitiminden itibaren genel zamanaşımı (6098 sayılı TBK "
        "m.146, 10 yıl) — hukuki teyit bekliyor (A-04).",
        True,
    ),
)
BY_KEY: dict[str, RetentionCategory] = {c.key: c for c in CATEGORIES}


class RetentionFieldError(ValueError):
    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(fields.values()))
        self.fields = fields


def check_value(category: RetentionCategory, value: object) -> int:
    """Süreyi doğrular: tam sayı, en az 1, platform üst sınırı kadar."""
    unit = UNIT_LABELS[category.unit]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RetentionFieldError({category.key: f"Süre en az 1 {unit} olmalı."})
    if category.max_value is not None and value > category.max_value:
        raise RetentionFieldError(
            {category.key: f"Süre en fazla {category.max_value} {unit} olabilir."}
        )
    return value


def cutoff(category: RetentionCategory, value: int, today: date) -> date:
    """Bu tarihten **önceki** kayıtların süresi dolmuştur."""
    if category.unit is RetentionUnit.DAY:
        return today - timedelta(days=value)
    if category.unit is RetentionUnit.MONTH:
        return months_before(today, value)
    return months_before(today, value * 12)
