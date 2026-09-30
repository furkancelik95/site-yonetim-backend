# 04 — İş kuralları

**Bu dosya sistemin kalbidir.** Para ile ilgili her iş buradaki kurallara uyar. Bir kural
belirsizse ya da bir istek bu kuralla çelişiyorsa: dur, issue'ya yaz, sor.

Her bölümün sonunda **"Referans uygulamada durum"** notu var. Bazı kurallar .NET referansında
eksik ya da hatalı uygulanmış; o durumda **buradaki kural doğrudur**, referansı kopyalama.

İçindekiler:
1. Para · 2. Kuruş kaybı olmadan dağıtım · 3. İşletme projesi · 4. Tahakkuk motoru ·
5. Tahsilat ve mahsup · 6. Gecikme tazminatı · 7. Tahakkuk kaydı ve ters kayıt ·
8. Değişmezlik · 9. Gider · 10. Kasa ve banka · 11. Adres eki (slug) ·
12. Gelir-gider raporu · 13. Borçlu listesi · 14. Metin biçimi · 15. Türkçe harf tuzakları

---

## 1. Para

| Kural | Uygulama |
|---|---|
| Tip | `decimal.Decimal`. **Asla `float`.** `Decimal(0.1)` değil, `Decimal("0.1")` |
| Hassasiyet | 2 ondalık (kuruş). Veritabanında `NUMERIC(18,2)` |
| Yuvarlama | **Yarım yukarı (sıfırdan uzağa)** → `ROUND_HALF_UP`. Python varsayılanı `ROUND_HALF_EVEN`'dır, **yanlış sonuç verir** |
| Para birimi | TRY. Tek para birimi |
| JSON | **Metin olarak:** `"amount": "1234.56"`. JavaScript'te de sayı `float`'tır, metin gönderilmezse frontend'de kuruş kayar |
| Gösterim | tr-TR: `1.234,56 ₺`. Biçimlendirme **frontend'in işi**; backend her zaman `"1234.56"` gönderir |

```python
# domain/money.py
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN

CENT = Decimal("0.01")
ZERO = Decimal("0")

def round_money(value: Decimal) -> Decimal:
    """Kuruşa yuvarlar: yarım yukarı (sıfırdan uzağa). 2.345 → 2.35, -2.345 → -2.35"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)
```

Karşılaştırmada küçük tolerans kullanılır: "sıfırdan büyük" yerine `> Decimal("0.005")`.

## 2. Kuruş kaybı olmadan dağıtım

Bir tutarı ağırlıklara göre paylaştırırken **dağıtılan toplam her zaman kaynak tutara eşittir.**
Yöntem: *en büyük kalan* (largest remainder).

**Algoritma:**
1. Her payın ham değeri: `ham[i] = toplam × ağırlık[i] / Σağırlık`
2. Her payı **kuruşa kes** (aşağı değil, *sıfıra doğru* kes): `pay[i] = kes(ham[i])`
3. Eksik kalan kuruş: `kalan = (toplam − Σpay) × 100` → tam sayı
4. Kuruşları, **kesilen kısmı (mutlak değerce) en büyük olanlardan başlayarak** birer birer dağıt.
   Eşitlikte **ağırlığı büyük olan** önce. O da eşitse **listedeki sıra** korunur.
5. **Negatif tutar pozitifin aynasıdır:** `distribute(-x, w) == [-p for p in distribute(x, w)]`
   (karar: Furkan, 30.09.2026 — şimdilik; değişirse burası güncellenir).
6. Savunma: **negatif ağırlık** ve **kuruştan hassas tutar** (`100.005`) `ValueError`. Dağıtılacak
   tutar önce kuruşa yuvarlanmış olmalı; sessizce yuvarlamak "toplam = kaynak" garantisini bozar.

```python
def distribute(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """total'ı weights oranında paylaştırır; sum(sonuç) == total her zaman."""
    if not weights:
        return []
    if any(w < 0 for w in weights):
        raise ValueError("Ağırlıklar negatif olamaz.")
    weight_sum = sum(weights, ZERO)
    if weight_sum <= 0:
        raise ValueError("Ağırlıklar toplamı sıfır veya negatif olamaz.")
    if total != round_money(total):
        raise ValueError("Dağıtılacak tutar kuruşa yuvarlanmış olmalı (en fazla 2 ondalık).")

    raw = [total * w / weight_sum for w in weights]
    result = [r.quantize(CENT, rounding=ROUND_DOWN) for r in raw]   # ROUND_DOWN = sıfıra doğru

    remaining_cents = int(((total - sum(result, ZERO)) * 100)
                          .quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if remaining_cents == 0:
        return result

    # kesilen kısmı (mutlak) büyük olan önce, eşitse ağırlığı büyük olan;
    # sorted kararlıdır → sıra korunur. abs() sayesinde negatif tutar pozitifin aynası olur.
    order = sorted(range(len(weights)),
                   key=lambda i: (abs(raw[i] - result[i]), weights[i]),
                   reverse=True)
    step = CENT if remaining_cents > 0 else -CENT
    for k in range(abs(remaining_cents)):
        result[order[k % len(order)]] += step
    return result
```

Örnekler (hepsi test: `07-test-senaryolari.md` §1):

| Girdi | Sonuç |
|---|---|
| `distribute(100, [1,1,1])` | `[33.34, 33.33, 33.33]` |
| `distribute(0.01, [1,1,1])` | toplam `0.01` |
| `distribute(1234.56, [1])` | `[1234.56]` |
| `distribute(x, [0,0])` | `ValueError` |
| `distribute(x, [])` | `[]` |

Bu fonksiyon tahakkukta, bileşik kuralda ve bileşen tutarlarının bölünmesinde kullanılır.
**Başka yerde elle `toplam / adet` yazma.**

## 3. İşletme projesi (bütçe) — KMK m.37

İşletme projesi, yılın gider tahminidir ve aidatın kaynağıdır.

```
draft ──tebliğ──> notified ──7 gün itiraz süresi──> finalized
                                                        │
                                                 yenisi kesinleşince
                                                        ▼
                                                   superseded
```

| Kural | |
|---|---|
| Tahakkuk **yalnızca `finalized`** projeden kesilir | Kesinleşmiş proje yoksa tahakkuk ekranı "önce işletme projesi kesinleşmeli" der |
| Birden çok kesinleşmiş proje varsa | `fiscal_year` en büyük olan kullanılır |
| `notified` olunca | `objection_deadline = notified_on + 7 gün` |
| `finalized` proje **değiştirilemez** | İİK m.68 belgesidir. Değişiklik gerekiyorsa yeni proje hazırlanır, eskisi `superseded` olur |

**Kalemin dönem tutarı:**

| `frequency` | Dönem tutarı |
|---|---|
| `monthly` | `round_money(annual_amount / 12)` |
| `quarterly` | `round_money(annual_amount / 4)` |
| `yearly` | `annual_amount` |
| `one_time` | `annual_amount` |

> **Referans uygulamada durum:** aylık tutar her ay ayrı yuvarlanıyor. 1.000 TL/yıl → 83,33 × 12 =
> 999,96 — yılda 4 kuruş kayıp. **Doğrusu:** yıllık tutar 12 aya `distribute()` ile bölünür
> (`distribute(1000, [1]*12)` → ilk 4 ay 83,34, sonraki 8 ay 83,33), dönem sırasına göre ilgili
> pay alınır. Bu davranış değişikliği Burhan'ın onayına tabidir → `12-acik-kararlar.md` #K3.

## 4. Tahakkuk motoru

İşletme projesinin her kalemini alır, kapsamdaki bölümlere dağıtım kuralına göre paylaştırır ve
borcu doğru kişinin doğru cari hesabına yazar. **Saftır:** veritabanına dokunmaz, aynı girdi
her zaman aynı çıktıyı verir. Önce **önizleme** üretir; kaydetmek ayrı bir adımdır (§7).

### 4.1 Girdi
- `site_id`, `budget_plan` (kalemleriyle), `charge_date`, `due_date`
- Sitenin bölümleri (blok ve daire tipiyle), dağıtım kuralları (bileşen ve özel ağırlıklarıyla),
  **tüm** `unit_parties` (motor tarihe göre kendisi süzer), cari hesaplar, tahakkuk tipleri
- `by_meter_consumption` için: `(budget_item_id, unit_id) → tüketim`

### 4.2 Adımlar
1. **Pasif bölümler (`is_active = false`) tamamen dışarıda.**
2. Kalemleri `sort_order`'a göre sırayla işle. Her kalem için:
   1. **Kapsam** (§4.3). Kapsama bölüm girmezse → uyarı `empty_scope`, kalemi atla.
   2. Kalemin dağıtım kuralı bulunamazsa → uyarı `missing_weight_data`, kalemi atla.
   3. `tutar = round_money(dönem tutarı)` (§3)
   4. **Dağıt** (§4.4) → her bölüme bir pay.
   5. Payı sıfır olmayan her bölüm için bir **satır** üret (§4.6).
3. Her bölümün satırlarını **ödeyen kuralına göre grupla** (§4.5): aynı bölümün aidatı kiracıya,
   çatı onarımı malike gidebilir → o bölüm için iki ayrı borç.
4. Her grup için **ödeyeni bul** (§4.5). Bulunamazsa → uyarı `no_active_party`, o grup yazılmaz.
   **Koşu durmaz**, diğer bölümler devam eder.

### 4.3 Kapsam
| `scope_kind` | Kapsamdaki bölümler |
|---|---|
| `whole_site` | tüm aktif bölümler |
| `blocks` | `block_id ∈ scope_block_ids` |
| `unit_types` | `unit_type_id ∈ scope_unit_type_ids` (tipi olmayan bölüm girmez) |
| `usage` | `usage == scope_usage` |

Örnek: asansör bakımı yalnız asansörlü bloğa → `scope_kind = blocks`.

### 4.4 Dağıtım türleri
Her bölümün **ağırlığı**:

| `kind` | Ağırlık | Veri yoksa |
|---|---|---|
| `equal` | 1 | — |
| `by_land_share` | `numerator / denominator` | 0 |
| `by_area` | `gross_area` ya da `net_area` (`area_basis`'e göre) | 0 |
| `by_unit_type_weight` | `unit_type.weight` | 0 |
| `by_custom_weight` | `unit_weights` tablosundaki ağırlık | 0 |
| `by_meter_consumption` | o kalem ve bölüm için sayaç tüketimi | 0 |
| `fixed_per_unit` | **havuz paylaştırılmaz** — her bölüme `round_money(fixed_amount)` yazılır | — |
| `composite` | §4.4.1 | — |

Sonra `distribute(tutar, ağırlıklar)`.

**Eksik veri:**
- **Tüm** bölümlerin ağırlığı 0 → uyarı `missing_weight_data` ("… verisi hiçbir bağımsız
  bölümde yok; kalem dağıtılamadı"), kalem dağıtılmaz.
- **Bazı** bölümlerin ağırlığı 0 → uyarı `missing_weight_data`, eksik bölümlerin ilk 5'i adıyla
  listelenir ("…, A-3, A-7 …"). O bölümler bu kalemden pay almaz, tutar diğerlerine dağılır.
  **Koşu durmaz.**

#### 4.4.1 Bileşik kural (`composite`)
Örnek: merkezi ısıtma = %70 sayaç tüketimi + %30 metrekare.
1. Yüzdelerin toplamı 100 değilse → uyarı `composite_percent_mismatch` ("… yüzdeleri 95 ediyor,
   100 olmalı. Tutar mevcut yüzdelere oranlanarak dağıtıldı."). **Durma**, devam et.
2. Tutarı bileşenlere böl: `bileşen_tutarları = distribute(tutar, [yüzdeler])` — bu adım da kuruş kaybetmez.
3. Her bileşeni kendi türüyle bölümlere dağıt; bir bileşenin verisi hiçbir bölümde yoksa uyar, o bileşeni atla.
4. Bölüm başına bileşen paylarını topla.

### 4.5 Kim öder? — KMK m.22
Kalemin `charge_type.payer_rule`'u belirler. Tip bulunamazsa varsayılan `occupant`.

| `payer_rule` | Kime yazılır | Hesap türü |
|---|---|---|
| `occupant` (aidat, işletme gideri) | `charge_date`'te **aktif kiracı** varsa kiracıya; yoksa **malike** | `occupant` hesabı |
| `owner` (demirbaş, yatırım, çatı/cephe) | her durumda **malike** | `owner` hesabı |

"Aktif" = `unit_party.is_active_on(charge_date)`. Kiracı eylülde çıktıysa ekim tahakkuku
malikin oturan hesabına yazılır.
Hesap = `(unit_id, person_id, kind)` eşleşen `ledger_account`. Bulunamazsa → `no_active_party`.

### 4.6 Satır ve açıklama
Her satır kendi dayanağını taşır — sakin "bu tutar nasıl hesaplandı" diye sorduğunda cevap satırdadır:
`budget_item_id, description (kalem adı), amount, allocation_kind, weight, weight_total,
source_amount, explanation`.

`explanation` metinleri (tr-TR biçimli, §14):

| Tür | Metin |
|---|---|
| `equal` | `50.000,00 TL, 24 bağımsız bölüme eşit paylaştırıldı` |
| `by_land_share` | `Arsa payı 0,045 / 0,85 × 180.000,00 TL` |
| `by_area` | `Brüt 110,00 m² / 2.140,00 m² × 50.000,00 TL` (net ise `Net`) |
| `by_unit_type_weight` | `Daire tipi ağırlığı 1,3 / 27,6 × 8.000,00 TL` |
| `by_custom_weight` | `Özel ağırlık 2 / 30 × 5.000,00 TL` |
| `by_meter_consumption` | `Sayaç tüketimi 12,50 / 300,00 × 40.000,00 TL` |
| `fixed_per_unit` | `Bağımsız bölüm başına sabit tutar` |
| `composite` | `%70 sayaç tüketimi + %30 metrekare bileşimi` |

### 4.7 Önizleme çıktısı
- `charges[]`: bölüm, bölüm adı, cari hesap, ödeyen kişi ve adı, hesap türü, satırlar, tutar
- `warnings[]`: `{kind, message, unit_id?}` — `kind` ∈ `no_active_party`, `missing_ledger_account`,
  `missing_weight_data`, `empty_scope`, `composite_percent_mismatch`
- `total_amount`, `unit_count` (borç yazılan farklı bölüm sayısı)
- `item_totals[]`: kalem başına dağıtılan toplam — **her kalem kendi tutarına kuruşu kuruşuna eşit
  olmalı**, bu bir kontroldür. Eşit değilse hata var demektir.

## 5. Tahsilat ve mahsup

Bir tahsilat girildiğinde **tek transaction** içinde:
1. `payments` satırı (`status = confirmed`)
2. Açık borçlara dağıtım → `payment_allocations` satırları (§5.1)
3. Cari hesaba **tahsilatın tamamı** kadar alacak hareketi: `ledger_entries(credit = tutar, source = payment)`
4. Kasa/banka hesabı seçildiyse **kasaya giriş**: `cash_movements(inflow = tutar, source = payment, source_id = payment.id)`

3 ve 4 birlikte: borç kapanır **ve** para bir yere girer. Yalnız birini yazmak
"borcu kapandı ama para nerede" ya da "kasada para var ama kimin" sorusunu doğurur.

**Doğrulama:** tutar `> 0` (sıfır ve negatif reddedilir), cari hesap mevcut, kasa hesabı
seçildiyse mevcut ve aktif. `reference` boşsa hesabın `reference_code`'u yazılır.

### 5.1 Hangi borca sayılır — en eski borçtan (FIFO)
**Açık borçlar:** hesabın `debit > 0` olan her hareketi için
`açık_tutar = debit − (bu harekete daha önce yapılmış payment_allocations toplamı)`.
`açık_tutar > 0.005` olanlar dağıtıma girer.

> "Daha önce yapılmış mahsuplar düşülür" kuralı kritik: düşülmezse ikinci tahsilat, ilk
> tahsilatın kapattığı borcu **tekrar kapatır.** Test: `07` §3 "ikinci tahsilat aynı borca
> tekrar mahsup edilmez".

**Sıra:**
1. Vadesi en eski olan önce (`due_date`, yoksa `date`)
2. Aynı vadede **önce gecikme tazminatı**, sonra anapara (TBK m.100). Site ayarıyla tersine
   çevrilebilir (`principal_first`).
3. Aynı vade ve türde, önce oluşturulan önce

Her borca `min(kalan, açık_tutar)` yazılır, kalan azaltılır, kalan bitince durulur.

**Fazla ödeme = avans:** dağıtılamayan tutar hesapta alacak olarak kalır, bakiye negatife düşer.
Örnek: 1.000 TL borca 1.500 TL ödeme → `applied = 1000`, `unapplied = 500`, bakiye `-500`.

**Sonuç:** `applied`, `unapplied`, kapatılan borç sayısı. Hareket açıklaması (tr-TR):
`15.06.2026 tahsilat (havale/EFT) — 2 borç kaydına mahsup edildi` ya da
`… — 500,00 TL avans olarak kaldı`. Yöntem metinleri: `nakit`, `havale/EFT`, `kredi kartı`, `diğer`.

> **Referans uygulamada durum:** avans, **sonraki tahakkuka mahsup edilmiyor.** Bakiye doğru
> çıkıyor (defter toplamı), ama yeni borç için `payment_allocation` üretilmediğinden mahsup
> dökümünde yeni borç "açık" görünüyor. **Doğrusu:** tahakkuk kaydedilirken hesapta
> kullanılmamış avans varsa, avans önce yeni borca mahsup edilir (mahsup kaydı üretilir).
> → `12-acik-kararlar.md` #K4.

## 6. Gecikme tazminatı — KMK m.20

"Geciktiği günler için aylık yüzde beş hesabıyla."

```python
def late_fee(outstanding: Decimal, due_date: date, as_of: date, policy) -> Decimal:
    if not policy.is_enabled or outstanding <= 0:
        return ZERO
    late_days = (as_of - due_date).days - policy.grace_days
    if late_days <= 0:
        return ZERO
    daily_rate = policy.monthly_rate_percent / Decimal(100) / Decimal(30)
    fee = round_money(outstanding * daily_rate * late_days)
    return ZERO if fee < policy.minimum_amount else fee
```

Örnek: 1.000 TL, vade 10 Haziran, tolerans 5 gün, 30 Haziran'da → gecikme 15 gün →
`1000 × 0,05/30 × 15 = 25,00 TL`.

**Nasıl deftere yazılır:**
- Her gün için ayrı kayıt **üretilmez.**
- **Ay sonu işi:** gecikmiş her hesap için o aya ait **tek** bir `ledger_entries(debit, source = late_fee)`.
- **Ödeme anında:** tahsilattan önce, o güne kadar biriken tazminat hesaplanıp kaydedilir, sonra
  tahsilat dağıtılır (§5.1 — aynı vadede önce tazminat kapanır).
- Tazminatın kendisine tazminat işletilmez (yalnız anapara borçlarından hesaplanır).

> **Referans uygulamada durum:** hesap fonksiyonu yazılmış ve politika tablosu var, ama **hiçbir
> yerde çağrılmıyor** — deftere tek gecikme kaydı düşmüyor. Uygulayan iş (ay sonu + ödeme anı)
> **yazılmalı.** Oran ve yöntemin hukuki teyidi açık → `12-acik-kararlar.md` #K2.

## 7. Tahakkuk kaydı ve ters kayıt

### 7.1 Kaydetme
Önizlemeden kalıcı kayda, **tek transaction:**
- 1 `charge_runs` (`status = posted`, `posted_at`, `posted_by`)
- her önizleme borcu için 1 `charges` + satırları `charge_lines`
- her borç için 1 `ledger_entries(debit = tutar, date = charge_date, due_date, source = charge,
  source_id = charge.id)` — açıklama `09/2026 tahakkuku — Aidat` (tek kalemse kalem adı,
  çoksa `3 kalem`)

**Reddetme koşulları:**
- Aynı ay için ters kaydı yapılmamış `posted` koşu varsa: *"09/2026 dönemi için tahakkuk zaten
  kesilmiş (15.09.2026 14:30). Aynı dönem ikinci kez kesilemez; düzeltme gerekiyorsa önce ters
  kayıt alın."*
- Önizlemede hiç borç yoksa: *"Kesilecek tahakkuk yok."*
- Dönem `closed` ise reddet.

Dönem (`periods`) yoksa oluşturulur.

### 7.2 Sıradaki dönem
- Geçerli koşu = `status = posted` **ve** `reversal_of_run_id IS NULL`.
- Sıradaki dönem = son geçerli koşunun ayından bir sonraki ay. Hiç geçerli koşu yoksa **içinde
  bulunulan ay.**
- `charge_date` = o ayın **1'i**, `due_date` = `charge_date + 14 gün`.

> **Referans uygulamada durum — HATA, kopyalama:** sıradaki dönem "tarihi en büyük koşu + 1 ay"
> diye hesaplanıyor ve **ters kayıt koşuları da hesaba giriyor.** Ters kayıt koşusunun tarihi
> ters kaydın yapıldığı gün olduğu için, eylülü 30 Eylül'de ters kaydedince sistem sıradaki
> dönemi eylül yerine **30 Ekim** öneriyor. Yukarıdaki kural doğrudur.

### 7.3 Ters kayıt (iptal)
Kaydedilmiş koşu **düzenlenmez, silinmez.** Yanlışsa ters kaydı alınır:
- Yalnız `posted` koşu ters kaydedilebilir; değilse *"Yalnızca kaydedilmiş koşular ters kayıtla
  iptal edilebilir."*
- Zaten ters kaydedildiyse (`reversal_of_run_id = bu koşu` olan kayıt varsa) *"Bu koşu zaten ters
  kayıtla iptal edilmiş."*
- Yeni koşu: `reversal_of_run_id = orijinal`, `charge_date = bugün`, `due_date = orijinalin vadesi`.
- Orijinalin her borcu için **aynı tutarda alacak**: `ledger_entries(credit = tutar, source = charge,
  source_id = charge.id)`, açıklama `TERS KAYIT — 09/2026 tahakkuku iptali`.
- Orijinal koşunun durumu `reversed` olur. Orijinal satırlar **yerinde kalır**; denetçi ikisini de görür.
- Ters kayıttan sonra aynı dönem **yeniden kesilebilir.**

## 8. Değişmezlik — dört defter, tek kural

| Defter | Silinir mi | Düzenlenir mi | Düzeltme |
|---|---|---|---|
| Cari hesap (`ledger_entries`) | hayır | hayır | ters hareket |
| Tahakkuk (`charge_runs`, `charges`) | hayır | hayır | ters kayıt koşusu (§7.3) |
| Gider (`expenses`) | hayır | tutar/tarih hayır | eksi tutarlı düzeltme kaydı (§9.3) |
| Kasa (`cash_movements`) | hayır | hayır | giriş/çıkış yer değiştirmiş hareket (§10.4) |

Değişebilen tek şeyler durum alanlarıdır: `charge_runs.status → reversed`,
`expenses.is_reversed → true`, `expenses.paid_on` (bir kez, ödenince).
Uç noktalarda bu tablolar için `DELETE` yoktur.

## 9. Gider

Gider ile ödeme **bilinçli olarak ayrı:** fatura geldiğinde gider yazılır, parası çıktığında
ödendi işaretlenir. Birleştirilseydi ödenmemiş faturalar kasadan düşmüş görünürdü.

### 9.1 Kayıt
| Alan | Kural |
|---|---|
| description | 3–200 karakter, kırpılmış |
| amount | `> 0`, `≤ 100.000.000` |
| expense_category_id | mevcut olmalı |
| date | gelecekte olamaz (bugün + 1 güne kadar tolerans) |
| vendor, document_number, note | kırpılır; 100 / 50 / 500 karakterde kesilir |
| dönem | `date`'in ayı; yoksa açılır. **Kapalıysa reddet:** *"09/2026 dönemi kapalı, bu tarihe kayıt yazılamaz."* |

**"Ödendi" işaretlenerek kaydediliyorsa** ayrıca: kasa hesabı zorunlu (*"Ödendi işaretlenen
giderde kasa/banka hesabı seçilmeli."*), hesap aktif olmalı, `paid_on` gelecekte olamaz,
`paid_on ≥ date` (*"Ödeme tarihi belge tarihinden önce olamaz."*).
Aynı transaction içinde kasadan çıkış: `cash_movements(outflow = tutar, source = expense,
source_id = expense.id)`, açıklama `{açıklama} · {tedarikçi}`.

Belge (fatura görüntüsü) varsa önce dosya kaydedilir (`09-guvenlik-kvkk.md` §3), sonra gider.

### 9.2 Sonradan ödeme
Reddet: gider yoksa, geri alınmışsa (*"Bu gider geri alınmış."*), zaten ödenmişse
(*"Bu gider zaten ödenmiş görünüyor."*), `paid_on < date`, `paid_on` gelecekte, hesap yok/pasif.
Aynı transaction: `paid_on` ve `cash_account_id` doldurulur + kasadan çıkış hareketi.

### 9.3 Geri alma (ters kayıt)
- Reddet: zaten geri alınmışsa, kendisi bir düzeltme kaydıysa (*"Düzeltme kaydı geri alınamaz."*),
  gerekçe 3 karakterden kısaysa.
- Yeni gider: `amount = −orijinal`, `date = bugün`, `description = "DÜZELTME — {orijinal}"`,
  `note = gerekçe`, `reversal_of_id = orijinal`, kategori/dönem/tedarikçi/belge no orijinalden.
- Orijinal: `is_reversed = true`.
- **Orijinal ödenmişse** para kasaya geri girer: `cash_movements(inflow = orijinal tutar,
  source = expense, source_id = düzeltme.id)`, açıklama `Gider düzeltmesi: {açıklama}`.

### 9.4 Toplamlar
"Gerçekleşen gider" = `is_reversed = false` **ve** `reversal_of_id IS NULL` olan giderlerin
toplamı. İkisi de listede görünür (soluk), toplama girmez.
"Ödenmemiş" = bu kümeden `paid_on IS NULL` olanlar.

## 10. Kasa ve banka

### 10.1 Hesap açma
- Ad 2–60 karakter, sitede benzersiz (*"'Ziraat' adında bir hesap zaten var."*)
- IBAN: yalnız harf/rakam bırakılır, büyük harfe çevrilir; `TR` ile başlamalı, 16–34 karakter
  (*"IBAN geçersiz görünüyor (TR ile başlamalı)."*)
- `opening_balance ≠ 0` ise aynı anda açılış hareketi: `source = opening`, açıklama
  `Açılış bakiyesi`, pozitifse `inflow`, negatifse `outflow`.
- Yeni site açılınca iki hesap otomatik kurulur: **"Banka Hesabı"** (`bank`, sitenin IBAN'ı ile)
  ve **"Kasa"** (`cash`).

### 10.2 Bakiye
`SUM(inflow) − SUM(outflow)`. **Saklanmaz**, hareketten hesaplanır — büyük ölçekte özet tablo
ile hızlandırılır (`08-performans.md` §2). Eksi bakiye mümkün ve ekranda uyarı olarak gösterilir.
Toplam bakiye yalnız **aktif** hesapların toplamıdır.

### 10.3 Elle hareket ve aktarım
- **Elle hareket** (banka masrafı, faiz geliri): açıklama 3–200, tutar `> 0`, tarih gelecekte
  değil, hesap aktif. `source = manual`.
- **Aktarım:** gönderen ≠ alan, ikisi de aktif. **İki hareket** yazılır: gönderende `outflow`
  (`{not} → {alan}`), alanda `inflow` (`{not} ← {gönderen}`), birbirinin `source_id`'sini taşır,
  `source = transfer`. Toplam bakiye değişmez.

### 10.4 Hareket geri alma
- **Tahsilat ve gider kaynaklı hareketler kasadan geri alınamaz:** *"Bu hareket bir tahsilat/gider
  kaydından doğdu. Düzeltme o kayıt üzerinden yapılmalı, yoksa kasa düzelir ama cari hesap yanlış
  kalır."*
- Düzeltme kaydı geri alınamaz; aynı hareket iki kez geri alınamaz; gerekçe ≥ 3 karakter.
- Yeni hareket: `inflow` ile `outflow` **yer değiştirir**, `reversal_of_id = orijinal`,
  açıklama `DÜZELTME — {orijinal}`, `reference = gerekçe`, tarih bugün.

### 10.5 Ekstre
- Tarih aralığı verilirse, **başlangıçtan önceki** hareketlerin neti "devreden bakiye" olur.
- Aralıktaki hareketler eskiden yeniye sıralanır (`date`, sonra `created_at`), **yürüyen bakiye
  bu sırada, devreden bakiyeden başlayarak** hesaplanır.
- Ekranda **en yeni üstte** gösterilir; sayfalama bu sıra üzerinden yapılır — yürüyen bakiye
  sayfa değişince bozulmaz, çünkü tüm aralık üzerinden hesaplanmıştır.
- Ekstre ayrıca: devreden, kapanış, toplam giren, toplam çıkan, toplam hareket sayısı ve hangi
  hareketlerin geri alındığı (ekranda "geri alındı" etiketi).

## 11. Adres eki (slug)

Site adından URL eki üretilir: "Aksu Konakları" → `aksu-konaklari`.
1. Kırp. Türkçe harfleri **elle** eşle: `ç→c ğ→g ı→i I→i İ→i i→i ö→o ş→s ü→u` (büyükleri de).
2. Kalan harf/rakamı küçük harfe çevir; boşluk, `-`, `_`, `.` → `-`; diğer her şeyi at.
3. Ardışık `--` → `-`; baştaki/sondaki `-` sil; en fazla 60 karakter.
4. Sitede benzersiz olmalı; çakışırsa kullanıcıdan farklı bir ek istenir.

**`str.lower()` kullanma** — §15.

## 12. Gelir-gider raporu (yıllık)

- **Gelir = fiilen tahsil edilen para, kesilen tahakkuk değil.** "Ne kadar tahakkuk ettik" ile
  "kasaya ne girdi" farklı sorulardır. Gelir = o yıl `confirmed` tahsilatlar + `manual`
  kaynaklı kasa girişleri (faiz, kira geliri).
- **Gider** = o yılın gerçekleşen giderleri (§9.4), belge tarihine göre.
- Ay ay: gelir, gider, fark (12 ay; veri olmayan aylar da sıfırla gelir).
- Kategori dökümü: kategori, tutar, kayıt sayısı, pay (%).
- **İşletme projesi karşılaştırması** (o yılın projesi varsa): kategori başına bütçelenen
  (kalemlerin `annual_amount` toplamı) × gerçekleşen, fark, kullanım %, `is_over` (gerçekleşen >
  bütçe). **Bütçede olmayan ama harcama yapılmış kategoriler de** bütçe 0 ile listelenir.
- Ayrıca: bugünkü toplam kasa/banka bakiyesi, seçilebilir yıllar (gider/tahsilat olan yıllar +
  bu yıl).

## 13. Borçlu listesi

- Bakiye = `SUM(debit) − SUM(credit)`; **bakiye > 0,005** olan hesaplar listelenir, büyükten küçüğe.
- **En eski açık vade:** borçlar (debit > 0) vadeye göre sıralanır; hesabın **toplam alacağı**
  bu sırayla borçlardan düşülür; tamamen kapanamayan ilk borcun vadesi "en eski açık vade"dir.
- **Gecikme günü** = `max(0, bugün − en eski açık vade)`.
- Pano/borçlu ekranı: toplam açık bakiye, 30 günü aşan, 60 günü aşan, ortalama borç.
- Satırda: bölüm adı, kişi adı, hesap türü (malik/oturan), referans kodu, en eski vade, gecikme, bakiye.

## 14. Metin biçimi

Backend'in ürettiği ve kullanıcıya gösterilen metinler (hareket açıklaması, tahakkuk
açıklaması, uyarı mesajı) **tr-TR** biçimindedir:
- Para: `1.234,56` (binlik nokta, ondalık virgül) + ` TL`
- Tarih: `15.06.2026`; dönem: `06/2026`
- Arsa payı gibi oranlar: gereksiz sıfır yok, `0,045`

API alanlarındaki **değerler** ise her zaman makine biçimindedir: para `"1234.56"`, tarih
`"2026-06-15"`. Biçimlendirme frontend'in işidir; backend yalnız hazır cümle ürettiğinde tr-TR kullanır.

## 15. Türkçe harf tuzakları (Python)

Python'un metin işlemleri **Türkçeyi bilmez:**

| İfade | Python sonucu | Doğrusu |
|---|---|---|
| `"i".upper()` | `"I"` | `"İ"` |
| `"ı".upper()` | `"I"` | `"I"` ✓ |
| `"I".lower()` | `"i"` | `"ı"` |
| `"İ".lower()` | `"i̇"` (i + birleşik nokta, **2 karakter**) | `"i"` |
| `"işçi".title()` | `"Işçi"` | `"İşçi"` |

Kişi adı biçimlendirmede (ad: baş harf büyük, soyad: tamamı büyük) önce Türkçe eşleme uygula:

```python
_TR_UPPER = str.maketrans({"i": "İ", "ı": "I"})
_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})

def tr_upper(s: str) -> str: return s.translate(_TR_UPPER).upper()
def tr_lower(s: str) -> str: return s.translate(_TR_LOWER).lower()
def tr_title(s: str) -> str:
    return " ".join(tr_upper(w[:1]) + tr_lower(w[1:]) for w in s.split(" "))
# tr_title("aYşE") → "Ayşe"   tr_upper("yılmaz işçi") → "YILMAZ İŞÇİ"
```

Arama ve karşılaştırmada (`"İstanbul" == "istanbul"`?) ikisini de `tr_lower` ile normalize et.
PostgreSQL'de Türkçe sıralama için `tr-TR-x-icu` collation kullanılabilir.

## Referans

`brhnnkaraa6/siteyonetimi`:
- §1–2: `src/SiteYonetimi.Domain/Common/Money.cs`
- §3: `src/SiteYonetimi.Domain/Finance/BudgetItem.cs` (`AmountForFrequency`)
- §4: `src/SiteYonetimi.Application/Charging/ChargeEngine.cs`, `ChargeEngineModels.cs`
- §5: `src/SiteYonetimi.Application/Charging/PaymentAllocator.cs`,
  `src/SiteYonetimi.Infrastructure/Persistence/PaymentRepository.cs`
- §6: `src/SiteYonetimi.Application/Charging/LateFeeCalculator.cs`
- §7: `src/SiteYonetimi.Application/Charging/ChargePostingService.cs`,
  `src/SiteYonetimi.Infrastructure/Persistence/ChargeRunRepository.cs`
- §9: `src/SiteYonetimi.Infrastructure/Persistence/ExpenseRepository.cs`
- §10: `src/SiteYonetimi.Infrastructure/Persistence/CashRepository.cs`,
  `src/SiteYonetimi.Infrastructure/Queries/FinanceQueries.cs`
- §11: `src/SiteYonetimi.Infrastructure/Provisioning/SiteProvisioningService.cs` (`Slugify`)
- §12–13: `src/SiteYonetimi.Infrastructure/Queries/FinanceQueries.cs`, `SiteQueries.cs`
