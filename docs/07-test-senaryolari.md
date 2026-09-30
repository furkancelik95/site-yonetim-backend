# 07 — Test senaryoları (altın testler)

Bu senaryolar referans uygulamada **geçen** testlerdir (101 test). Python'a **birebir** çevir:
aynı girdi, aynı beklenen değer. Bir senaryo geçmiyorsa kural yanlış taşınmıştır — testi
değiştirme, kodu düzelt.

- §1–2 ve §5 **saf alan testleri**dir: veritabanı yok, milisaniyede koşar.
- §3–4, §6 **entegrasyon testleri**dir: gerçek PostgreSQL (test veritabanı ya da testcontainers).
- İsimlendirme önerisi: `test_esit_dagitimda_toplam_korunur` gibi Türkçe, okunur.

---

## 0. Test sitesi (tahakkuk testleri için ortak kurgu)

| | |
|---|---|
| Bloklar | **A** (asansörlü, 6 kat) · **B** (asansörsüz, 4 kat) |
| Daire tipleri | **1+1** ağırlık `1.0` · **2+1** ağırlık `1.3` |
| A blok, 12 daire (`1`…`12`) | **çift** numara: 2+1, 110 m², arsa payı 45/1000 · **tek**: 1+1, 75 m², 33/1000 |
| B blok, 12 daire (`1`…`12`) | **3'ün katı**: 2+1, 105 m², 42/1000 · **diğerleri**: 1+1, 72 m², 31/1000 |
| Toplam | **24 aktif daire** |
| Malik | her dairede; soyadı `MALİK`, başlangıç 2020-01-01, **malik hesabı** (`{blok}{no}-M`) |
| Kiracı | **çift numaralı** dairelerde; soyadı `KİRACI`, başlangıç 2025-06-01, **oturan hesabı** kiracıya (`-K`) |
| Kiracısız daire | **oturan hesabı malike** açılır (`-O`) |
| Tahakkuk tipleri | **Aidat** → `occupant` · **Demirbaş** → `owner` |
| Gider kategorileri | İşletme (`operating`) · Demirbaş (`capital_improvement`) |
| Tarihler | `charge_date = 2027-01-01`, `due_date = 2027-01-15` |

Tahakkuk tutarı aylık kalemde `yıllık / 12`, `one_time` kalemde yıllık tutarın tamamıdır.

## 1. Kuruş kaybı olmadan dağıtım (`distribute`)

| # | Senaryo | Girdi | Beklenen |
|---|---|---|---|
| 1.1 | Eşit dağıtımda toplam korunur | `distribute(100, [1,1,1])` | `[33.34, 33.33, 33.33]`, toplam `100` |
| 1.2 | Ağırlıklı dağıtımda toplam korunur | `20000`, farklı pozitif ağırlıklar | toplam `20000`, **her pay > 0** |
| 1.3 | Arsa payına göre toplam korunur | `180000`, arsa payı oranları | toplam `180000` |
| 1.4 | Bölünmeyen kuruşlar kaybolmaz | `(0.01, 3)`, `(0.02, 3)`, `(1000.05, 7)`, `(99999.99, 13)` — eşit ağırlık | her birinde toplam = girdi |
| 1.5 | Tek daireye tamamı yazılır | `distribute(1234.56, [1])` | `[1234.56]` |
| 1.6 | Sıfır ağırlık toplamı reddedilir | `distribute(x, [0, 0])` | `ValueError` |
| 1.7 | Boş liste boş sonuç | `distribute(x, [])` | `[]` |
| 1.8 | *(öneri, ek)* Rastgele özellik testi | 20.000 rastgele tutar (**negatif dahil**) × rastgele ağırlık listesi | her birinde `sum == total` |
| 1.9 | Yuvarlama yarım yukarı | `round_money(2.345)`, `round_money(-2.345)` | `2.35`, `-2.35` |

## 2. Tahakkuk motoru (test sitesi §0)

| # | Senaryo | Kurgu | Beklenen |
|---|---|---|---|
| 2.1 | Eşit dağıtımda kalem toplamı kuruşu kuruşuna tutar | "Kapıcı maaşı" 240.000/yıl, aylık, `equal`, Aidat | kalem toplamı **20.000**, `unit_count = 24` |
| 2.2 | Arsa payı dağıtımında toplam korunur | "Çatı onarımı" 180.000, **one_time**, `by_land_share`, Demirbaş | kalem toplamı **180.000** |
| 2.3 | Metrekare dağıtımında toplam korunur | "Isıtma sabit payı" 600.000/yıl, aylık, `by_area` (brüt) | **50.000** |
| 2.4 | Daire tipi ağırlığıyla toplam korunur | "Yönetim ücreti" 96.000/yıl, aylık, `by_unit_type_weight` | **8.000** |
| 2.5 | Bileşik kural toplamı korur | "Merkezi ısıtma" 600.000/yıl, aylık, composite **%30 `by_area` + %70 `equal`** | **50.000**, **uyarı yok** |
| 2.6 | Çok kalemli projede her kalem ayrı ayrı tutar | 2.1 + 2.3 + 2.2 + 2.4 birlikte | Kapıcı 20.000 · Isıtma 50.000 · Çatı 180.000 · Yönetim 8.000 · **toplam 258.000** |
| 2.7 | Asansör bakımı yalnız asansörlü bloğa yazılır | 36.000/yıl, aylık, `equal`, kapsam `blocks=[A]` | **3.000**, **12 borç** (yalnız A) |
| 2.8 | Boş kapsam uyarı üretir, çökmez | kapsamda hiç daire yok | uyarı `empty_scope` |
| 2.9 | Aidat kiracıya, demirbaş malike yazılır | A-2 (kiracılı): Aidat 240.000 `equal` + Çatı 180.000 one_time `by_land_share` | A-2 için **2 borç**: oturan borcu `…KİRACI` adına "Aidat" satırıyla, malik borcu `…MALİK` adına "Çatı onarımı" satırıyla |
| 2.10 | Kiracı yoksa aidat malikin oturan hesabına | A-1 (kiracısız), Aidat | A-1 borcu `account_kind = occupant`, ödeyen `…MALİK` |
| 2.11 | Kiracı çıkmışsa o dönemde borç malike | A-2 kiracısının `end_date = 2026-12-31`, Aidat, `charge_date = 2027-01-01` | A-2 borcu `…MALİK` adına |
| 2.12 | Aktif taraf yoksa uyarı, koşu durmaz | A-1'in **tüm** taraflarına `end_date = 2026-01-01`, Aidat | uyarı `no_active_party` (`unit_id = A-1`), A-1'e borç **yok**, `unit_count = 23` |
| 2.13 | Her satır kendi dayanağını taşır | 2.3 kurgusu | satır: `allocation_kind = by_area`, `source_amount = 50.000`, `explanation` içinde `m²` |
| 2.14 | Sabit tutar her daireye aynı tutarı yazar | "Otopark bedeli" `annual_amount = 0`, `fixed_per_unit` **250** | **her** borç 250, toplam `24 × 250 = 6.000` |
| 2.15 | Arsa payı eksik daireler uyarı, diğerleri dağıtılır | A-1'in `land_share_numerator = None`, Çatı 180.000 one_time | uyarı `missing_weight_data`, kalem toplamı **yine 180.000**, A-1'e borç yok |
| 2.16 | Bileşik yüzdeler 100 değilse uyarır ama dağıtır | composite **%30 `by_area` + %50 `equal`** (toplam 80), 600.000/yıl aylık | uyarı `composite_percent_mismatch`, kalem toplamı **yine 50.000** |
| 2.17 | Pasif daireye tahakkuk kesilmez | bir daire `is_active = false`, Aidat 240.000 | `unit_count = 23`, kalem toplamı **20.000** (23 daireye) |

## 3. Tahakkuk kaydı ve tahsilat (entegrasyon)

Kurgu: tek site, tek daire, tek kişi, bir **oturan** hesabı; kesinleşmiş işletme projesinde tek
kalem 12.000/yıl aylık `equal` → aylık **1.000**. Dönem Haziran 2026.

| # | Senaryo | Adım | Beklenen |
|---|---|---|---|
| 3.1 | Tahakkuk kaydedilince borç defterde oluşur | önizle → kaydet | başarı, 1 borç, toplam **1.000**, hesap bakiyesi **1.000** |
| 3.2 | Aynı dönem ikinci kez kesilemez | 3.1'den sonra tekrar kaydet | ret, mesajda `zaten kesilmiş`; bakiye **1.000** kalır |
| 3.3 | Ters kayıt bakiyeyi geri alır | 3.1 → ters kaydet | başarı, bakiye **0** |
| 3.4 | Aynı koşu iki kez ters kaydedilemez | 3.3'ten sonra tekrar ters kaydet | ret, bakiye **0** kalır |
| 3.5 | Tahsilat borcu kapatır | 3.1 → 1.000 tahsilat | `applied = 1000`, `unapplied = 0`, kapatılan borç **1**, bakiye **0** |
| 3.6 | Kısmi ödeme kalan borcu bırakır | 3.1 → 400 tahsilat | bakiye **600** |
| 3.7 | Fazla ödeme avans olarak kalır | 3.1 → 1.500 tahsilat | `applied = 1000`, `unapplied = 500`, bakiye **−500** |
| 3.8 | İkinci tahsilat aynı borca tekrar mahsup edilmez | 3.1 → 1.000 → 200 tahsilat | bakiye **−200**; o borca yapılan **toplam mahsup 1.000** (1.200 değil) |
| 3.9 | Sıfır veya negatif tutar reddedilir | 0 ve −50 tahsilat | ikisi de ret |
| 3.10 | *(yeni — `04` §7.2)* Ters kayıttan sonra sıradaki dönem aynı ay | Eylül kaydet → 30 Eylül'de ters kaydet → önizle | sıradaki dönem **Eylül**, `charge_date = 2026-09-01` |
| 3.11 | *(yeni — `04` §6)* Gecikme tazminatı | 1.000 borç, vade 10 Haz, tolerans 5, `as_of = 30 Haz` | tazminat **25,00** |
| 3.12 | *(yeni — `04` §6)* Tolerans içinde tazminat yok | aynı borç, `as_of = 14 Haz` | **0** |

## 4. Gider ve kasa (entegrasyon)

Kurgu: bir site, bir gider kategorisi, **Banka** hesabı (açılış **10.000**, açılış hareketi ile),
**Kasa** hesabı (açılış 0). Tarih: bugün.

| # | Senaryo | Beklenen |
|---|---|---|
| 4.1 | Ödenen gider kasadan düşer — 2.500 gider, ödendi, Banka | Banka **7.500** |
| 4.2 | Ödenmeyen gider kasayı etkilemez — 2.500, ödenmedi | Banka **10.000** |
| 4.3 | Ödendi işaretlenirken hesap seçilmezse reddedilir | ret, mesajda `kasa` |
| 4.4 | Sonradan ödeme kasaya işlenir — 1.200 ödenmedi → Kasa'dan öde | Kasa **−1.200**, Banka **10.000** |
| 4.5 | Geri alınan gider kasaya döner, silinmez — 3.000 ödendi → geri al | Banka **10.000**; **2 gider kaydı**: orijinal `is_reversed = true`, düzeltme `amount = −3.000`, `reversal_of_id = orijinal`; gerçekleşen gider toplamı **0** |
| 4.6 | Aynı gider iki kez geri alınamaz — 500 ödendi → geri al → tekrar | ikinci ret; Banka **10.000** |
| 4.7 | Aktarım toplam bakiyeyi değiştirmez — Banka → Kasa 4.000 | Banka **6.000**, Kasa **4.000**, toplam **10.000** |
| 4.8 | Gider kaynaklı hareket kasa ekranından geri alınamaz | ret, mesajda `gider` |
| 4.9 | Elle hareket ters kayıtla geri alınır — 150 çıkış → geri al | önce **9.850**, sonra **10.000** |
| 4.10 | İleri tarihli gider reddedilir — bugün + 30 gün | ret |
| 4.11 | Ekstre yürüyen bakiyesi doğru — bugün−3: +1.000, bugün−2: −400 | kapanış **10.600**; ekstre en yeni üstte, **ilk satırın yürüyen bakiyesi = kapanış** |
| 4.12 | Başka sitenin kasası görünmez | başka siteye "Yabancı Hesap" ekle | bu sitenin özetinde **2 hesap**, "Yabancı Hesap" yok |
| 4.13 | Yanlış içerikli dosya reddedilir — `fatura.pdf` içinde `<script>` | ret; diskte **dosya kalmaz** |
| 4.14 | Geçerli PDF kaydedilir, disk adı kullanıcı adından bağımsız — `../../gizli fatura.pdf`, içerik `%PDF-1.4…` | başarı; `file_name = "gizli fatura.pdf"`; `storage_path` içinde `..` **yok**; dosya diskte var |

## 5. Excel içe aktarma doğrulayıcısı (saf)

Kurallar: `11-excel-aktarim.md`.

| # | Senaryo | Girdi | Beklenen |
|---|---|---|---|
| 5.1 | Telefon E.164'e çevrilir | `5321234567` · `05321234567` · `905321234567` · `+90 532 123 45 67` · `0532 123 45 67` · `532-123-45-67` | hepsi `+905321234567` |
| 5.2 | Geçersiz telefon uyarı verir ama satırı düşürmez | `2121234567` (sabit hat) · `53212345` · `abc` | "Telefon" sütununda **uyarı**, satır **yine aktarılır** |
| 5.3 | Metrekare kültürden bağımsız okunur | `104,50`→104.50 · `104.50`→104.50 · `104.5`→104.5 · `68`→68 · `1.234`→1234 · `1.234,56`→1234.56 · `1,234.56`→1234.56 · `1 234,5`→1234.5 | parantezdekiler |
| 5.4 | Sayı olmayan metrekare hata | `abc` | "Brüt m²" **hata** |
| 5.5 | Net brütten büyükse uyarır | brüt 80, net 90 | "Net m²" **uyarı**, satır aktarılır |
| 5.6 | Arsa payı tek başına girilirse hata | yalnız pay / yalnız payda | "Arsa Payı Payda" / "Arsa Payı Pay" **hata** |
| 5.7 | Arsa payı doğru | 45 / 1000 | 45, 1000 |
| 5.8 | Malik yoksa satır aktarılmaz | malik adı ve soyadı boş | "Malik…" **hata** |
| 5.9 | İsimde rakam hata | malik adı `Ayşe2` | "Malik Ad" **hata** |
| 5.10 | İsim biçimi düzeltilir | ad `aYşE`, soyad `yılmaz` | `Ayşe`, `YILMAZ` |
| 5.11 | Kiracı isteğe bağlı | kiracı alanları boş | hata yok |
| 5.12 | Aynı blokta aynı numara iki kez olamaz | satır 2 ve 3: A-5 | satır **3**'te "Daire No" **hata**; aktarılabilir **1** satır |
| 5.13 | Farklı bloklarda aynı numara sorun değil | A-5 ve B-5 | aktarılabilir **2** |
| 5.14 | Daire numarası boşsa satır atlanır | numara boş | "Daire No" **hata** |
| 5.15 | Kullanım tipi esnek okunur | `Konut`→residential · `ticari`→commercial · `Dükkan`→commercial · `DEPO`→storage · `otopark`→parking · boş→residential | |
| 5.16 | Tanınmayan kullanım uyarır, konut kabul eder | `villa` | residential + "Kullanım" **uyarı** |
| 5.17 | Boş satırlar atlanır | araya boş satır | yalnız dolu satır |
| 5.18 | Hatalı satır diğerlerini engellemez | 3 satır, 1'i hatalı | aktarılabilir **2**, **1** hata |

## 6. Kiracı (tenant) izolasyonu — entegrasyon, **zorunlu**

Kurgu: **A sitesi** (1 blok, 4 daire, "Aksu …" başlıklı duyurular) · **B sitesi** (1 blok, 3 daire).

| # | Senaryo | Beklenen |
|---|---|---|
| 6.1 | A kapsamında yalnız A verisi | 4 daire, 1 blok; tüm dairelerin `site_id = A`; tüm duyurular "Aksu" içeriyor |
| 6.2 | B kapsamında yalnız B verisi | 3 daire, hepsi `site_id = B` |
| 6.3 | Diğer sitenin kaydı **kimlikle bile** getirilemez | A kapsamında B'nin bir dairesini `id` ile iste → bulunamaz |
| 6.4 | İlişkili veri üzerinden de sızmaz | A dairelerinin bloğunu yükle → hepsi "A" |
| 6.5 | Başka sitenin `site_id`'siyle kayıt eklemek hata | A kapsamında `site_id = B` kayıt ekle → istisna ("sızıntı") |
| 6.6 | Site bağlamı açılmadan kayıt eklenemez | kapsam yokken kiracı tablosuna ekle → istisna ("site bağlamı") |
| 6.7 | Yeni kayda geçerli site otomatik damgalanır | A kapsamında `site_id` vermeden blok ekle → `site_id = A` |
| 6.8 | Başka sitenin kaydı güncellenemez | B'nin kaydını A kapsamında güncelle → istisna |
| 6.9 | Kapsam kapanınca önceki site geri gelir | A içinde B aç → 3 daire, kapat → yine 4 |
| 6.10 | "Tüm siteler" kapsamı her şeyi görür | 7 daire, 2 blok |
| 6.11 | *(RLS)* Uygulama filtresi atlansa bile | ham SQL `SELECT * FROM units` A bağlamında → yalnız A |

**Her yeni uç nokta için ayrıca:** "B sitesinin kullanıcısı bu uç noktadan A'nın verisini
alamaz — 404 döner" testi.

## 7. Mimari kurallar (statik testler)

| # | Kural |
|---|---|
| 7.1 | Her model ya `site_id` taşır ya da global beyaz listededir (`plans, organizations, sites, users, organization_memberships`) |
| 7.2 | Beyaz listedeki global tablolar gerçekten `site_id` taşımaz |
| 7.3 | **Para alanları `float` değildir** — modellerde para sütunları `Numeric`, şemalarda `Decimal` |
| 7.4 | `domain/` paketi `sqlalchemy`, `fastapi`, `datetime.now` içe aktarmaz |

## 8. Yetki (uç nokta testleri)

| # | Senaryo | Beklenen |
|---|---|---|
| 8.1 | Güvenlik görevlisi borçlular / gider / kasa / rapor uçlarını çağırır | **403** |
| 8.2 | Güvenlik görevlisi bir sakinin bakiyesini ister | **403** |
| 8.3 | Denetçi kasayı görür | 200 |
| 8.4 | Denetçi kasa hesabı açmaya, gider eklemeye çalışır | **403** |
| 8.5 | Kullanıcı erişimi olmayan bir sitenin ucunu çağırır | **404** (403 değil) |
| 8.6 | Platform yöneticisi olmayan `/platform/overview` çağırır | **404** |
| 8.7 | Platform yöneticisi bir sitenin borçlularını ister | **404** — site üyeliği yok |
| 8.8 | Modül kapalıyken modülün ucu çağrılır | **404** |
| 8.9 | 5 hatalı girişten sonra doğru parola | reddedilir (kilit, 15 dk) |

## Referans

`brhnnkaraa6/siteyonetimi/tests/`:
- §0–2: `SiteYonetimi.Domain.Tests/TestSite.cs`, `ChargeEngineTests.cs`, `MoneyDistributeTests.cs`
- §3: `SiteYonetimi.Integration.Tests/PostingAndPaymentTests.cs`
- §4: `SiteYonetimi.Integration.Tests/ExpenseAndCashTests.cs`
- §5: `SiteYonetimi.Domain.Tests/UnitImportValidatorTests.cs`
- §6: `SiteYonetimi.Integration.Tests/TenantLeakTests.cs`
- §7: `SiteYonetimi.Architecture.Tests/TenantIsolationRulesTests.cs`
