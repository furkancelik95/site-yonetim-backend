"""Rapor kataloğu ve metrik sözlüğü — saf (Yazılım Dokümanı RAP-01, §18.2; servis isteği 79).

**Metrik sözlüğü tek kaynaktır:** ekranlardaki "bu metrik nasıl hesaplanır" metinleri buradan
gelir. Katalog yetkiye (rapor bazında izin) ve sitenin açık modüllerine göre süzülür; yetkisiz
rapor katalogda görünmez, ucu 403 döner (RAP-01 S1: YK güvenlik raporunu göremez; denetçi yalnız
finans, satın alma ve denetim raporlarını görür).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from site_yonetim.domain.access import Permission
from site_yonetim.domain.modules import ModuleKey


@dataclass(frozen=True, slots=True)
class Metric:
    key: str
    name: str
    definition: str


METRICS: dict[str, Metric] = {
    m.key: m
    for m in (
        Metric("income", "Gelir", "Dönemde yapılan onaylı tahsilatlar ve elle kasa girişleri "
               "(tahakkuk değil)."),
        Metric("expense", "Gider", "Dönemde kaydedilen, ters kaydı olmayan giderler; kategori "
               "bazında."),
        Metric("collection_rate", "Tahsilat oranı",
               "Dönemde tahsil edilen / dönemde vadesi gelen."),
        Metric("aging_bucket", "Yaş dilimi", "En eski açık borcun vadesinden bugüne gün: "
               "0–30 / 31–60 / 61–90 / 90+."),
        Metric("budget_usage", "Gerçekleşme oranı", "Kategoride yıl içinde gerçekleşen gider / "
               "işletme projesindeki yıllık tutar."),
        Metric("cash_balance", "Bakiye", "Hesap hareketlerinden hesaplanır; ayrıca saklanmaz."),
        Metric("sla_rate", "SLA uyumu", "Dönemde çözülen taleplerden SLA süresi içinde "
               "çözülenlerin oranı (SLA kuralı olmayan talepler hariç)."),
        Metric("avg_resolution", "Ortalama çözüm süresi", "Dönemde çözülen taleplerde açılıştan "
               "çözüme geçen takvim saati ortalaması; çözüm zamanı açılıştan önce olan bozuk "
               "kayıtlar ortalamaya girmez."),
        Metric("satisfaction", "Memnuniyet", "Dönemde çözülen taleplerin sakin kapanış "
               "puanlarının ortalaması (1–5); talep başına son puan."),
        Metric("patrol_completion", "Devriye tamamlama", "Okutulan nokta / plandaki nokta."),
        Metric("overtime", "Fazla mesai", "Vardiya süresini aşan çalışma saatleri."),
        Metric("po_lead_time", "Talep–sipariş süresi", "Satın alma talebinin açılışından "
               "siparişe geçen gün."),
        Metric("vendor_score", "Tedarikçi puanı", "Mal/hizmet kabul puanlarının ortalaması."),
        Metric("occupancy", "Doluluk", "Kullanılan saat / açık saat × kapasite."),
        Metric("no_show", "Gelmedi oranı", "Doğrulama yapılan alanlarda gelinmeyen / biten "
               "rezervasyon."),
        Metric("consumption", "Tüketim", "(Endeks − önceki endeks) × çarpan."),
        Metric("audit_entry", "Kayıt", "Kim, ne zaman, neyi değiştirdi (önce/sonra farkı)."),
    )
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class CatalogReport:
    key: str
    name: str
    group: str
    permission: Permission
    link: str  # frontend ekran yolu (site altında)
    metric_keys: tuple[str, ...]
    module: ModuleKey | None = None  # modül kapalıysa katalogda yok

    @property
    def metrics(self) -> tuple[Metric, ...]:
        return tuple(METRICS[k] for k in self.metric_keys)


P = Permission
CATALOG: tuple[CatalogReport, ...] = (
    CatalogReport("income_expense", "Gelir–gider", "Finans", P.FINANCE_REPORTS_READ,
                  "raporlar", ("income", "expense")),
    CatalogReport("collections", "Tahsilat oranı ve trendi", "Finans", P.FINANCE_REPORTS_READ,
                  "raporlar", ("collection_rate",)),
    CatalogReport("aging", "Borç yaşlandırma", "Finans", P.FINANCE_READ, "borclular",
                  ("aging_bucket",)),
    CatalogReport("budget", "Bütçe–gerçekleşme", "Finans", P.FINANCE_READ, "isletme-projesi",
                  ("budget_usage",)),
    CatalogReport("cash", "Kasa ve banka özeti", "Finans", P.FINANCE_CASH_READ, "kasa",
                  ("cash_balance",)),
    CatalogReport("requests_sla", "Talep ve SLA", "Operasyon", P.REQUESTS_READ,
                  "rapor-katalogu?rapor=requests_sla",
                  ("sla_rate", "avg_resolution", "satisfaction"), ModuleKey.REQUESTS),
    CatalogReport("security", "Güvenlik ve devriye", "Operasyon", P.SECURITY_INCIDENTS,
                  "guvenlik?sekme=devriye", ("patrol_completion",), ModuleKey.VISITORS),
    CatalogReport("hr", "Puantaj ve izin", "İK", P.STAFF_MANAGE, "puantaj", ("overtime",),
                  ModuleKey.STAFF),
    CatalogReport("purchasing", "Satın alma ve sözleşme karnesi", "Satın alma",
                  P.EXPENSES_READ, "satin-alma", ("po_lead_time", "vendor_score")),
    CatalogReport("facility", "Tesis kullanımı", "Operasyon", P.REQUESTS_ASSIGN,
                  "rezervasyon?sekme=rapor", ("occupancy", "no_show"), ModuleKey.RESERVATIONS),
    CatalogReport("meters", "Sayaç ve tüketim", "Operasyon", P.UNITS_MANAGE, "sayaclar",
                  ("consumption",)),
    CatalogReport("audit", "Denetim kaydı", "Yönetim", P.AUDIT_READ, "denetim",
                  ("audit_entry",)),
)  # fmt: skip
BY_KEY: dict[str, CatalogReport] = {r.key: r for r in CATALOG}


def visible(
    permissions: Iterable[Permission | str], modules: Iterable[ModuleKey]
) -> list[CatalogReport]:
    granted = set(permissions)
    open_modules = set(modules)
    return [
        r
        for r in CATALOG
        if r.permission in granted and (r.module is None or r.module in open_modules)
    ]


# --- Talep ve SLA raporu -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlaCounts:
    """Veritabanında toplanmış sayılar (kategori başına)."""

    total: int = 0  # dönemde açılan
    open: int = 0  # dönemde açılıp hâlâ açık
    closed: int = 0  # dönemde çözülen (çözüldü/kapandı)
    with_sla: int = 0  # dönemde çözülen ve SLA kuralı olan
    met: int = 0  # bunlardan süresinde çözülen
    seconds: Decimal = Decimal(0)  # geçerli çözüm sürelerinin toplamı
    timed: int = 0  # çözüm zamanı açılıştan önce olmayanlar
    score_sum: int = 0
    rated: int = 0

    def __add__(self, other: SlaCounts) -> SlaCounts:
        return SlaCounts(
            self.total + other.total,
            self.open + other.open,
            self.closed + other.closed,
            self.with_sla + other.with_sla,
            self.met + other.met,
            self.seconds + other.seconds,
            self.timed + other.timed,
            self.score_sum + other.score_sum,
            self.rated + other.rated,
        )


@dataclass(frozen=True, slots=True)
class SlaLine:
    total: int
    open: int
    closed: int
    sla_rate: Decimal | None  # %, bir ondalık
    avg_hours: Decimal | None
    satisfaction: Decimal | None
    rated: int


_ONE = Decimal("0.1")


def _ratio(value: Decimal, count: int) -> Decimal:
    return (value / count).quantize(_ONE, ROUND_HALF_UP)


def sla_line(c: SlaCounts) -> SlaLine:
    return SlaLine(
        total=c.total,
        open=c.open,
        closed=c.closed,
        sla_rate=_ratio(Decimal(c.met * 100), c.with_sla) if c.with_sla else None,
        avg_hours=_ratio(c.seconds / 3600, c.timed) if c.timed else None,
        satisfaction=_ratio(Decimal(c.score_sum), c.rated) if c.rated else None,
        rated=c.rated,
    )
