# 03 — Veri modeli

Tablo ve sütun adları `snake_case`, İngilizce. Kullanıcıya görünen etiketler Türkçe (frontend'de).

## Genel kurallar

| Konu | Kural |
|---|---|
| Birincil anahtar | `id UUID` — **zaman sıralı UUIDv7** önerilir (indeks yerelliği için). Python 3.14+ `uuid.uuid7()`, öncesi `uuid_utils` paketi |
| Zaman damgası | Her tabloda `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `updated_at TIMESTAMPTZ NULL` |
| Kiracı sütunu | **Kiracı tablolarında** `site_id UUID NOT NULL` + indeks. Aşağıda `[K]` ile işaretli |
| Global tablolar | `[G]` ile işaretli — `site_id` yok: `plans`, `organizations`, `sites`, `users`, `organization_memberships` |
| Para | `NUMERIC(18,2)`. Python'da `Decimal`. **Asla `float`/`REAL`/`DOUBLE`** |
| Alan (m²) | `NUMERIC(10,2)` |
| Ağırlık / oran | `NUMERIC(12,4)` (daire tipi ağırlığı, özel ağırlık, tüketim) |
| Yüzde | `NUMERIC(5,2)` |
| Tarih (vade, belge tarihi) | `DATE` — saat bilgisi yok |
| Olay anı (giriş, gönderim) | `TIMESTAMPTZ`, UTC |
| Enum | `TEXT` + `CHECK` kısıtı (okunabilir, göçü kolay). Değerler aşağıda |
| Silme | Finansal tablolarda **fiziksel silme yok**. Diğerlerinde de tercih edilen: `is_active` / `ended_on` |

---

## 1. Platform ve kiracı

### `plans` [G]
| Sütun | Tip | Not |
|---|---|---|
| name | TEXT | Başlangıç / Standart / Pro / Yönetim Şirketi |
| max_units | INT NULL | `NULL` = sınırsız |
| max_storage_mb | INT NULL | |
| allowed_modules | TEXT[] | modül anahtarları, `01-urun.md` |
| sort_order | INT | |

### `organizations` [G] — yönetim şirketi
| Sütun | Tip | Not |
|---|---|---|
| name | TEXT | 3–150 karakter |
| tax_number | TEXT NULL | VKN |
| plan_id | UUID NULL → plans | |

### `sites` [G] — **kiracı (tenant)**
| Sütun | Tip | Not |
|---|---|---|
| name | TEXT | |
| name_key | TEXT **UNIQUE** | adın Türkçe kurallarla küçük harfli, tek boşluklu hali — ad benzersizliği (`04` §15; PostgreSQL `lower()` Türkçeyi bilmez) |
| slug | TEXT **UNIQUE** | URL'de kullanılır: `/sites/{slug}/...`. Üretimi: `04-is-kurallari.md` §11 |
| address, city, district | TEXT NULL | |
| property_kind | TEXT | `residential` · `mixed` · `office` · `shopping_center` |
| organization_id | UUID NULL → organizations | `NULL` = bağımsız site |
| plan_id | UUID NULL → plans | |
| fiscal_year_start_month | INT | varsayılan 1 |
| iban, bank_name | TEXT NULL | |

### `site_modules` [K]
| Sütun | Tip | Not |
|---|---|---|
| module_key | TEXT | `(site_id, module_key)` UNIQUE |
| enabled | BOOL | |
| settings | JSONB NULL | |

---

## 2. Kimlik ve üyelik

### `users` [G]
| Sütun | Tip | Not |
|---|---|---|
| email | TEXT **UNIQUE** | küçük harfe çevrilmiş |
| password_hash | TEXT | argon2/bcrypt |
| full_name | TEXT | |
| kind | TEXT | `staff` · `resident` |
| is_active | BOOL | pasif kullanıcı giriş yapamaz ve hiçbir siteye erişemez |
| is_platform_admin | BOOL | platform paneli erişimi — `05-yetki.md` §5 |
| last_login_at | TIMESTAMPTZ NULL | |
| failed_login_count, locked_until | INT, TIMESTAMPTZ NULL | 5 hatalı denemede 15 dk kilit |

### `organization_memberships` [G]
| Sütun | Tip | Not |
|---|---|---|
| organization_id | UUID → organizations | |
| user_id | UUID → users | |
| role | TEXT | `Sahip` · `Yönetici` · `Muhasebe` · `İzleyici` — `05-yetki.md` §3 |
| is_active | BOOL | |

### `site_memberships` [K]
| Sütun | Tip | Not |
|---|---|---|
| user_id | UUID → users | |
| role | TEXT | site rol şablonu adı — `05-yetki.md` §2 |
| person_id | UUID NULL → persons | **Sakin** üyeliğinde dolu: kullanıcının hangi kişi olduğu. FK `persons` tablosuyla (Dilim 3) eklenir |
| is_active | BOOL | pasif açık üyelik erişim vermez ve o sitedeki türetilmiş erişimi de kapatır (`05` §4) |

### `auth_sessions` [G] — oturum (yenileme jetonu)
| Sütun | Tip | Not |
|---|---|---|
| user_id | UUID → users | |
| token_hash | TEXT UNIQUE | yenileme jetonunun **SHA-256** hash'i; jetonun kendisi saklanmaz |
| previous_token_hash | TEXT NULL UNIQUE | bir önceki jeton — tekrar gelirse çalınma belirtisi, oturum iptal |
| expires_at | TIMESTAMPTZ | kayan pencere: her yenilemede `now + SESSION_HOURS` |
| last_used_at | TIMESTAMPTZ | |
| revoked_at | TIMESTAMPTZ NULL | çıkış / iptal |

---

## 3. Yapı

### `blocks` [K]
| Sütun | Tip | Not |
|---|---|---|
| name | TEXT | "A", "B", "Kule 1" |
| has_elevator | BOOL | asansör bakımı gibi kalemler yalnız asansörlü bloğa yazılabilir |
| floor_count | INT NULL | |
| sort_order | INT | |

### `unit_types` [K]
| Sütun | Tip | Not |
|---|---|---|
| name | TEXT | "1+1", "2+1", "3+1" |
| weight | NUMERIC(12,4) | varsayılan 1. "Daire tipi ağırlığı" dağıtımında kullanılır |
| sort_order | INT | |

### `units` [K] — bağımsız bölüm
| Sütun | Tip | Not |
|---|---|---|
| block_id | UUID → blocks | |
| number | TEXT | "12", "A-12", "Z03". `(site_id, block_id, number)` UNIQUE |
| floor | INT NULL | -5 … 100 |
| unit_type_id | UUID NULL → unit_types | |
| gross_area | NUMERIC(10,2) NULL | brüt m² |
| net_area | NUMERIC(10,2) NULL | net m² |
| land_share_numerator | INT NULL | arsa payı — pay |
| land_share_denominator | INT NULL | arsa payı — payda. Pay ile birlikte ya ikisi dolu ya ikisi boş |
| usage | TEXT | `residential` · `commercial` · `storage` · `parking` |
| is_active | BOOL | **pasif bölüme tahakkuk kesilmez** |
| commercial_title | TEXT NULL | dükkan ise ticari unvan |

Hesaplanan:
- `display_name` = `"{block.name}-{number}"` (blok yoksa yalnız `number`). Örnek: `A-12`.
- `land_share` = `numerator / denominator` (ikisi de doluysa ve payda ≠ 0), yoksa `None`.

---

## 4. Kişiler

### `persons` [K]
| Sütun | Tip | Not |
|---|---|---|
| first_name | TEXT | 2–40, rakam yok. Kayıtta baş harf büyük: "Ayşe" |
| last_name | TEXT | 2–40, rakam yok. Kayıtta tamamı büyük: "YILMAZ" |
| phone | TEXT NULL | **E.164**: `+905321234567` |
| email | TEXT NULL | küçük harf, ≤ 254 |
| national_id_encrypted | TEXT NULL | TC kimlik — **şifreli** saklanır (`09-guvenlik-kvkk.md`) |
| user_id | UUID NULL → users | kişi sisteme giriş yapıyorsa |

Aynı gerçek kişi iki farklı sitede iki ayrı `persons` kaydıdır (kiracı sınırı).
Hesaplanan: `full_name` = `"{first_name} {last_name}"`.

### `unit_parties` [K] — bölüm ↔ kişi ilişkisi, **tarih aralıklı**
| Sütun | Tip | Not |
|---|---|---|
| unit_id | UUID → units | |
| person_id | UUID → persons | |
| role | TEXT | `owner` (malik) · `tenant` (kiracı) · `resident` (oturan) · `proxy` (vekil) |
| share_percent | NUMERIC(5,2) | hisseli mülkiyet; varsayılan 100 |
| start_date | DATE | |
| end_date | DATE NULL | `NULL` = hâlâ devam ediyor |

Hesaplanan: `is_active_on(date)` = `start_date <= date AND (end_date IS NULL OR end_date >= date)`.

**Kiracı değişimi kayıt silerek yapılmaz:** eski kiracının `end_date`'i doldurulur, yeni kiracı
yeni satırla eklenir. Tahakkuk motoru hangi tarihte kimin oturduğuna bu tablodan bakar.

---

## 5. Cari hesap defteri

### `ledger_accounts` [K] — kim borçlu
| Sütun | Tip | Not |
|---|---|---|
| unit_id | UUID → units | |
| person_id | UUID → persons | |
| kind | TEXT | `owner` (malik hesabı) · `occupant` (oturan hesabı) |
| reference_code | TEXT | `(site_id, reference_code)` UNIQUE. Havale açıklamasında kullanılır |
| is_closed | BOOL | |

`(unit_id, person_id, kind)` UNIQUE önerilir.

**Hangi hesaplar açılır** (daire kaydedilince):
| Durum | Açılan hesaplar | Referans kodu |
|---|---|---|
| Malik | malik hesabı (`owner`) | `{BLOK}{NO}-M` |
| Kiracı varsa | kiracıya oturan hesabı (`occupant`) | `{BLOK}{NO}-K` |
| Kiracı yoksa | **malike** oturan hesabı da açılır | `{BLOK}{NO}-O` |

Kod: blok adı + numara, yalnız harf/rakam, büyük harf. Çakışırsa sonuna sayı eklenir: `A12-M`, `A12-M2`, `A12-M3`.
Kiracı yoksa malike oturan hesabı açılmasının sebebi: aidatın yazılacağı bir hesap her zaman bulunsun.

### `ledger_entries` [K] — borç/alacak hareketi, **değişmez**
| Sütun | Tip | Not |
|---|---|---|
| account_id | UUID → ledger_accounts | |
| date | DATE | hareket tarihi |
| due_date | DATE NULL | borçlarda vade |
| debit | NUMERIC(18,2) | borç (tahakkuk, gecikme) |
| credit | NUMERIC(18,2) | alacak (tahsilat, ters kayıt) |
| source | TEXT | `charge` · `payment` · `late_fee` · `adjustment` · `transfer` · `advance` |
| source_id | UUID NULL | kaynak kayıt (charge.id, payment.id …) |
| description | TEXT | kullanıcıya gösterilen açıklama, tr-TR biçimli |
| reversal_of_entry_id | UUID NULL | ters kayıtsa orijinal hareket |

Bir satırda `debit` ile `credit`'ten **yalnızca biri** sıfırdan büyüktür.
Hesap bakiyesi = `SUM(debit) - SUM(credit)`. Pozitif = borçlu, negatif = alacaklı (avans).
İndeks: `(site_id, account_id, date)`.

---

## 6. Finans — kurallar ve bütçe

### `periods` [K] — mali dönem
| Sütun | Tip | Not |
|---|---|---|
| year, month | INT | `(site_id, year, month)` UNIQUE |
| status | TEXT | `open` · `closed`. **Kapalı döneme kayıt yazılamaz** |

Hesaplanan: `name` = `"MM/YYYY"`, `first_day`, `last_day`.

### `expense_categories` [K]
| Sütun | Tip | Not |
|---|---|---|
| name | TEXT | "İşletme Giderleri", "Demirbaş ve Yatırım" |
| kind | TEXT | `operating` (oturan öder) · `capital_improvement` (malik öder) |
| sort_order | INT | |

### `charge_types` [K] — tahakkuk tipi
| Sütun | Tip | Not |
|---|---|---|
| name | TEXT | "Aidat", "Demirbaş Katılım Payı", "Isıtma Payı" |
| payer_rule | TEXT | **`occupant`** (oturan öder) · **`owner`** (malik öder) — KMK m.22 |
| legal_basis | TEXT NULL | "KMK m.20 — işletme gideri, oturan öder" |
| is_advance | BOOL | |
| sort_order | INT | |

### `allocation_rules` [K] — dağıtım kuralı
| Sütun | Tip | Not |
|---|---|---|
| name | TEXT | |
| kind | TEXT | `equal` · `by_land_share` · `by_area` · `by_unit_type_weight` · `by_custom_weight` · `fixed_per_unit` · `by_meter_consumption` · `composite` |
| fixed_amount | NUMERIC(18,2) NULL | yalnız `fixed_per_unit` |
| area_basis | TEXT | `gross` · `net` (yalnız `by_area`) |

### `allocation_components` [K] — bileşik kuralın parçaları
| Sütun | Tip | Not |
|---|---|---|
| allocation_rule_id | UUID → allocation_rules | |
| kind | TEXT | bileşik dışındaki türlerden biri |
| area_basis | TEXT | |
| percent | NUMERIC(5,2) | bileşenlerin toplamı 100 olmalı |
| sort_order | INT | |

Örnek: merkezi ısıtma = %70 `by_meter_consumption` + %30 `by_area`.

### `unit_weights` [K] — `by_custom_weight` için daire başına ağırlık
| Sütun | Tip | Not |
|---|---|---|
| allocation_rule_id | UUID | |
| unit_id | UUID | |
| weight | NUMERIC(12,4) | |

### `budget_plans` [K] — işletme projesi (KMK m.37)
| Sütun | Tip | Not |
|---|---|---|
| fiscal_year | INT | |
| name | TEXT | |
| status | TEXT | `draft` · `notified` (tebliğ edildi) · `finalized` (kesinleşti) · `superseded` |
| notified_on | DATE NULL | |
| objection_deadline | DATE NULL | tebliğ + 7 gün |
| finalized_on | DATE NULL | |

Hesaplanan: `total_annual_amount` = kalemlerin `annual_amount` toplamı.

### `budget_items` [K] — işletme projesi kalemi
| Sütun | Tip | Not |
|---|---|---|
| budget_plan_id | UUID → budget_plans | |
| name | TEXT | "Kapıcı maaşı", "Asansör bakımı" |
| expense_category_id | UUID → expense_categories | |
| charge_type_id | UUID → charge_types | **kimin ödeyeceğini bu belirler** |
| allocation_rule_id | UUID → allocation_rules | |
| annual_amount | NUMERIC(18,2) | yıllık toplam |
| frequency | TEXT | `monthly` · `quarterly` · `yearly` · `one_time` |
| scope_kind | TEXT | `whole_site` · `blocks` · `unit_types` · `usage` |
| scope_block_ids | UUID[] NULL | `scope_kind = blocks` ise |
| scope_unit_type_ids | UUID[] NULL | `scope_kind = unit_types` ise |
| scope_usage | TEXT NULL | `scope_kind = usage` ise |
| sort_order | INT | |

Referans uygulamada `scope_*_ids` virgüllü metin olarak saklanıyordu; PostgreSQL'de dizi kullan.

### `late_fee_policies` [K] — gecikme tazminatı (KMK m.20), site başına bir kayıt
| Sütun | Tip | Varsayılan |
|---|---|---|
| monthly_rate_percent | NUMERIC(5,2) | 5 |
| grace_days | INT | 5 |
| minimum_amount | NUMERIC(18,2) | 0 |
| is_enabled | BOOL | true |

---

## 7. Finans — hareketler

### `charge_runs` [K] — bir tahakkuk koşusu (bir dönemin aidat kesimi)
| Sütun | Tip | Not |
|---|---|---|
| period_id | UUID → periods | |
| budget_plan_id | UUID → budget_plans | |
| status | TEXT | `draft` · `posted` · `reversed` |
| charge_date | DATE | |
| due_date | DATE | |
| posted_at | TIMESTAMPTZ NULL | |
| posted_by | TEXT NULL | işlemi yapanın adı |
| reversal_of_run_id | UUID NULL | ters kayıt koşusuysa orijinali |

**Bir dönem için en fazla bir geçerli koşu:** aynı `period_id` için ters kaydı yapılmamış ikinci
`posted` koşu olamaz. Kısmi benzersiz indeksle zorla.

### `charges` [K] — bir bölümün o koşudaki borcu
| Sütun | Tip | Not |
|---|---|---|
| charge_run_id | UUID → charge_runs | |
| unit_id | UUID → units | |
| ledger_account_id | UUID → ledger_accounts | borcun yazıldığı hesap |
| amount | NUMERIC(18,2) | satırların toplamı |

Aynı bölüm aynı koşuda **iki** `charge` alabilir: oturan kalemleri kiracının hesabına, malik
kalemleri malikin hesabına.

### `charge_lines` [K] — borcun kalem dökümü ("bu tutar nasıl hesaplandı")
| Sütun | Tip | Not |
|---|---|---|
| charge_id | UUID → charges | |
| budget_item_id | UUID → budget_items | |
| description | TEXT | kalem adı |
| amount | NUMERIC(18,2) | |
| allocation_kind | TEXT | |
| weight | NUMERIC NULL | bu bölümün ağırlığı (ör. 110 m²) |
| weight_total | NUMERIC NULL | kapsamdaki toplam ağırlık (ör. 2.140 m²) |
| source_amount | NUMERIC(18,2) NULL | dağıtılan havuz |
| explanation | TEXT NULL | "Brüt 110,00 m² / 2.140,00 m² × 50.000,00 TL" |

### `payments` [K] — tahsilat
| Sütun | Tip | Not |
|---|---|---|
| ledger_account_id | UUID → ledger_accounts | |
| amount | NUMERIC(18,2) | > 0 |
| date | DATE | |
| method | TEXT | `cash` · `bank_transfer` · `credit_card` · `other` |
| cash_account_id | UUID NULL → cash_accounts | **paranın girdiği hesap** |
| reference | TEXT NULL | boşsa hesabın `reference_code`'u |
| note | TEXT NULL | |
| status | TEXT | `pending` (sakin bildirdi) · `confirmed` · `cancelled` |

### `payment_allocations` [K] — tahsilatın hangi borca sayıldığı
| Sütun | Tip | Not |
|---|---|---|
| payment_id | UUID → payments | |
| ledger_entry_id | UUID → ledger_entries | kapatılan borç hareketi |
| amount | NUMERIC(18,2) | |

### `expenses` [K] — gider
| Sütun | Tip | Not |
|---|---|---|
| expense_category_id | UUID | |
| period_id | UUID NULL | belge tarihinin dönemi; yoksa açılır |
| description | TEXT | 3–200. **Sakinler bu metni görür** |
| amount | NUMERIC(18,2) | > 0 (ters kayıtta negatif) |
| date | DATE | belge tarihi. Gelecekte olamaz |
| vendor | TEXT NULL | tedarikçi (≤ 100) |
| document_number | TEXT NULL | fatura no (≤ 50) |
| note | TEXT NULL | (≤ 500) |
| stored_file_id | UUID NULL → stored_files | fatura görüntüsü |
| paid_on | DATE NULL | **`NULL` = kaydedildi ama parası çıkmadı** |
| cash_account_id | UUID NULL | parasının çıktığı hesap |
| reversal_of_id | UUID NULL | düzeltme kaydıysa orijinal gider |
| is_reversed | BOOL | ters kaydı atılmış gider — toplamlara girmez |
| created_by_name | TEXT NULL | |

Hesaplanan: `is_paid` = `paid_on IS NOT NULL`, `is_reversal` = `reversal_of_id IS NOT NULL`.
İndeks: `(site_id, date)`.

### `cash_accounts` [K] — kasa / banka hesabı ("para nerede")
| Sütun | Tip | Not |
|---|---|---|
| name | TEXT | 2–60, sitede benzersiz |
| kind | TEXT | `cash` (nakit) · `bank` |
| bank_name, iban | TEXT NULL | IBAN boşluksuz, büyük harf, `TR` ile başlar, 16–34 karakter |
| opening_balance | NUMERIC(18,2) | sisteme geçişteki bakiye |
| opening_date | DATE NULL | |
| is_active | BOOL | kapalı hesaba hareket yazılamaz |
| sort_order | INT | |
| note | TEXT NULL | |

**Cari hesap ile karıştırma:** `ledger_accounts` "kim ne kadar borçlu" sorusunun, `cash_accounts`
"para nerede" sorusunun cevabıdır. Bir tahsilat ikisine birden yazılır.

### `cash_movements` [K] — kasa/banka hareketi, **değişmez**
| Sütun | Tip | Not |
|---|---|---|
| cash_account_id | UUID → cash_accounts | |
| date | DATE | |
| inflow | NUMERIC(18,2) | giren. `inflow` ile `outflow`'dan yalnız biri > 0 |
| outflow | NUMERIC(18,2) | çıkan |
| description | TEXT | |
| reference | TEXT NULL | |
| source | TEXT | `manual` · `payment` · `expense` · `transfer` · `opening` |
| source_id | UUID NULL | kaynak kayıt; aktarımda karşı hareket |
| reversal_of_id | UUID NULL | düzeltme hareketiyse orijinali |
| created_by_name | TEXT NULL | |

Hesap bakiyesi = `SUM(inflow) - SUM(outflow)`. İndeks: `(site_id, cash_account_id, date)`.
Açılış bakiyesi de bir harekettir (`source = opening`) — ekstrede nereden geldiği görünsün.

---

## 8. Dosyalar

### `stored_files` [K]
| Sütun | Tip | Not |
|---|---|---|
| file_name | TEXT | kullanıcının yüklediği ad, temizlenmiş (≤ 120) |
| content_type | TEXT | beyaz listeden: `application/pdf`, `image/jpeg`, `image/png`, `image/webp` |
| byte_size | BIGINT | ≤ 10 MB |
| storage_path | TEXT | `{site_id}/{file_id}.{ext}` — kullanıcının verdiği ad **kullanılmaz** |
| sha256 | TEXT NULL | |
| uploaded_by_name | TEXT NULL | |

Kurallar: `09-guvenlik-kvkk.md` §3.

---

## 9. Operasyon

### `requests` [K] — talep / arıza / şikâyet
| Sütun | Tip | Not |
|---|---|---|
| number | INT | site içinde artan: 1, 2, 3 … |
| title | TEXT | |
| description | TEXT NULL | |
| category | TEXT | `other` · `plumbing` · `electrical` · `elevator` · `heating` · `cleaning` · `security` · `garden` · `common_area` |
| priority | TEXT | `low` · `normal` · `high` · `urgent` |
| status | TEXT | `open` · `in_progress` · `waiting` · `resolved` · `closed` · `cancelled` |
| reported_by_person_id | UUID NULL | |
| unit_id | UUID NULL | ortak alan talebinde boş |
| location | TEXT NULL | "B blok otopark girişi" |
| assigned_to | TEXT NULL | |
| due_at | TIMESTAMPTZ NULL | |
| resolved_at | TIMESTAMPTZ NULL | |
| resolution | TEXT NULL | **`resolved`/`closed` yapılırken zorunlu** |

### `request_events` [K] — talebin geçmişi (değişmez)
| Sütun | Tip | Not |
|---|---|---|
| request_id | UUID | |
| kind | TEXT | `created` · `status_changed` · `assigned` · `comment` · `resolved` |
| description | TEXT | |
| actor_name | TEXT NULL | |
| new_status | TEXT NULL | |

### `announcements` [K]
| Sütun | Tip | Not |
|---|---|---|
| title, body | TEXT | |
| importance | TEXT | `normal` · `important` · `critical` |
| audience | TEXT | `all_residents` · `blocks` · `owners_only` · `tenants_only` · `debtors_only` |
| audience_block_ids | UUID[] NULL | |
| published_at | TIMESTAMPTZ NULL | `NULL` = taslak |
| expires_on | DATE NULL | |
| published_by | TEXT NULL | |
| is_pinned | BOOL | |

### `announcement_deliveries` [K] — kime, hangi kanaldan ulaştı
| Sütun | Tip | Not |
|---|---|---|
| announcement_id | UUID | |
| person_id | UUID | |
| channel | TEXT | `in_app` · `web_push` · `email` · `sms` |
| sent_at, read_at | TIMESTAMPTZ NULL | |
| failure_reason | TEXT NULL | |

### `packages` [K] — kargo
| Sütun | Tip | Not |
|---|---|---|
| unit_id | UUID | |
| person_id | UUID NULL | alıcı |
| carrier | TEXT NULL | kargo firması |
| pickup_code | TEXT | 4 haneli teslim kodu (1000–9999) |
| status | TEXT | `waiting` · `delivered` · `returned` |
| received_at | TIMESTAMPTZ | |
| received_by | TEXT NULL | kaydı açan görevli |
| delivered_at, delivered_by, delivered_to | … NULL | |
| note | TEXT NULL | |
| notification_sent | BOOL | |
| notification_channel | TEXT NULL | |

### `visitors` [K] — ziyaretçi
| Sütun | Tip | Not |
|---|---|---|
| unit_id | UUID | |
| host_person_id | UUID NULL | ev sahibi |
| full_name | TEXT | |
| phone | TEXT NULL | |
| plate_number | TEXT NULL | |
| kind | TEXT | `guest` · `cargo` · `service` · `contractor` |
| qr_token | TEXT NULL | ön davet için |
| expected_on | DATE NULL | |
| status | TEXT | `expected` · `entered` · `exited` · `denied` |
| entered_at, exited_at | TIMESTAMPTZ NULL | |
| recorded_by | TEXT NULL | |
| note | TEXT NULL | |

---

## 10. Eksik ama gerekecek

Referans uygulamada yok, PRD'de MVP işaretli. Tasarlanırken bunlara yer bırak:

| Tablo | Neden |
|---|---|
| `audit_log` | Her kritik değişiklik: kim, ne zaman, ne, eski/yeni değer. Finansal işlemler dahil |
| `account_balances` | Cari hesap başına özet bakiye — `08-performans.md` §2 |
| `cash_balances` | Kasa hesabı başına özet bakiye |
| `consents` | KVKK rıza kayıtları, sürümlü |
| `idempotency_keys` | `(key, user_id)` → önceki yanıt |

## Referans

`brhnnkaraa6/siteyonetimi` → `src/SiteYonetimi.Domain/` (her varlık bir dosya)
