# 01 — Ürün

## Ne yapıyoruz

Toplu yaşam ve çalışma alanlarının (site, apartman, rezidans, iş merkezi, AVM) günlük yönetimini
tek platformdan yürüten, **çok kiracılı** bir yönetim sistemi.

Sistemin çekirdeği **para**: aidatın doğru hesaplanması, doğru kişiye yazılması, tahsilatın doğru
borca sayılması ve bunların her birinin denetlenebilir olması. Diğer her şey (talep, duyuru,
güvenlik) bu çekirdeğin etrafında durur.

## Kim kullanıyor

| Kullanıcı | Ne yapar | Ne görür |
|---|---|---|
| **Platform yöneticisi** | Ürünü işleten taraf. Müşteri ekler, site açar, plan atar | Müşteri/site/plan listesi ve kullanım ölçüsü. **Hiçbir müşterinin borcunu, sakin bilgisini görmez** |
| **Yönetim şirketi sahibi** | Birden çok siteyi yönetir | Portföyündeki bütün siteler |
| **Site yöneticisi** | Tek bir sitenin her işi | O sitenin her şeyi |
| **Muhasebe** | Tahakkuk, tahsilat, gider, kasa | Finans ekranları |
| **Yönetim kurulu üyesi** | Karar verir, işlem yapmaz | Her şeyi görür, hiçbirini değiştiremez |
| **Denetçi** (KMK m.41) | Hesapları denetler | Salt okunur finans. Kişisel veri görmez |
| **Güvenlik** | Kapıdaki iş | Yalnızca ziyaretçi ve kargo. Borç, kişi listesi, muhasebe **yok** |
| **Teknik personel** | Arıza ve iş emri | Yalnızca talepler |
| **Sakin** (malik veya kiracı) | Kendi dairesi | Kendi borcu, ödemeleri, duyurular, kendi talepleri |

## Hiyerarşi

```
Platform
└── Organization (yönetim şirketi)           — isteğe bağlı; bağımsız site da olabilir
    └── Site (tenant — izolasyon sınırı)
        └── Block (blok)
            └── Unit (bağımsız bölüm: daire, dükkan, depo, otopark)
                └── UnitParty (malik / kiracı / oturan / vekil — tarih aralıklı)
                    └── Person
```

**Tenant = Site.** Veri izolasyonu site düzeyindedir. Bir yönetim şirketi çalışanı, şirketin
bütün sitelerine erişir ama her istek yine **tek bir site** kapsamında çalışır.

## Modüller

Modüller site bazında açılıp kapanır. Kapatılan modülün verisi **silinmez**, salt okunur kalır;
modül yeniden açılınca eski kayıtlar yerindedir.

| Anahtar | Modül | Durum (referans uygulamada) |
|---|---|---|
| `finance` | Aidat, tahakkuk, tahsilat, borç, gider, kasa, rapor | **Çekirdek — kapatılamaz** · var |
| `announcements` | Duyuru | var |
| `requests` | Talep / arıza / iş emri | var |
| `visitors` | Ziyaretçi ve araç girişi | var |
| `packages` | Kargo | var |
| `documents` | Doküman | dosya altyapısı var, ekranı yok |
| `reservations` | Sosyal tesis rezervasyonu | yok |
| `valet` | Vale | yok |
| `general-assembly` | Genel kurul, karar | yok |
| `surveys` | Anket | yok |
| `staff` | Personel | yok |
| `portfolio` | Çoklu site portföy görünümü | var |

Bir modülün kullanılabilmesi için iki şart vardır: **sitenin planı o modüle izin vermeli** ve
**sitede açık olmalı.** Plan izin vermiyorsa yönetici açamaz.

## Planlar

| Plan | Bölüm tavanı | Depolama | İzin verilen modüller |
|---|---|---|---|
| Başlangıç | 30 | 500 MB | finance, announcements, requests |
| Standart | 150 | 5.000 MB | + documents, visitors, surveys |
| Pro | sınırsız | 50.000 MB | portfolio hariç hepsi |
| Yönetim Şirketi | sınırsız | sınırsız | hepsi |

Tavan aşılınca kullanım durmaz; platform panelinde "tavan aşıldı" uyarısı çıkar.
Fiyatlar henüz belirlenmedi (`12-acik-kararlar.md`).

## Hukuki çerçeve — kod bunlara göre davranır

| Kural | Ne der | Sistemde karşılığı |
|---|---|---|
| **KMK m.20** | Geciken aidat için aylık %5 gecikme tazminatı | `LateFeePolicy` — `04-is-kurallari.md` §6 |
| **KMK m.22** | Demirbaş/yatırım gideri malike, işletme gideri oturana | `ChargeType.payer_rule` — §4 |
| **KMK m.37** | İşletme projesi (bütçe) tebliğ edilir, 7 gün itiraz süresi, sonra kesinleşir | `BudgetPlan.status` — §3 |
| **KMK m.41** | Denetçi hesapları denetler | Denetçi rolü: salt okunur |
| **İİK m.68** | Kesinleşmiş işletme projesi icra takibinin dayanağıdır | Kesinleşmiş proje değiştirilemez |
| **6493 sayılı Kanun** | Başkasının parasına aracılık lisans gerektirir | Platform **para tutmaz, parayı üzerinden geçirmez**. Online ödeme lisanslı PSP üzerinden |
| **KVKK** | Kişisel verinin işlenmesi | `09-guvenlik-kvkk.md` |
| **TBK m.100** | Kısmi ödeme önce faize/masrafa sayılır | Mahsup sırası — §5 |

## Kapsam dışı (şimdilik)

- Resmi muhasebe (yevmiye, büyük defter, e-defter, beyanname). Sistem **işletme defteri** tutar
  ve muhasebe programına aktarım üretir; resmi defter tutmaz.
- Fatura kesme (e-fatura/e-arşiv). Site yönetimi KDV mükellefi değildir.
- Bordro.

## Referans

- Ürün kapsam dokümanı: `Site_Tesis_Yonetim_Platformu_Master_PRD_v1_2.docx` (ortaklarda)
- Referans kod: `brhnnkaraa6/siteyonetimi` → `src/SiteYonetimi.Domain/`
