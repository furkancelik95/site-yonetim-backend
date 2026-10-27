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
Kullanıcı ↔ site bağı. **Rol burada değil** (P18, göç 0041): roller `site_role_assignments`'ta.
| Sütun | Tip | Not |
|---|---|---|
| user_id | UUID → users | `(site_id, user_id)` benzersiz |
| person_id | UUID NULL → persons | **Sakin** üyeliğinde dolu: kullanıcının hangi kişi olduğu. FK `persons` tablosuyla (Dilim 3) eklenir |
| is_active | BOOL | pasif açık üyelik erişim vermez ve o sitedeki türetilmiş erişimi de kapatır (`05` §4) |

### `site_roles` [K] — siteye özel rol ya da şablon düzeyi (P18, K20)
| Sütun | Tip | Not |
|---|---|---|
| key | TEXT | `(site_id, key)` benzersiz. Şablon düzeyi satırında şablon anahtarı (`security`), özel rolde `custom-…` |
| name | TEXT | `(site_id, name)` benzersiz; onay kurallarında rolü tanıtır, **değişmez** |
| description | TEXT NULL | |
| base_key | TEXT NULL | özel rolün kopyalandığı rol |
| is_system | BOOL | true: hazır şablonun bu sitedeki düzeyi (izinler koddan); false: özel rol |
| permissions | TEXT[] NULL | özel rolün izinleri (şablonda NULL). Tanınmayan izin adı yok sayılır |
| level | SMALLINT | 1–9, 1 en üst (rol sırası) |
| is_active | BOOL | özel rol arşivi (silinmez) |
| created_by_name | TEXT NULL | |

### `site_role_assignments` [K] — rol ataması (PLT-09, P18)
Kişi × rol × kapsam × (başlangıç–bitiş). Silinmez; kaldırma `ends_at = now` + `removed_*` (atama
geçmişi). Etkin: `starts_at ≤ now < ends_at` (boş = sınırsız).
| Sütun | Tip | Not |
|---|---|---|
| user_id | UUID → users | |
| role_key | TEXT | şablon anahtarı ya da özel rol `key`'i |
| scope_kind | TEXT | `site` · `block` · `gate` (kapı: GUV-08 gelince) |
| block_id | UUID NULL → blocks | bileşik FK; yalnız `block` |
| gate_id | UUID NULL | yalnız `gate`; kapı tablosu gelince FK |
| starts_at · ends_at | TIMESTAMPTZ NULL | `ends_at ≥ starts_at` |
| assigned_by_name · removed_at · removed_by_name | | atama geçmişi |

### `auth_sessions` [G] — oturum (yenileme jetonu)
| Sütun | Tip | Not |
|---|---|---|
| user_id | UUID → users | |
| token_hash | TEXT UNIQUE | yenileme jetonunun **SHA-256** hash'i; jetonun kendisi saklanmaz |
| previous_token_hash | TEXT NULL UNIQUE | bir önceki jeton — tekrar gelirse çalınma belirtisi, oturum iptal |
| expires_at | TIMESTAMPTZ | kayan pencere: her yenilemede `now + SESSION_HOURS` |
| last_used_at | TIMESTAMPTZ | |
| revoked_at | TIMESTAMPTZ NULL | çıkış / iptal |
| ip, user_agent | TEXT NULL | oturum listesi (cihaz adı); yenilemede son adres yazılır |

### `auth_events` [G] — kimlik olayı, **değişmez**
`user_id` (bilinmeyen e-postada boş — e-posta yazılmaz), `kind` (`login_succeeded` ·
`login_failed` · `account_locked` · `logout` · `password_changed` · `session_revoked` ·
`token_reused`), `session_id`, `ip`, `user_agent`, `request_id`, `created_at`. Tetikleyici
(`forbid_history_change`) güncellemeyi ve silmeyi engeller; saklama süresi K8.

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
| search_name | TEXT, indeks | Türkçe kurallarla küçük harf "ad soyad" — arama için (PostgreSQL `lower()` Türkçeyi bilmez) |

TC kimlik (`national_id_encrypted`) şifreleme anahtarı yönetimi (`09` §5) gelince eklenecek; şimdilik tutulmuyor.

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
| source | TEXT | `charge` · `payment` · `late_fee` · `adjustment` · `transfer` · `advance` · `opening` (devir bakiye — hesap başına bir, kısmi benzersiz indeks) · `refund` (iade — borç tarafında, **açık borç sayılmaz**) |
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

### `charge_schedules` [K] — otomatik aylık tahakkuk ayarı (site başına bir)
| Sütun | Tip | Not |
|---|---|---|
| enabled | BOOL | |
| charge_day | INT | 1–28 (her ayda bulunsun) |
| due_days | INT | **kullanılmaz** (K25, göç 0028): vade finans parametresi `due_day` |
| draft_lead_days | INT | 0–15 (varsayılan 5): "Tahakkuk" onay kuralı varsa taslak kesimden bu kadar gün önce (FIN-05) |
| notify_on_run | BOOL | bildirim sağlayıcısı yok (`12` K5) — şimdilik yalnız kayıt |
| enabled_on | DATE NULL | açıldığı gün: daha önceki kesim günü geriye dönük kesilmez |
| enabled_by_id, enabled_by | UUID NULL → users, TEXT | ayarı açan: gece işinin taslağı onaya onun adına gider |

### `charge_schedule_runs` [K] — otomatik tahakkukun ay başına sonucu
| Sütun | Tip | Not |
|---|---|---|
| year, month | INT | `(site_id, year, month)` benzersiz — iş iki kez çalışsa da tek sonuç |
| status | TEXT | `posted` · `drafted` (taslak onaya gönderildi) · `skipped` (dönem kesilmiş, proje yok, önizlemede uyarı) · `failed` (ertesi gün yeniden denenir) |
| message | TEXT NULL | atlama/hata nedeni |
| charge_run_id | UUID NULL → charge_runs | kesildiyse |
| ran_at | TIMESTAMPTZ | |

### `charge_runs` [K] — bir tahakkuk koşusu (bir dönemin aidat kesimi)
| Sütun | Tip | Not |
|---|---|---|
| period_id | UUID → periods | |
| source | TEXT | `budget_plan` · `billing_rule` (K23, göç 0028) |
| budget_plan_id | UUID NULL → budget_plans | işletme projesi kaynağında zorunlu |
| status | TEXT | `draft` · `approval_pending` · `approved` · `rejected` · `cancelled` · `posted` · `reversed` (FIN-05) |
| approval_request_id | UUID NULL → approval_requests | "Tahakkuk" onayı |
| created_by | TEXT NULL | taslağı hazırlayan |
| charge_date | DATE | |
| due_date | DATE | |
| posted_at | TIMESTAMPTZ NULL | |
| posted_by | TEXT NULL | işlemi yapanın adı |
| reversal_of_run_id | UUID NULL | ters kayıt koşusuysa orijinali |

**Bir dönem için en fazla bir geçerli koşu:** aynı `period_id` için ters kaydı yapılmamış ikinci
`posted` koşu olamaz. Kısmi benzersiz indeksle zorla. Aynı şekilde dönem başına en fazla bir
açık taslak (`draft`/`approval_pending`/`approved`).

### `charge_run_drafts` [K] — taslağın önizleme anlık görüntüsü (göç 0028)
`charge_run_id` (benzersiz), `snapshot` JSONB (borçlar, satırlar, uyarılar, kalem toplamları).
Kesinleşme bu görüntüden yapılır. Denetim kaydına girmez (büyük); koşu denetlenir.

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
| budget_item_id | UUID NULL → budget_items | işletme projesi kalemi YA DA… |
| billing_rule_version_id | UUID NULL → billing_rule_versions | …aidat kuralı sürümü (tam biri dolu, göç 0028) |
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

### `clearance_certificates` [K] — borçsuzluk belgesi (**değişmez**, tetikleyici)
| Sütun | Tip | Not |
|---|---|---|
| ledger_account_id | UUID → ledger_accounts | |
| year, sequence | INT | `(site_id, year, sequence)` benzersiz; site + yıl bazında boşluksuz |
| number | TEXT | `BB-{yıl}-{5 hane}` |
| reference_code, account_kind, unit_name, person_name | TEXT | **belge anındaki** görüntü |
| balance | NUMERIC(18,2) | belge anında defterden; ≤ 0,005 (borçluya belge yok) |
| as_of, valid_until | DATE | `valid_until` = `as_of` + 30 gün |
| issued_by_user_id, issued_by_name | UUID NULL, TEXT | |

### `refunds` [K] — iade (**değişmez**, tetikleyici)
| Sütun | Tip | Not |
|---|---|---|
| ledger_account_id | UUID → ledger_accounts | alacaklı hesap |
| cash_account_id | UUID → cash_accounts | paranın çıktığı hesap |
| amount | NUMERIC(18,2) | > 0, ≤ hesabın alacağı |
| date | DATE | gelecekte olamaz |
| reason | TEXT | zorunlu |
| created_by_name | TEXT NULL | |

Aynı transaction'da cari defterde borç (`source = refund`) ve kasada çıkış (`source = refund`).

### `bank_imports` [K] — banka ekstresi önizlemesi
| Sütun | Tip | Not |
|---|---|---|
| cash_account_id | UUID → cash_accounts | banka türü |
| file_name, uploaded_by_user_id | TEXT, UUID | yalnız yükleyen onaylar |
| status | TEXT | `pending` · `confirmed` |
| rows | JSONB | okunan satırlar + eşleşme; **onayda silinir** (kişisel veri). Dosya diske yazılmaz |
| expires_at | TIMESTAMPTZ | 6 saat; dolunca `purge-imports` siler |

### `bank_import_lines` [K] — aktarılmış banka hareketi (**değişmez**)
| Sütun | Tip | Not |
|---|---|---|
| cash_account_id, fingerprint | UUID, TEXT | `(site, hesap, parmak izi)` benzersiz — aynı hareket iki kez aktarılmaz |
| payment_id, bank_import_id | UUID | |

### `recurring_expenses` [K] — tekrarlanan gider tanımı
| Sütun | Tip | Not |
|---|---|---|
| description, expense_category_id, amount, vendor | | gider alanları |
| day_of_month | INT | 1–28 |
| auto_pay, cash_account_id | BOOL, UUID NULL | otomatik ödeme hesap ister |
| is_active, active_since | BOOL, DATE | etkin olduğu günden önce geriye dönük yazılmaz |
| removed_at | TIMESTAMPTZ NULL | "kaldır" arşive alır; silinmez |

`recurring_expense_runs`: `(recurring_expense_id, year, month)` benzersiz, `status` `created` · `skipped` · `failed`, `expense_id`.

### `registrations` [K] — sakin kayıt başvurusu (kişisel veri, saklama K8)
`number` (site içinde artan, `KB-0001`), ad (`Ayşe`), soyad (`YILMAZ`), `phone` (E.164), `email?`,
`unit_text`, `relation` (`owner` · `tenant`), `kvkk_ack_at`, `explicit_consent_at?`, `ip`,
`status` (`pending` · `approved` · `rejected`), karar alanları, onayda `unit_id`, `person_id`.
Aynı telefonla tek bekleyen başvuru (kısmi benzersiz indeks). Kayıt bağlantısı
`registration_links(code benzersiz, is_enabled)`, site başına bir. `rate_limits` [G]: herkese
açık uçların IP + uç anahtarlı sayaçları.

### `incidents`, `lost_items` [K] — güvenlik olay kaydı ve kayıp eşya
Site içinde artan `number`; silinmez, içerik değişmez. Olay: `kind`, `location`, `description`,
`occurred_at`, `unit_id?`, `status` (`open` · `closed`) + kapanış notu. Kayıp eşya: `description`,
`location`, `found_by`, `found_at`, `status` (`waiting` · `returned` · `disposed`), `returned_to`.

### `request_departments` [K] — talep departmanı
`name`, `name_key` (Türkçe harf duyarsız, site içinde benzersiz), `is_active`. `requests.department_id`
(bileşik FK). Silinmez; pasif departmana yeni talep atanmaz.

### Yönetim paketi [K] — toplantı, anket, sözleşme, demirbaş/stok, personel (istekler 14–18)
- `meetings` (site içinde artan `number`, `kind`, `title`, `scheduled_at`, `location`, `status`
  `planned` · `held` · `cancelled`, `attendance_note`, `held_at`, `cancel_reason`) ve
  `meeting_agenda_items` (`order` toplantı içinde tekil, `result`, `decision`, `votes_*` ≥ 0).
  Kararlar tek seferde girilir, sonra değişmez.
- `polls` (`question` = başlık, `audience`, `block_ids?`, `ends_on`, erken kapanışta `closed_at`,
  K31: `is_anonymous`, `one_per_unit`), `poll_questions` (`order` anket içinde tekil, `kind`
  `single` · `multiple` · `rating` · `text`), `poll_options` (`question_id`, `order` soru içinde
  tekil), `poll_ballots` (yanıt: `unit_id?`, `person_id?` — yalnız isimli ankette; kısmi tekil
  indeks `(poll_id, unit_id)` ve kişi başına ankette `(poll_id, person_id)`), `poll_answers`
  (seçim / puan 1–5 / metin), `poll_participants` (anonim + kişi başına ankette yalnız katılım).
  Göç 0036 eski `poll_votes` satırlarını pusula + yanıta taşıdı. Yanıt tabloları denetim kaydına
  yazılmaz.
- `contracts` (`end_date ≥ start_date`, `amount?` NUMERIC(18,2), `notice_days` 0–365,
  `is_archived`). `days_left`/`state` saklanmaz, okurken hesaplanır.
- `assets` (`sequence` site içinde tekil → `code` `DB-0001`, `status` `in_use` · `broken` ·
  `retired`, `value?`). `stock_items` (`name_key` tekil, `quantity` NUMERIC(14,3) ≥ 0 — hareketle
  aynı işlemde satır kilidiyle güncellenir, `min_quantity`). `stock_moves` (`direction`,
  `quantity > 0`) **değişmez** (`forbid_history_change` tetikleyicisi).
- `staff_members` (`full_name`, `position`, `employer` `site` · `contractor` — taşeronda
  `contractor_name` zorunlu, `phone?`, `start_date`, `end_date?`, `shift?`). Veri minimizasyonu:
  T.C. kimlik, maaş, adres, sağlık yok; saklama süresi K8. + `payroll_no?` (site içinde tekil,
  kısmi benzersiz dizin), `user_id?` → `users` (K29, site içinde tekil), `access_closed_at?`.

### İnsan kaynakları [K] — vardiya, izin, puantaj (istekler 36–39, göç 0029)
- `shift_templates` (`name` tekil, `start_time`/`end_time` TIME — bitiş ≤ başlangıç ertesi gün,
  `break_minutes` 0–180, `position?`, `location?`, `is_active`). Silinmez.
- `shift_assignments` (`staff_id`, `template_id`, `work_date`; `(staff, gün, şablon)` tekil;
  `changed_after_publish`). `shift_weeks` (`week_start` pazartesi, yayın anı/eden,
  `removed_after_publish`).
- `leave_requests` (`leave_type`, `date_from`–`date_to`, `days`, `leave_note` — raporluda boş
  (kısıt), `status` `pending`·`approved`·`rejected`·`cancelled`, `source`, `approval_request_id?`).
  Denetim kaydında `leave_type`/`leave_note` maskeli (sağlık verisi olabilir).
- `timesheet_fixes` (atama başına bir düzeltme, gerekçeli), `timesheet_days` (kesinleşen gün) ve
  `timesheet_lines` (donmuş satır) — son ikisi **değişmez**. `timesheet_exports` (`month` tekil,
  `rows` JSONB) **değişmez**.
- `attendance_settings` (site başına bir: `method`, `qr_code`, konum NUMERIC(9,6), `radius_m`,
  toleranslar) ve `attendance_events` (giriş/çıkış, cihaz ve sunucu saati, `verified`,
  `distance_m`; konum saklanmaz) **değişmez**.

Hiçbiri silinmez. Hepsi (oy tablosu hariç) denetim kaydında.

### Bildirim [K/G] — bildirim merkezi (Yazılım Dokümanı PLT-05)
- `notifications` [K]: `user_id`, `category`, `kind`, `title`, `body`, `link_entity`, `link_id`,
  `dedupe_key`, `read_at`. `(site_id, user_id, dedupe_key)` benzersiz — aynı olay aynı kişiye bir kez.
- `notification_preferences` [G]: `user_id × category × channel → enabled`; yalnız varsayılandan
  farklı hücre saklanır (`domain/notifications.py`).
- `push_tokens` [G]: `token` benzersiz, `platform` (`ios` · `android` · `web`), `last_seen_at`.

### Onay mekanizması [K] (Yazılım Dokümanı Bölüm 6)
- `approval_rules`: `action_type`, `is_active`, `is_template`, `current_version`.
- `approval_rule_versions` **değişmez**: `amount_min` (hariç) / `amount_max` (dahil), `source`,
  `mode`, `reject_behavior`, `remind_after_days`, `escalate_after_days`, `day_basis`,
  `escalation_role`, `effective_from`, `steps` JSONB, `basis_note`.
- `approval_requests`: `number` (site içinde artan), `action_type`, `subject_type/id`, `title`,
  `summary`/`payload` JSONB, `amount`, `requested_by`, `rule_version_id` (boş = varsayılan),
  `snapshot` (kuralın donmuş kopyası + adım başına M/N), `status`, `revision`, `current_step`,
  `step_started_at`, `reminded_at`, `escalated_at`, `decided_at`, `failure_reason`.
- `approval_assignments`: adım açılırken çözülen onaycılar (`source`: rule · escalation).
- `approval_decisions` **değişmez**: kararı veren, `slot_user_id` (kimin hakkı), vekâleten asıl
  onaycı adı, `decision`, `comment`; `(talep, sürüm, karar veren)` ve `(talep, sürüm, hak)` tekil.
- `approval_delegations`: devreden/devralan, `roles[]`, `starts_at`–`ends_at`, `ended_at`.

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
| source | TEXT | `manual` · `payment` · `expense` · `transfer` · `opening` · `refund` |
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

### `documents` [K] — belge arşivi (RAP-03; servis isteği 78)
| Sütun | Tip | Not |
|---|---|---|
| title · title_key | TEXT | ad (3–120) · sürüm eşleşmesi anahtarı (Türkçe küçük harf, boşluk tekleştirilmiş) |
| kind | TEXT | `plan` · `rules` · `insurance` · `fire_report` · `elevator` · `contract` · `minutes` · `financial` · `other` |
| scope · block_id · unit_id | TEXT · UUID NULL | `site` (ikisi boş) · `block` · `unit`; bileşik FK |
| confidentiality | TEXT | `public` · `unit` (yalnız `scope = unit`) · `management` · `secret` |
| valid_until | DATE NULL | geçerlilik bitişi (60/30 gün uyarı) |
| retention_years | INT NULL | saklama süresi (K8), 1–100 — bilgi amaçlı, otomatik silme yok |
| subject_type · subject_id | TEXT · UUID NULL | başka modülün kaydına bağ (ikisi birlikte) |
| current_version | INT | son sürüm no |

Benzersiz: `(site_id, title_key, scope, block_id, unit_id)` NULLS NOT DISTINCT — aynı ad + kapsam
tek belge, yeni yükleme yeni sürüm. Silinmez.

### `document_versions` [K] — **değişmez**
`document_id`, `version` (belgede tekil), `stored_file_id` (tekil; ad, tür, boyut, SHA-256
oradan), `uploaded_by_id`, `uploaded_by_name`.

### `document_access_logs` [K] — gizli belge açılışı, **değişmez**
`document_id`, `version`, `user_id`, `user_name`, `created_at`.

---

## 9. Operasyon

### `requests` [K] — talep / arıza / şikâyet
| Sütun | Tip | Not |
|---|---|---|
| number | INT | site içinde artan: 1, 2, 3 … |
| title | TEXT | |
| description | TEXT NULL | |
| category | TEXT | site kategorisinin anahtarı — `(site_id, category)` → `request_categories(site_id, key)` (D-26) |
| priority | TEXT | `low` · `normal` · `high` · `urgent` |
| status | TEXT | `open` · `assigned` · `in_progress` · `waiting` · `resolved` · `closed` · `cancelled` (geçiş tablosu K18) |
| reported_by_person_id | UUID NULL | |
| unit_id | UUID NULL | ortak alan talebinde boş |
| location | TEXT NULL | "B blok otopark girişi" |
| assigned_to | TEXT NULL | |
| due_at | TIMESTAMPTZ NULL | |
| resolved_at | TIMESTAMPTZ NULL | |
| resolution | TEXT NULL | **`resolved`/`closed` yapılırken zorunlu**; iptalde gerekçe |
| created_by_name | TEXT NULL | talebi açan kullanıcının adı (Python backend) |
| closed_at | TIMESTAMPTZ NULL | kapandı/iptal anı |
| auto_close_at | TIMESTAMPTZ NULL | çözüldü talep sakin yanıtsızsa bu anda kapanır (`auto_close_days`) |
| sla_hour_type, sla_target_hours, sla_calendar | TEXT/INT/JSONB NULL | SLA kural anlık görüntüsü (açılıştaki kategori + takvim); `due_at` bitiş |
| sla_warn_at, sla_warned_at, sla_breached_at | TIMESTAMPTZ NULL | %80 anı; uyarı ve aşım bildirimi yazıldı mı (iş tekrarlamaz) |
| outage_id | UUID NULL | ana olay (`outages`) |

`(site_id, number)` UNIQUE; numara site başına işlem kilidi altında `max + 1`. İndeks `(site_id, status)`.

### `request_events` [K] — talebin geçmişi (değişmez)
| Sütun | Tip | Not |
|---|---|---|
| request_id | UUID | |
| kind | TEXT | `created` · `status_changed` · `assigned` · `comment` · `resolved` · `department_changed` · `closed` · `cancelled` · `reopened` · `feedback` · `outage_linked` · `outage_unlinked` · `quote` |
| description | TEXT | |
| actor_name | TEXT NULL | |
| new_status | TEXT NULL | |

UPDATE/DELETE tetikleyiciyle reddedilir.

### Hizmetler (HIZ-01..05, göç 0031)
- `request_categories` [K] — site tanımlı kategori: `key` (değişmez, site içinde benzersiz), `name`
  (+`name_key` benzersiz), `hour_type` (`24x7` · `business`), `hours_low/normal/high/urgent`
  (1–2160), `default_priority`, `department_id?` (atanacak ekip), `is_active`, `sort_order`.
- `request_settings` [K] — tek satır: `calendar` JSONB (7 gün), `auto_close_days` (1–30, vars. 3),
  `merge_window_hours` (1–168, vars. 24), `low_rating_threshold` (1–4, vars. 2).
- `reply_templates` [K] — `title`, `category?` (→ kategori anahtarı), `body`, `is_active`.
- `request_internal_notes` [K] — iç not (K26) **değişmez**: `request_id`, `body`, `author_user_id`, `author_name`.
- `request_feedback` [K] — sakin değerlendirmesi **değişmez**: `request_id`, `solved`, `score`
  (çözüldüyse 1–5, değilse NULL), `comment`, `person_id`, `by_name`. Son satır geçerli.
- `outages` [K] — ana olay: `title`, `category`, `location`, `public_description`, `status`
  (`open` · `resolved`), `resolution`, `resolved_at`. Bağlı talepler `requests.outage_id`.
- `service_tariffs` [K] — `name` (+`name_key` benzersiz), `unit`, `price` > 0, `is_active`.
- `service_quotes` [K] — `request_id`, `unit_id`, `account_id` (teklif anında belirlenir),
  `total`, `status` (`pending` · `accepted` · `rejected` · `extra_pending` · `billed`),
  `actual_total`, `billed_total`, `ledger_entry_id` (kaynak `service`). Talepte tek açık teklif
  (kısmi benzersiz indeks). `service_quote_lines`: `line_no`, `description`, `qty`, `unit_price`, `total`.
- `permit_settings` [K] — tek satır: `weekday_hours`, `saturday_hours?`, `sunday_allowed`,
  `deposit_amount`, `checklist[]`.
- `permits` [K] — `unit_id`, `person_id`, `type` (`renovation` · `move_in` · `move_out`),
  `date_from/to`, `contractor`, `description`, `status` (`pending` · `approved` · `rejected` ·
  `completed`), `reject_reason`, `checklist` JSONB, `deposit`, `deposit_entry_id` (kaynak
  `deposit`), `refund_entry_id` (ters kayıt), `approval_request_id`.

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

## 9a. Yapı, kişiler ve kayıt (servis istekleri 19–26, göç 0026)

| Tablo | Not |
|---|---|
| `common_areas` [K] | ortak alan: ad (sitede tekil), tür, blok (boşsa site geneli), rezerve edilebilir, etkin. Silinmez |
| `unit_attribute_changes` [K] | yürürlük tarihli m² / arsa payı / tip değişikliği: `effective_from`, `changes {alan: {from, to}}`, `applied`. Silinmez; uygulanmış satır değişmez (tetikleyici `guard_unit_attribute_change`) |
| `structure_settings` [K] | site başına bir satır: araç ayarı, kayıt yöntemleri, KVKK yayında bayrağı, açılış mutabakatı işareti |
| `import_batches` [K] | aktarım partisi (`units`/`vehicles`/`opening_balances`): `previewed → imported → rolled_back`, geri alma için oluşan kayıt kimlikleri (`created` JSONB), 7 gün |
| `household_members` [K] | hane üyesi: bölüm, telefon, yetkiler, `invited → active → ended`; bölümde aynı telefonla tek açık üye |
| `vehicles` [K] | plaka (normalize), tür (kalıcı/misafir), durum (`active/pending/inactive`), sahip kişi; bölümde aynı plaka tek açık kayıt |
| `invitations` [K] | davet: telefon, bölüm, ilişki (`owner/tenant/household`), durum; belirtecin **yalnız SHA-256 özeti** (`token_hash`, global tekil) |
| `custom_fields` · `custom_field_values` [K] | özel alan tanımı (anahtar ve tip değişmez, arşivlenir) ve bölüm/kişi başına JSON değer |
| `persons.merged_into_id`, `merged_at` | kişi birleştirme izi; birleşen kişi listelenmez |
| `registrations.requested_unit_id` | herkese açık formda listeden seçilen bölüm |
| `sites.lifecycle_status`, `went_live_at` | `setup` / `live` (YAP-01); mevcut siteler `live` |

### Güvenlik, devriye, otopark (göç 0030 — servis istekleri 40–46, K30) [K]

| Tablo | Özet |
|---|---|
| `gates` | kapı: ad (`name_key` tekil), tip, bağlı bloklar (`uuid[]`), 7/24, etkin |
| `security_settings` | site başına tek satır: yöntem, süre, kapı istisnası (JSONB), kargo yöntemi, ziyaretçi alanları (JSONB), saklama (1–24 ay), `auto_anonymize` (varsayılan false), `version` |
| `visitor_invites` | ön davet; `code_hash` (HMAC — kod saklanmaz), etkin davetlerde tekil (kısmi indeks); tip, tarih/gün/saat, plaka, durum, `anonymized_at` |
| `visit_requests` | habersiz ziyaretçi: yöntem, durum, `expires_at`, `visit_date` (tesis günü), karar kaynağı ve gerekçe (görevli kararında zorunlu — CHECK), ziyaretçi kaydı |
| `gate_logs` | plaka giriş/çıkış; sorgu sonucu dondurulur; `log_date`; plaka anonimleşince boş |
| `parking_violations` | otopark ihlali; sonuç, bölüm, bildirim hedefi (`resident` / `management`) |
| `parcel_delegates` | kargo teslim yetkilisi (yalnız ad); kaldırılınca `is_active = false` |
| `shift_log_entries` · `shift_log_addenda` · `shift_handover_acks` | nöbet defteri — **değişmez** (`forbid_history_change`); devir onayı devir başına tekil |
| `patrol_points` · `patrol_plans` · `patrol_runs` · `patrol_scans` | devriye; nokta kodu site içinde tekil; yürütme planı dondurur; planın tek açık yürütmesi (kısmi tekil indeks); okutma (yürütme, nokta) tekil |
| `parking_spaces` · `parking_allocations` | park yeri (kod tekil, tip, ücretli) ve tarihçeli tahsis — silinmez, bitirilir; çakışma uygulamada satır kilidiyle engellenir |

`visitors.anonymized_at` eklendi. Kişisel veri (ziyaretçi adı, plaka, not) saklama süresi dolunca
gece işiyle silinir ya da "Anonim" olur; kaydın kendisi (tarih, sayı) kalır.

### Operasyon ekleri ve teknik varlık [K] (göç 0032; OPS-01..06, HIZ-06/07; K28, K29)
Hepsi kiracı tablosu (RLS), denetim kaydında; silme yok (pasifleştirme / durum).

| Tablo | Ne | Not |
|---|---|---|
| `operation_settings` | site başına tek satır | `meter_anomaly_percent` (varsayılan 50), `meter_photo_required` |
| `facility_keys` | anahtar / kart / kumanda | `code` site içinde tekil (büyük harf), `authorized_roles` JSONB |
| `key_loans` | teslim kaydı | açık teslim tekil (kısmi indeks `returned_at IS NULL`); `to_phone` denetimde maskeli |
| `deposited_keys` | sakinin emanet anahtarı | `authorized` JSONB `[{name, phone}]` (denetimde `***`), `status` requested/held/handed/returned; `expired` okumada |
| `stock_counts` · `stock_count_lines` | sayım ve satırları | sayım anındaki sistem miktarı + fark; `approval_request_id` ("Stok sayım farkı" onayı) |
| `checklist_templates` · `checklist_inspections` | denetim şablonu (maddeler JSONB) ve saha denetimi | denetim **değişmez**; yanıtlar `[{text, type, value, ok, photo_id}]`, açılan talepler |
| `insurance_policies` · `insurance_claims` | poliçe ve hasar dosyası | `end_date > start_date`; ödendi → `paid` + `paid_on` (kısıt); belgeler `documents.subject_*` |
| `meters` · `meter_readings` | sayaç ve dönem okuması | aktif seri no tekil; dönem başına tek geçerli okuma (kısmi indeks `status <> 'reread'`); fotoğraf `stored_files` |
| `task_templates` · `task_completions` | görev şablonu ve günlük kapanış | şablon `staff_member_id` (K29); kapanış (şablon, gün) tekil ve **değişmez** |
| `technical_assets` | teknik varlık kartı (**K28: `assets`'ten ayrı**) | kod `TV-0001` sıralı, `contract_id` → `contracts` |
| `asset_inspection_schedules` · `asset_inspection_records` | yasal muayene takvimi ve sonucu | `next_due_on` saklanır; kayıt **değişmez**, `report_document_id` zorunlu, olumsuzda `request_id` |
| `maintenance_plans` · `maintenance_runs` | periyodik bakım planı ve dönemi | (plan, `due_on`) tekil; iş emri `request_id`, bakımda bulunan arıza `issue_request_id` |

## 9b. Finans motoru (göç 0028; P03b)

| Tablo | Not |
|---|---|
| `billing_rules` [K] | aidat kuralı kimliği: ad, `charge_type_id` (kim öder), `owner_item` (malik kalemi) |
| `billing_rule_versions` [K] | sürüm: `version_no`, `status` (draft · approval_pending · active · rejected), yürürlük yıl/ay, `kind`, `area_basis`, `amount_mode` (per_unit · total), `amount`, `frequency`, `components`/`type_coefficients`/`exemptions` JSONB, kapsam, `discontinued`, `basis_note` (zorunlu), `basis_document_id` → documents |
| `charge_adjustments` [K] | satır düzeltmesi: charge, charge_line, hesap, eski/yeni tutar, gerekçe, durum (approval_pending · applied · rejected), `prior_period`, `applied_on`, ters/yeni hareket kimlikleri; borç başına tek bekleyen (kısmi indeks) |
| `late_fee_accruals` [K] | **değişmez** — gecikme hareketinin ana borç dökümü (`principal_entry_id`, tutar, `as_of`) |
| `late_fee_runs` [K] | ay sonu gecikme işinin dönem başına sonucu (bir kez) |
| `budgets` [K] | yıllık bütçe sürümü: yıl, `version`, durum (draft · approval_pending · active · rejected · superseded; yıl başına tek active), `income_target`, öneri kaynağı `budget_plan_id` |
| `budget_lines` [K] | bütçe × gider kategorisi × ay (1–12) tutarı |
| `budget_alerts` [K] | **değişmez** — kategori %90/%100 uyarısı, bütçe × kategori × eşik başına bir |

`budget_plans.finalize_request_id` ("Bütçe" onayı, D-16). Onay işlem tiplerine `budget` ve
`charge_adjustment` eklendi.

## 9a. Finans Mod P işlemleri (göç 0027; servis istekleri 27–33)

| Tablo | Özet |
|---|---|
| `periods` (genişletildi) [K] | `status`: `open` · `closing` · `locked` · `temp_open` (eski `closed` → `locked`); `exceptions` (madde → gerekçe), kapanışı başlatan, kilitleyen, `unlock_until`, `unlock_reason`, `unlock_request_id` (onay bekleyen açma) |
| `period_events` [K] **değişmez** | dönem durum geçmişi (`start_close`, `reopen`, `lock`, `unlock_requested`, `unlock`, `unlock_rejected`, `relocked`) |
| `cash_days` [K] | kasa gün sonu sayımı: `system_balance`, `counted`, `difference`, `reason`, `status` (`closed` · `approval_pending` · `rejected`); hesap + gün başına bir geçerli sayım (kısmi benzersiz) |
| `collection_actions` [K] **değişmez** | hatırlatma / arama / not; gece işi kademesi `stage` + `due_ref` (aynı açık vade için bir kez) |
| `demand_letters` [K] | ihtarname gönderimi: yöntem, tarih, takip no, tebliğ tarihi, durum |
| `payment_plans` [K] | ödeme planı: tutar, taksit sayısı, ilk vade, tolerans (parametreden kopya), durum (`approval_pending` · `active` · `cancelled` · `rejected`; bozuldu/tamamlandı taksitlerden) |
| `payment_plan_installments` [K] **değişmez** | taksit → cari borç hareketi (`source: payment_plan`) |
| `payment_plan_closures` [K] **değişmez** | planın kapattığı borç hareketleri (`restructure` · `cancel`) — açık borç hesabı bunları düşer |
| `ad_hoc_charge_settings` · `ad_hoc_charges` [K] | dönem dışı ücret ayarı (kaynak → yansıma, vade günü) ve ücretler (borç hareketi `source: ad_hoc_charge`, iptal ters kayıt) |
| `accounting_mapping_versions` [K] **değişmez** | hesap kodu eşlemesi (versiyonlu) |
| `accounting_exports` · `accounting_export_items` [K] **değişmez** | üretilen fiş dosyası (içerik saklanır) ve aktarılan fişler (`voucher_key` sitede tekil → fiş bir kez aktarılır) |
| `finance_parameter_versions` [K] | finans parametresi sürümü: yürürlük ayı, tüm değerler (JSONB), değişen alanlar, durum |

`ledger_entries.source` değerlerine `ad_hoc_charge`, `payment_plan`; onay işlem tiplerine
`period_unlock`, `cash_difference`, `ad_hoc_charge`, `finance_parameter`, `demand_letter` eklendi.

## 9b. İletişim, acil durum ve şeffaflık (göç 0036 — ILT-01..06, RAP-02)

| Tablo | Not |
|---|---|
| `announcements` (+ sütunlar) | `channels[]`, zamanlama `scheduled_for` · `requested_publish_at` · `cancelled_at` (`published_at` boşken), okuma onayı `requires_ack` · `ack_hours` (1–720) · `ack_deadline` · `sms_fallback` ve yedek izi `sms_fallback_ran_at/_count/_sent` |
| `announcement_deliveries.acked_at` | "Okudum, anladım" anı (okundu da sayılır) |
| `notification_template_versions` | şablon sürümü (`code`, `version` site içinde tekil, `variants` JSONB, `customized`) — **değişmez**; satır yoksa sürüm 1 = sistem metni |
| `emergencies` · `emergency_units` | acil durum (tek aktif: kısmi tekil indeks `ended_at IS NULL`), kapsam başlatmada sabitlenen bölümler ve son durumu; bitişte `summary` + `purge_on`; ayrıntı `run-emergency-purge` ile silinir (`purged_at`) |
| `emergency_settings` | site başına: `retention_days` (30), `auto_purge` (false — K8) |
| `emergency_info_cards` | site başına tek kart (JSONB: toplanma alanları, telefonlar, talimatlar, blok notları), her yayında `version` artar |
| `transparency_reports` | dönem (`year`, `month`; reddedilmemiş rapor dönemde tekil), `status` `draft` · `pending` · `published` · `rejected`, donmuş `content` (kişi/bölüm yok), `scope`, onay talebi bağı |

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


## Kimlik ve bildirim gönderimi (göç 0043, P5 / K5)

- `users` [G]: `phone` (E.164, tekil), `email` ve `password_hash` boş olabilir (en az biri:
  e-posta ya da telefon), `phone_changed_at`, `mfa_secret_sealed` (şifreli), `mfa_enabled_at`,
  `mfa_last_step`, `mfa_grace_until`.
- `auth_sessions` [G]: `mfa_pending`, `verified_at`.
- `otp_challenges` [G]: amaç, kanal, hedef, kod özeti (HMAC), son geçerlilik, deneme, kilit,
  sağlayıcı referansı ve SMS adedi.
- `mfa_recovery_codes` [G]: yedek kod özeti, kullanım zamanı.
- `user_invitations` [G]: jeton özeti, 72 saat, kabul/iptal; site adı yalnız gösterim.
- `notification_deliveries` [K]: bildirim × kanal teslim kaydı (durum, sağlayıcı, maliyet birimi,
  kota aşımı, sessiz saat ertelemesi, SMS yedeği bağı).
- `site_messaging_settings` [K]: sessiz saatler, SMS hesabı (platform/kendi), başlık, şifreli
  anahtar + son 4, aylık kota (boş → paket varsayılanı).
- `scheduled_reports` / `scheduled_report_runs` [K]: planlı rapor tanımı ve çalışma geçmişi.
