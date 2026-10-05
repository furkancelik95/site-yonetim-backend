# 06 — API sözleşmesi

Frontend ile backend arasındaki anlaşma. **Bölüm 1 bağlayıcıdır.** Bölüm 2 planlanan uç
noktaların listesidir; her birinin ayrıntısı frontend'in açtığı servis isteği issue'sunda
netleşir. Çelişki olursa issue'da konuşulur, sonra bu doküman güncellenir.

Güncel şema her zaman OpenAPI'dir (`/api/v1/openapi.json`). Frontend tip üretimini oradan yapar.

---

## 1. Genel kurallar (bağlayıcı)

### 1.1 Adres
- Kök: `/api/v1`
- Siteye ait her şey: `/api/v1/sites/{slug}/...` — `slug` sitenin adres ekidir (`aksu-konaklari`).
- Kaynak adları İngilizce, çoğul, `kebab-case`: `/charge-runs`, `/cash-accounts`.
- Kimlikler UUID.

### 1.2 Değer biçimleri
| Tür | JSON'da | Örnek |
|---|---|---|
| **Para** | **metin**, 2 ondalık | `"1234.56"`, `"-500.00"` |
| Tarih | metin `YYYY-MM-DD` | `"2026-06-15"` |
| Zaman | metin ISO 8601, UTC | `"2026-06-15T09:30:00Z"` |
| Enum | küçük harf `snake_case` metin | `"bank_transfer"`, `"in_progress"` |
| Alan adları | `snake_case` | `"due_date"`, `"ledger_account_id"` |
| Boş değer | `null` (alan atlanmaz) | |

Para neden metin: JavaScript'te sayı kayan noktadır; `0.1 + 0.2 = 0.30000000000000004`.
Frontend parayı ekranda gösterirken biçimlendirir (`1.234,56 ₺`), hesap yapması gerekirse
ondalık kütüphanesi kullanır.

### 1.3 Liste ve sayfalama
Liste döndüren **her** uç nokta sayfalıdır:
```
GET /api/v1/sites/{slug}/debtors?page=1&page_size=50&sort=-balance&q=ayşe
```
```json
{
  "items": [ ... ],
  "page": 1,
  "page_size": 50,
  "total": 1284
}
```
- `page_size` varsayılan 50, en fazla 200.
- `sort`: alan adı; başında `-` azalan.
- `q`: serbest arama (varsa).
- Toplamlar/özet gerekiyorsa ayrı bir `summary` nesnesi eklenir (ör. borçlular: toplam açık bakiye).

### 1.4 Hata biçimi
Her hata aynı gövdeyi döner:
```json
{
  "error": {
    "code": "period_already_charged",
    "message": "09/2026 dönemi için tahakkuk zaten kesilmiş. Aynı dönem ikinci kez kesilemez; düzeltme gerekiyorsa önce ters kayıt alın.",
    "fields": { "amount": "Tutar sıfırdan büyük olmalı." }
  }
}
```
- `code`: makine için, İngilizce `snake_case`, sabit.
- `message`: **Türkçe**, kullanıcıya olduğu gibi gösterilebilir, ne yapması gerektiğini söyler.
- `fields`: yalnız doğrulama hatalarında; alan adı → Türkçe mesaj. Frontend bunu ilgili alanın
  altında gösterir.

| Kod | Ne zaman |
|---|---|
| 400 | İstek biçimi bozuk |
| 401 | Giriş yok / oturum düştü |
| 403 | Siteye erişim var ama bu işleme izin yok |
| **404** | Kayıt yok · **siteye erişim yok** · modül kapalı · platform ucu ve kullanıcı platform yöneticisi değil |
| 409 | İş kuralı çakışması (aynı dönem ikinci tahakkuk, zaten ödenmiş gider…) |
| 422 | Doğrulama hatası (`fields` dolu) |
| 429 | Hız sınırı (giriş denemeleri) |

### 1.5 Yazma işlemleri
- Para yazan uç noktalar (tahsilat, tahakkuk kaydı, gider, kasa hareketi, aktarım)
  **`Idempotency-Key`** başlığını kabul eder. Aynı anahtarla ikinci istek, ilkinin sonucunu
  döndürür, yeni kayıt açmaz (yanıtta `Idempotent-Replayed: true`).
  - Anahtar 8–100 karakter (`A-Z a-z 0-9 - _ . :`), kullanıcı + site başına, **24 saat** geçerli.
  - Aynı anahtar farklı bir istekle (yol/gövde) → 422 `idempotency_key_reused`.
  - Aynı anahtarla eşzamanlı iki istek → biri yazar, diğeri 409 `request_in_progress`.
  - Yanıt yazmayla aynı transaction'da saklanır; yazma geri alınırsa anahtar da kalmaz.
- Başarılı yazma, oluşan kaydı ve kullanıcıya gösterilecek **Türkçe `message`** döner:
  ```json
  { "data": { ... }, "message": "A-12 — Ayşe YILMAZ: 1.000,00 ₺ tahsilat kaydedildi, 1 borç kaydına mahsup edildi." }
  ```
- Silme yok (finansal tablolar). Düzeltme = `.../reverse` uç noktası, gövdede `reason` zorunlu.

### 1.6 Dosya
- Yükleme: `multipart/form-data`.
- İndirme: `GET .../files/{id}` → dosyanın kendisi, `Content-Disposition: inline`. Doğrudan dosya
  sistemi adresi **yoktur**; her indirme yetki kontrolünden geçer.
- Excel çıktısı: `GET .../export.xlsx` → `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`.

### 1.7 Kimlik
`Authorization: Bearer <erişim jetonu>`. Ayrıntı: `05-yetki.md` §8.

---

## 2. Uç nokta kataloğu

**Durum** sütunu: `R` = referans uygulamada (.NET) var, davranışı belli · `Y` = yeni, tasarlanacak.
**İzin** sütunu `05-yetki.md` §2. **Modül** kapalıysa 404.

### 2.1 Kimlik
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| POST | `/auth/login` | — | R | `{email, password}` → `{access_token, token_type: "bearer", expires_in, must_change_password}` + `sy_refresh` çerezi. Hatalı: 401 `invalid_credentials`; 5 hatada 15 dk kilit: 429 `account_locked`; aynı adresten 15 dk'da 20 hatalı deneme: 429 `too_many_attempts` (+ `Retry-After`, parola denenmez) |
| POST | `/auth/change-password` | giriş | R | `{current_password, new_password}` → 204. Geçici parolada (`must_change_password: true`) zorunlu; o zamana kadar `/me` dışındaki uçlar 403 `password_change_required`. Hatalar 422: `invalid_current_password` (hesap kilidi sayacını artırır), `weak_password` (en az 8), `password_unchanged`. Başarılıysa **diğer oturumlar iptal**, bu oturum sürer |
| POST | `/auth/refresh` | çerez | Y | gövde yok; çerezle → yeni `access_token` + yeni çerez. Geçersiz/süresi dolmuş: 401 `session_expired`. Frontend `credentials: "include"` ile çağırır |
| POST | `/auth/logout` | — | R | Bearer ve/veya çerez; oturumu iptal eder, çerezi siler. Her zaman 204 |
| GET | `/me` | giriş | R | `UserAccess` — `05-yetki.md` §4 + `must_change_password`. Frontend yönlendirmesi buna bakar (geçici paroladayken de çalışır) |

### 2.2 Platform (yalnız platform yöneticisi — değilse 404)
| Yöntem | Yol | Durum | Not |
|---|---|---|---|
| GET | `/platform/overview` | R | müşteri sayısı, site sayısı, bölüm sayısı, tavanı aşan siteler, müşteri ve site listeleri (sayfalı: `customers`, `sites`). Yalnız kullanım ölçüsü — borç/sakin verisi yok (`05` §5) |
| GET | `/platform/plans` | R | sayfalı |
| POST | `/platform/customers` | R | `{name, tax_number?, plan_id, admin_full_name, admin_email}` → organizasyon + ilk yetkili (Sahip) + **bir kez gösterilen geçici parola** (`Cache-Control: no-store`). Tek transaction. Aynı e-posta 409 `email_already_exists`, olmayan plan 422 |
| POST | `/platform/sites` | R | `{name, slug?, plan_id, organization_id?, property_kind, city?, district?, iban?, bank_name?}` → site + varsayılan kurulum (`10-demo-veri.md` §3). IBAN TR + mod-97 doğrulanır. Aynı ad (Türkçe büyük/küçük harf duyarsız) ya da slug 409 `site_already_exists` |
| PATCH | `/platform/sites/{id}/plan` | R | `{plan_id}` |

### 2.3 Portföy
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/portfolio` | `portfolio.read` / >1 site | R | site başına: bölüm, tahakkuk, tahsilat, açık bakiye, tahsilat oranı, açık/geciken talep, sağlık durumu. Uygulama: >1 siteye erişim (tek siteli ve platform yöneticisi 404); `finance` yalnız o sitede `finance.read`, `requests` yalnız `requests.read` varsa (yoksa null); geciken = hedef tarihi (`due_at`) geçmiş açık talep. **Sağlık durumu henüz yok** (`12` K19) |

### 2.4 Site: pano ve yapı
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}` | erişim | R | site bilgisi + açık modüller + kullanıcının o sitedeki izinleri |
| GET | `/sites/{slug}/dashboard` | `finance.read` | R | toplam tahakkuk/tahsilat, bu ay, açık bakiye, borçlu sayısı, ilk 5 borçlu, açık talepler, son duyurular, son giderler. **Özet tablodan**, `08-performans.md`. Yanıt: `finance{total_charged, total_collected, month_charged, month_collected, open_balance, debtor_count, over_30_days, over_60_days, average_debt}`, `top_debtors[]`, `requests` (modül + `requests.read`, yoksa null), `announcements` (son 3), `expenses` (son 5, `expenses.read`), `cash_balance` (`finance.cash.read`) |
| GET | `/sites/{slug}/units` | `units.read` | R | sayfalı; `q` (no / blok+no / unvan), `block_id`, `is_active`. bölüm, blok, tip, m², arsa payı, kullanım, **bugünkü** malik ve kiracı adları. Bakiye finans diliminde eklenecek |
| GET | `/sites/{slug}/units/{id}` | `units.read` | Y | bölüm ayrıntısı + taraflar (geçmiş dahil) + hesaplar |
| POST | `/sites/{slug}/units` | `units.manage` | Y | elle bölüm ekleme |
| PATCH | `/sites/{slug}/units/{id}` | `units.manage` | Y | |
| POST | `/sites/{slug}/units/{id}/parties` | `people.manage` | Y | `{role, start_date, share_percent?, person_id \| person}` — gerekli cari hesapları da açar (`03` §5); malik hisseleri toplamı ≤ 100 (409) |
| POST | `/sites/{slug}/units/{id}/parties/{party_id}/end` | `people.manage` | Y | `{end_date}` — kiracı çıkışı. **Silme değil, bitiş tarihi** |
| POST · PATCH | `/sites/{slug}/blocks`, `/blocks/{id}` | `units.manage` | Y | blok ekle/güncelle; ad sitede benzersiz (Türkçe harf duyarsız) |
| GET | `/sites/{slug}/people` | `people.read` | Y | sayfalı; `q` ad soyad içinde (Türkçe harf duyarsız) |
| POST · PATCH | `/sites/{slug}/people`, `/people/{id}` | `people.manage` | Y | ad baş harf büyük, soyad tamamı büyük; telefon E.164 (cep); e-posta küçük harf |
| GET | `/sites/{slug}/blocks`, `/unit-types` | `units.read` | R | |

### 2.5 Excel içe aktarma
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/imports/units/template.xlsx` | `units.manage` | R | şablon — `11-excel-aktarim.md` |
| POST | `/sites/{slug}/imports/units` | `units.manage` | R | dosya yükle → **önizleme**: satırlar, hata ve uyarılar, aktarılabilir satır sayısı. Hiçbir şey yazılmaz |
| POST | `/sites/{slug}/imports/units/{import_id}/confirm` | `units.manage` | R | önizlenen aktarımı uygula |

- Yükleme `multipart/form-data`, alan adı `file`. Dosya hataları **422** (`invalid_file_type`,
  `invalid_file_content`, `file_too_complex`, `missing_columns`, `too_many_rows`, `empty_file`;
  `fields.file` dolu) · 5 MB üstü **413**.
- Önizleme: `{import_id | null, expires_at, total_rows, importable_count, new_count,
  existing_count, error_count, warning_count, rows[], issues[]}`. Satırda `already_exists`
  (onayda atlanır); telefon/e-posta yalnız `people.read` ile. Sorun: `{row_number | null,
  column, message, severity: "error" | "warning"}` — `row_number` Excel satır numarasıdır;
  `null` dosya geneli (ör. plan tavanı uyarısı).
- Onay → `Written`: `{created_units, created_people, created_accounts, created_blocks,
  created_unit_types, skipped[]}` + Türkçe `message`. Başkasının, başka sitenin, süresi dolmuş
  ya da zaten onaylanmış aktarımı **404** `import_not_found` · eşzamanlı çakışma **409**
  `import_conflict`.

### 2.6 Finans
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/debtors?q=` | `finance.read` | R | sayfalı, bakiye büyükten küçüğe; satırda `overdue_days`; `summary`: `total_balance`, `debtor_count`, `over_30_days`/`_count`, `over_60_days`/`_count`, `average_balance` — `04` §13 |
| GET | `/sites/{slug}/accounts?q=&unit_id=` | `finance.read` | Y | cari hesaplar + bakiye (tahsilat girişinde hesap arama: referans, `A-12`, kişi adı) |
| GET | `/sites/{slug}/accounts/{id}/statement` | `finance.read` veya kendi hesabı | R | cari ekstre: `account` (bakiye, en eski açık vade), sayfalı `entries` (**en yeni üstte**, `running_balance`; ilk satırınki = güncel bakiye), `last_charge` (son tahakkukun kalem dökümü). Başkasının hesabı **403** |
| GET | `/sites/{slug}/budget-plans/current` | `finance.read` | R | kesinleşmiş son proje + kalemleri; yoksa 404 |
| GET | `/sites/{slug}/budget-plans` · `/budget-plans/{id}` | `finance.read` | Y | sayfalı liste · ayrıntı (kalemlerle) |
| POST | `/sites/{slug}/budget-plans` | `finance.budget.manage` | Y | `{fiscal_year, name}` → taslak |
| POST · PUT · DELETE | `/sites/{slug}/budget-plans/{id}/items[/{item_id}]` | `finance.budget.manage` | Y | kalem ekle/güncelle/kaldır — **yalnız taslakta** (aksi 409). `{name, expense_category_id, charge_type_id, allocation_rule_id, annual_amount, frequency, scope_kind, scope_block_ids?, scope_unit_type_ids?, scope_usage?, sort_order}` |
| POST | `/sites/{slug}/budget-plans/{id}/notify` | `finance.budget.manage` | Y | `{notified_on}` → `notified`, itiraz son günü = +7 gün |
| POST | `/sites/{slug}/budget-plans/{id}/finalize` | `finance.budget.manage` | Y | itiraz süresi **dolduktan sonra** (son günün ertesi); öncekiler `superseded` |
| GET | `/sites/{slug}/charge-types` · `/allocation-rules` · `/expense-categories` | `finance.read` | Y | seçim listeleri (site açılışında varsayılanlar kurulur — `10` §3) |
| GET | `/sites/{slug}/charge-runs` · `/charge-runs/{id}` | `finance.read` | R | geçmiş koşular (sayfalı, en yeni üstte) · ayrıntı |
| GET | `/sites/{slug}/charge-runs/{id}/charges` | `finance.read` | Y | koşunun borçları + kalem dökümü (sayfalı) |
| GET | `/sites/{slug}/charge-runs/preview?charge_date=&due_date=` | `finance.charge.post` | R | varsayılan **sıradaki dönem** — `04` §4, §7.2. Hiçbir şey yazılmaz. Özet + uyarılar + kalem toplamları + `not_due_items` + sayfalı `charges` |
| POST | `/sites/{slug}/charge-runs` | `finance.charge.post` | R | `{charge_date?, due_date?}` → kaydet (201). 409: dönem zaten kesilmiş, kesinleşmiş proje yok, kesilecek tahakkuk yok. `Idempotency-Key`. Büyük sitede arka plan işi (`202`) — **henüz yok**, istek içinde çalışır |
| POST | `/sites/{slug}/charge-runs/{id}/reverse` | `finance.charge.post` | R | `{reason}` (3–500). 409: zaten ters kaydedilmiş / ters kayıt koşusu. `Idempotency-Key` |
| GET | `/sites/{slug}/charge-schedule` | `finance.read` | R | **otomatik aylık tahakkuk**: `{enabled, charge_day, due_days, notify_on_run, next_run_on, last_run{run_id, period, ran_at, status, message}}`. Kaydedilmediyse varsayılan (kapalı, gün 1, vade 14). `next_run_on` kapalıyken null (İstanbul tarihi). `last_run.status`: `posted` · `skipped` · `failed`; `run_id` yalnız kesildiyse dolu |
| PUT | `/sites/{slug}/charge-schedule` | `finance.charge.post` | R | `{enabled, charge_day (1–28), due_days (0–60), notify_on_run}` → aynı nesne + mesaj. Sınır dışı 422 `fields.charge_day` / `fields.due_days`. Kesimi gece işi yapar, **elle kaydetmeyle aynı servis**: dönem kesilmişse, proje yoksa ya da önizlemede uyarı varsa kesmez (`skipped`). Açıldığı günden önceki kesim günü geriye dönük kesilmez; sunucu kesim günü çalışmadıysa ay içinde ertesi gün yetişir |
| POST | `/sites/{slug}/payments` | `finance.payment.record` | R | `{ledger_account_id, amount, date, method, reference?, note?, cash_account_id?}` → `{payment, applied, unapplied, closed_debt_count, balance}` (201). `method`: `cash` · `bank_transfer` · `credit_card` · `other`. Kasa seçildiyse kasaya giriş hareketi de yazılır. `Idempotency-Key` |
| GET | `/sites/{slug}/payments?account_id=&from=&to=` | `finance.read` | Y | sayfalı, en yeni üstte |
| GET | `/sites/{slug}/payments/{id}` | `finance.read` veya kendi hesabı | Y | **makbuz verisi**: site, tahsilat, hesap, kapatılan borçlar (`allocations`), `applied`/`unapplied`. `receipt_number` şimdilik `null` (`12` K15) |
| POST | `/sites/{slug}/accounts/{id}/clearance-certificates` | `finance.payment.record` | R | **borçsuzluk belgesi** — gövde yok → `{id, number, site{name, slug}, account{id, reference_code, kind, unit_name, person_name}, balance, as_of, issued_at, issued_by, valid_until}` (201). Bakiye belge anında defterden; > 0,005 ise 409 `has_debt` ("Bu hesabın 1.000,00 TL borcu var; …"), kapalı hesap 409 `account_closed`. Numara `BB-{yıl}-{5 hane}`, site + yıl bazında boşluksuz. Belge değişmez. `Idempotency-Key` |
| GET | `/sites/{slug}/clearance-certificates/{id}` | `finance.read` | R | aynı nesne (`data` sarmalı yok) — yazdırma sayfası. Sakin göremez |
| POST | `/sites/{slug}/accounts/{id}/opening-balance` | `finance.payment.record` | R | **devir bakiye** `{amount, direction: debit\|credit, date, description?}` → `{id, amount, direction, date, description}` (201). Defter hareketi `source: opening`; `debit` vadesi devir tarihi olan borç (FIFO mahsuba, borçlulara girer), `credit` avans. Hesap başına bir kez: 409 `already_exists`. İleri tarih 422 `fields.date`. Pano özetini değiştirmez. `Idempotency-Key` |
| POST | `/sites/{slug}/refunds` | `finance.payment.record` + `finance.cash.manage` | R | **iade** `{ledger_account_id, amount, date, cash_account_id, reason}` → `{id, ledger_account_id, amount, date, cash_account_id, reason}` (201). Yalnız alacaklı bakiye: alacak yok 409 `no_credit`, aşan tutar 422 `fields.amount` ("En fazla 1.250,00 TL iade edilebilir."), gerekçe boş 422 `fields.reason`. Tek transaction: cari borç (`source: refund`, açık borç sayılmaz) + kasadan çıkış. Kapalı hesaba yapılabilir (taşınan sakin). Değişmez. **`Idempotency-Key` zorunlu** (yoksa 422 `idempotency_key_required`) |
| POST | `/sites/{slug}/bank-imports` | `finance.payment.record` + `finance.cash.manage` | R | **banka hareketi aktarımı — önizleme**, multipart `file` (.xlsx/.xls/.csv, ≤ 5 MB) + `cash_account_id` (banka türü). Hiçbir tahsilat yazılmaz; dosya diske yazılmaz, satırlar 6 saat saklanır. Başlık ilk 30 satırda aranır (Tarih, Açıklama, Tutar ya da Borç/Alacak, Dekont No). Satır `status`: `matched` (açıklamada referans kodu) · `suggested` (gönderen adı tek hesapla ya da aynı kişinin tek borçlu hesabıyla eşleşiyor) · `unmatched` · `ignored` (çıkış) · `duplicate` (aynı hesaba daha önce aktarıldı: tarih + tutar + dekont no, yoksa açıklama). Sütun tanınmazsa 422 `fields.file` hangi sütun olduğunu söyler |
| POST | `/sites/{slug}/bank-imports/{id}/confirm` | aynı | R | `{rows: [{row_number, ledger_account_id}]}` → `{created_payments, total_amount, skipped[{row_number, code, message}]}`. Tek transaction; her satır tahsilat servisiyle (FIFO, defter, bankaya giriş, `bank_transfer`, `reference` = dekont no). İşlenemeyen satır `skipped`. Yalnız yükleyen onaylar; süresi dolmuş/başkasının 404; ikinci onay 409 `already_confirmed`. Aynı hareket iki ayrı onayda da bir kez (benzersiz kısıt). `Idempotency-Key` |

Kişi adları (`person_name`) `people.read` izni olmayana `null` döner — Denetçi finansı görür,
kişisel veriyi görmez (`05`). Sakin kendi hesabında kendi adını görür.

### 2.7 Gider
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/expenses` | `expenses.read` | R | sayfalı; filtre `year`, `category_id`, `paid=all\|paid\|unpaid`; `summary`: `total` (gerçekleşen), `unpaid_total`/`unpaid_count`, `by_category[]` |
| GET | `/sites/{slug}/expenses/{id}` | `expenses.read` | Y | |
| POST | `/sites/{slug}/expenses` | `expenses.manage` | R | multipart (`multipart/form-data` ya da dosyasızsa form): `expense_category_id, description, amount, date, vendor?, document_number?, note?, paid?, paid_on?, cash_account_id?` + `document?` (PDF/JPG/PNG/WEBP, ≤ 10 MB, içerik imzası). `Idempotency-Key` |
| POST | `/sites/{slug}/expenses/{id}/pay` | `expenses.manage` | R | `{cash_account_id, paid_on}` · `Idempotency-Key` |
| POST | `/sites/{slug}/expenses/{id}/reverse` | `expenses.manage` | R | `{reason}` → eksi tutarlı düzeltme kaydı · `Idempotency-Key` |
| GET | `/sites/{slug}/expenses/export.xlsx` | `expenses.read` | R | aynı filtreler |
| GET | `/sites/{slug}/recurring-expenses` | `expenses.read` | R | **tekrarlanan gider** tanımları (dizi): `{id, description, expense_category_id, amount, vendor, day_of_month, is_active, auto_pay, cash_account_id, next_run_on, last_created_on, created_at}` |
| POST · PATCH · DELETE | `/sites/{slug}/recurring-expenses[/{id}]` | `expenses.manage` (+ `auto_pay` açmak için `finance.cash.manage`) | R | gün 1–28; `auto_pay` hesap ister. PATCH kısmi (`{is_active:false}` durdurur; yeniden başlatınca arada kalan günler geriye dönük yazılmaz). DELETE tanımı arşive alır, oluşmuş giderler yerinde kalır. Giderleri gece işi yazar: açıklama `"{tanım} — AA/YYYY"`, ay başına bir |
| GET | `/sites/{slug}/files/{id}` | `expenses.read` (belgenin bağlı olduğu kayda göre) | R | `Content-Disposition: inline`, temizlenmiş ad (`filename*` UTF-8) |

### 2.8 Kasa ve banka
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/cash-accounts` | `finance.cash.read` | R | `{items: [hesap + inflow, outflow, balance], total_balance}` — toplam yalnız aktif hesaplar |
| POST | `/sites/{slug}/cash-accounts` | `finance.cash.manage` | R | `{name, kind, bank_name?, iban?, opening_balance, opening_date?, note?}` — açılış ≠ 0 ise açılış hareketi. `Idempotency-Key` |
| GET | `/sites/{slug}/cash-accounts/{id}/statement` | `finance.cash.read` | R | sayfalı, `from`, `to` — `04` §10.5: `opening` (devreden), `closing`, `total_in`, `total_out`, `movements` (en yeni üstte, `running_balance`, `is_reversed`) |
| GET | `/sites/{slug}/cash-accounts/{id}/statement/export.xlsx` | `finance.cash.read` | R | sayfalama yok, tüm aralık |
| POST | `/sites/{slug}/cash-movements` | `finance.cash.manage` | R | elle hareket `{cash_account_id, date, direction: in\|out, amount, description, reference?}` · `Idempotency-Key` |
| POST | `/sites/{slug}/cash-transfers` | `finance.cash.manage` | R | `{from_id, to_id, date, amount, note?}` → `{outgoing, incoming}` · `Idempotency-Key` |
| POST | `/sites/{slug}/cash-movements/{id}/reverse` | `finance.cash.manage` | R | `{reason}`; tahsilat/gider kaynaklıysa 409 · `Idempotency-Key` |

### 2.9 Rapor
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/reports/income-expense?year=2026` | `finance.reports.read` | R | `04` §12: `total_income`, `total_expense`, `difference`, `months[12]`, `categories[]` (pay %), `budget_plan`, `budget[]` (bütçelenen × gerçekleşen, kullanım %, `is_over`), `cash_balance`, `years[]` |
| GET | `/sites/{slug}/reports/income-expense/export.xlsx?year=2026` | `finance.reports.read` | R | 3 sayfa: Özet, Kategori, İşletme Projesi |
| GET | `/sites/{slug}/reports/collections?year=2026` | `finance.reports.read` | Y | **aidat tahsilat özeti**: dönem başına `charged` (geçerli koşunun borcu), `collected` (o borçlara mahsuplar), `outstanding`, `rate` %; yıl toplamları |

Yüzdeler metin (`"5.42"`). Excel'de kullanıcı metni her zaman metin hücresidir — `=` ile başlayan
açıklama formül olarak çalışmaz (`09` §3.3).

### 2.10 Talep
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/requests?status=&category=&priority=&department_id=` | `requests.read` veya kendi talepleri | R | sayfalı, yeni üstte. İzni olmayan sakin yalnız kendi açtıklarını görür. Satırda `department_id`, `department_name` |
| GET | `/sites/{slug}/requests/{id}` | `requests.read` veya kendi talebi | R | + olay geçmişi (`events`, eskiden yeniye). Başkasının talebi 404 |
| POST | `/sites/{slug}/requests` | `requests.create` | R | `{title, description?, category, priority, unit_id?, location?, reported_by_person_id?}` → numara site içinde artan. Sakin kendi adına ve yalnız kendi bölümü (ya da ortak alan) için açar (422 `unit_not_yours`) |
| POST | `/sites/{slug}/requests/{id}/status` | `requests.assign` | R | `{status, resolution?}` — çözüldü/kapandı yapılırken `resolution` zorunlu (422); aynı durum 409 |
| POST | `/sites/{slug}/requests/{id}/assign` | `requests.assign` | R | `{assignee}` |
| POST | `/sites/{slug}/requests/{id}/department` | `requests.assign` | R | `{department_id \| null}` → talep ayrıntısı; geçmişe `department_changed` olayı. Pasif ya da başka sitenin departmanı 422 `fields.department_id` |
| GET | `/sites/{slug}/departments` | `requests.read` | R | dizi `{id, name, is_active, request_count}`; site açılışında Teknik, Temizlik, Güvenlik, Bahçe, Yönetim |
| POST · PATCH | `/sites/{slug}/departments[/{id}]` | `requests.assign` | R | `{name}` / `{name?, is_active?}`. Ad site içinde benzersiz (Türkçe harf duyarsız) → 409 `already_exists`. Silme yok, pasifleştirme var |
| POST | `/sites/{slug}/requests/{id}/comments` | `requests.read` veya kendi talebi | R | `{body}` |

Modül kapalıysa tüm talep uçları 404. `reporter_name` `people.read` izni ya da kendi talebi
değilse `null`. Durum geçişleri serbest (açık karar `12` K18).

### 2.11 Duyuru
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/announcements` | `announcements.read` | R | sabitlenmiş önce, sonra yayın tarihine göre. Yayınlayan hepsini (süresi geçenler dahil) + `recipient_count`/`read_count`; **sakin yalnız kendisine teslim edilen**, süresi geçmemiş olanları + `read_at`; diğer personel yürürlükteki hepsini |
| GET | `/sites/{slug}/announcements/{id}` | `announcements.read` | Y | aynı görünürlük; görmediği 404 |
| POST | `/sites/{slug}/announcements` | `announcements.publish` | R | `{title, body, importance, audience, audience_block_ids?, channels[], expires_on?, is_pinned}` → hedef kişiler için teslim kayıtları. **Gerçek gönderim: arka plan işi** — henüz yok (`12` K5): `in_app` hemen teslim, diğer kanallar bekler |
| POST | `/sites/{slug}/announcements/{id}/read` | `announcements.read` | Y | sakin okundu işaretler (ilk okuma anı korunur); kendisine teslim edilmemişse 404 |

Hedef kitle bugünkü etkin malik/kiracı/oturanlardan seçilir (vekil hariç): `blocks` seçilen
blokların bölümleri, `owners_only` malikler, `tenants_only` kiracılar, `debtors_only` bakiyesi
borçlu bir cari hesabı olanlar. Yayından sonra taraf değişse de teslim listesi değişmez.

### 2.12 Güvenlik (modül: `visitors`, `packages`)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/packages?status=&unit_id=` | `security.packages` | R | sayfalı, yeni üstte |
| POST | `/sites/{slug}/packages` | `security.packages` | R | `{unit_id, person_id?, carrier?, note?}` → 4 haneli teslim kodu üretir (`secrets`). **Kod güvenliğe dönmez** — sakin `resident/packages`'ta görür. Bildirim gönderimi yok (`12` K5) |
| POST | `/sites/{slug}/packages/{id}/deliver` | `security.packages` | R | `{pickup_code, delivered_to}` — kod sabit zamanlı doğrulanır; hatalı 422, teslim edilmiş 409 |
| GET | `/sites/{slug}/visitors?date=&status=` | `security.visitors` | R | gün (boşsa bugün): beklenen gün ya da kayıt günü |
| POST | `/sites/{slug}/visitors` | `security.visitors` | R | `{unit_id, full_name, kind, host_person_id?, phone?, plate_number?, expected_on?, note?, enter_now}` — bugünse hemen içeri, ileri tarihse `expected` |
| POST | `/sites/{slug}/visitors/{id}/enter` · `/exit` | `security.visitors` | R | yalnız `expected` → giriş, `entered` → çıkış (aksi 409) |
| GET | `/sites/{slug}/units/lookup?q=` | `security.*` | R | güvenlik için daire arama (en fazla 20): **yalnız bölüm adı ve oturan adı** (kiracı/oturan, yoksa malik) — borç ve telefon **yok** |

Modül kapalıysa ilgili uçlar 404; `units/lookup` için iki modülden biri açık olmalı.

**Olay kaydı ve kayıp eşya** (servis istekleri 09, 10) — modül `visitors`, izin `security.incidents`
(Yönetici, Güvenlik; Denetçi erişmez — açıklamada kişisel veri olabilir):

| Yöntem | Yol | Not |
|---|---|---|
| GET · POST | `/sites/{slug}/incidents?status=open\|closed` | `{kind, location, description, occurred_at?, unit_id?}`; `kind`: `theft` · `damage` · `noise` · `fire` · `water_leak` · `suspicious` · `accident` · `other`. `number` site içinde artan. Kayıt değişmez, silinmez |
| POST | `/sites/{slug}/incidents/{id}/close` | `{note}` zorunlu; kapalıysa 409 `already_closed` |
| GET · POST | `/sites/{slug}/lost-items?status=waiting\|returned\|disposed` | `{description, location, found_by?, found_at?}` |
| POST | `/sites/{slug}/lost-items/{id}/return` | `{returned_to}` teslim ya da `{disposed: true}`; yalnız bekleyen (409 `not_waiting`) |


### 2.13 Modüller
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/modules` | `modules.manage` | R | `[{key, enabled, in_plan, available, is_core}]` |
| POST | `/sites/{slug}/modules/{key}/toggle` | `modules.manage` | R | durumu tersine çevirir; çekirdek (`finance`) kapatılamaz, planda yoksa açılamaz (409); bilinmeyen anahtar 404. Kapatmak veriyi silmez |

### 2.14 Sakin (kendi verisi — `person_id` ile süzülür)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/resident/home` | sakin | R | `units[]` (bugünkü bölümleri ve rolü), `accounts[]` (bakiyeli), `total_balance`, `recent_entries[5]`, `announcements[3]` ve `open_requests[]` (modül kapalıysa null), `payment_info{bank_name, iban, account_holder}` (sitenin IBAN'ı yoksa null) |
| GET | `/sites/{slug}/resident/statement?account_id=` | sakin | R | kendi cari ekstresi; `account_id` boşsa oturan hesabı. Başkasının hesabı 404 |
| GET | `/sites/{slug}/resident/announcements` | sakin | R | kendisine teslim edilen, süresi geçmemiş duyurular (+ `read_at`). Modül kapalıysa 404 |
| GET · POST | `/sites/{slug}/resident/requests` | sakin | R | kendi talepleri / yeni talep — her zaman kendi adına, yalnız kendi bölümü ya da ortak alan (422 `unit_not_yours`). Modül kapalıysa 404 |
| GET | `/sites/{slug}/resident/expenses?year=` | sakin | **Y** | sitenin **gerçekleşen** gider dökümü (geri alınan ve düzeltmeler hariç) + `total_amount`; satırda `document_id`. Şeffaflık vaadinin karşılığı |
| GET | `/sites/{slug}/resident/packages` | sakin | Y | dairelerine gelen, teslim bekleyen kargolar **teslim koduyla** (modül: `packages`) |
| GET | `/sites/{slug}/resident/files/{id}` | sakin | Y | **fatura görüntüsü** — yalnız gerçekleşen bir giderin belgesi; başka dosya 404 |

"Sakin" = üyeliğinde `person_id` olan kullanıcı; yoksa **403** ("Bu ekran yalnız sakinler
içindir"). Sakin, `requests.read` izni olmadan da kendi taleplerini görür (`05` §6).

### 2.15 Denetim kaydı (`09` §6)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/audit?entity=&entity_id=&action=&user_id=&from=&to=` | `audit.read` | R | sayfalı, yeni üstte. Satır: `{id, at, user_id, actor_name, action, entity, entity_id, before, after, ip}`. `action`: `create` · `update` · `delete` · `import` · `download`. `entity` tablo adıdır (`payments`, `expenses`, `budget_items`, `site_modules` …). Güncellemede `before`/`after` yalnız değişen alanları taşır. `from`/`to` iş günü (İstanbul). `user_id` null: sistem (demo, gece işi) |

### 2.16 Site kullanıcıları (servis isteği 12)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/roles` | `members.manage` | R | sabit roller `[{key, name, description}]`: `manager` · `board` · `auditor` · `accounting` · `security` · `technical` |
| GET | `/sites/{slug}/members` | `members.manage` | R | personel (sakinler hariç), dizi: `{id, full_name, email, role_key, role_name, is_active, source: site\|organization, last_login_at, invited_at}`. `organization`: yönetim şirketinden gelen erişim |
| POST | `/sites/{slug}/members` | `members.manage` | R | `{full_name, email, role_key}` → `{member, temporary_password}` (201, `Cache-Control: no-store`). Yeni e-posta → yeni kullanıcı + geçici parola (yalnız bu yanıtta; ilk girişte değiştirme zorunlu). Kayıtlı e-posta → mevcut kullanıcıya rol, `temporary_password: null`. Zaten sitede 409 `already_member` |
| PATCH | `/sites/{slug}/members/{id}` | `members.manage` | R | `{role_key?, is_active?}`. Kapatma kullanıcının oturumlarını sonlandırır. 409: `derived_membership` (şirketten gelen erişim), `last_manager` (en az bir etkin yönetici kalır), `self_change` (kendi kaydı) |

### 2.17 Sakin kayıt başvurusu (servis isteği 13)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET · PATCH | `/sites/{slug}/registration-link` | `people.manage` | R | `{code, is_enabled}`; yoksa açılır. PATCH `{is_enabled}` kayda kapatır/açar |
| POST | `/sites/{slug}/registration-link/rotate` | `people.manage` | R | yeni kod; eskisi hemen geçersiz |
| GET | `/sites/{slug}/registrations?status=pending\|approved\|rejected` | `people.manage` | R | sayfalı, yeni üstte: `{id, reference: KB-0001, first_name, last_name, phone, email, unit_text, relation, explicit_consent, status, created_at, decided_at, decided_by, reject_reason, unit_name}` |
| POST | `/sites/{slug}/registrations/{id}/approve` | `people.manage` | R | `{unit_id, start_date}` → bölüme malik/kiracı eklenir (bölüme kişi ekleme kuralları; hisse doluysa 409 `owner_shares_exceed`). Telefon/e-postayla eşleşen kişi varsa yeni kişi açılmaz. E-posta varsa sakin giriş hesabı açılır: `temporary_password` yalnız bu yanıtta (`no-store`; SMS/e-posta K5'e kadar elden). İkinci karar 409 `already_decided` |
| POST | `/sites/{slug}/registrations/{id}/reject` | `people.manage` | R | `{reason}` (3–300) |
| GET | `/public/registration/{code}` | **herkese açık** | R | `{site_name, site_slug}` — başka site bilgisi yok. Kod geçersiz/kapalı 404. IP başına dakikada 30 |
| POST | `/public/registration/{code}` | **herkese açık** | R | `{first_name, last_name, phone, email?, unit_text, relation: owner\|tenant, explicit_consent, kvkk_ack}` → `{reference}` (201). Ad/soyad 2–40 harf, cep telefonu `+905…`, `kvkk_ack` zorunlu; hatalar 422 `fields`. Aynı telefonla bekleyen başvuru 409 `already_pending`. IP başına dakikada 5 → 429 |

### 2.18 Toplantı (servis isteği 14 — modül `general-assembly`)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/meetings?status=planned\|held\|cancelled` | `meetings.read` | R | sayfalı, tarihe göre yeniden eskiye; satır `agenda[]` ile |
| GET | `/sites/{slug}/meetings/{id}` | `meetings.read` | R | `{id, number, kind, title, scheduled_at, location, status, agenda[{id, order, title, result, decision, votes_for, votes_against, votes_abstain}], attendance_note, held_at, cancel_reason, created_by, created_at}` |
| POST | `/sites/{slug}/meetings` | `meetings.manage` | R | `{kind: general_ordinary\|general_extraordinary\|board, title, scheduled_at, location, agenda[1–30]}`; sıra sunucuda. Gündem sonradan değişmez (yanlışsa iptal + yeniden) |
| POST | `/sites/{slug}/meetings/{id}/decisions` | `meetings.manage` | R | `{attendance_note, items[{id, result, decision?, votes_*?}]}` — **tek seferlik**: her maddeye sonuç (`accepted` · `rejected` · `postponed` · `info`), `info` dışında karar metni zorunlu; hata 422 `fields.items.{sıra}`. Kayıtla `held`, sonra değişmez; ikinci kayıt 409 `not_planned` |
| POST | `/sites/{slug}/meetings/{id}/cancel` | `meetings.manage` | R | `{reason}` zorunlu; yalnız planlanan (409 `not_planned`) |

Yeter sayı (KMK m.30) hesaplanmaz; çağrı süresi (15 gün, m.29) yalnız ekranda uyarı. Sistemdeki
tutanak noter onaylı karar defterinin yerine geçmez.

### 2.19 Anket (servis isteği 15 — modül `surveys`)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/polls?status=open\|closed` | `announcements.read` (sakin hariç) | R | sayfalı, yeni üstte: `{id, question, description, options[{id, label, votes}], audience, ends_on, status, total_votes, created_by, created_at}` — yalnız toplamlar |
| POST | `/sites/{slug}/polls` | `polls.manage` | R | `{question, description?, options[2–8, tekrarsız], audience: all\|owners\|tenants, ends_on ≥ bugün}` |
| POST | `/sites/{slug}/polls/{id}/close` | `polls.manage` | R | erken kapatır; kapalıysa 409 `already_closed` |
| GET | `/sites/{slug}/resident/polls` | sakin | R | açık + son 30 günde kapananlar, dizi; ek `my_votes[{unit_id, unit_name, option_id}]` (oy verebileceği bölümler). Oy vermeden ve anket açıkken `votes`/`total_votes` **null** |
| POST | `/sites/{slug}/resident/polls/{id}/vote` | sakin | R | `{unit_id, option_id}` → `{option_id}`. **Bir bölüm = bir oy** (benzersiz kısıt; eşzamanlıda da). Kapalı 409 `poll_closed`, bölüm adına oy var 409 `already_voted`, bölüm sakinin değil 404, rol hedef kitle dışında 403 `not_eligible` |

`ends_on` günü dahil açık. `audience`: `all` bölümdeki malik/kiracı/oturan (ilk veren), `owners`
malik, `tenants` kiracı ya da oturan; vekil oy vermez. **Gizli oy**: kimin neye oy verdiği hiçbir
uçta yok, oy tablosu denetim kaydına yazılmaz. Sonuç danışma niteliğindedir.

### 2.20 Sözleşme, demirbaş ve stok, personel (servis istekleri 16–18)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/contracts?archived=false` | `expenses.read` | R | dizi, `end_date` artan. Sunucuda `days_left` ve `state`: arşivse `archived`, `days_left < 0` `expired`, `≤ notice_days` `expiring`, aksi `active` (sitenin bugünü, İstanbul) |
| POST · PATCH | `/sites/{slug}/contracts` · `/{id}` | `contracts.manage` | R | `{vendor, subject, category, start_date, end_date ≥ start_date, amount? (para metni), period?: monthly\|yearly\|once, notice_days 0–365, auto_renew, note?}`; PATCH kısmi, `{is_archived}` arşivler/geri alır. Silme yok; **gider yazmaz** |
| GET | `/sites/{slug}/assets?status=in_use\|broken\|retired` | `inventory.read` | R | dizi, koda göre |
| POST · PATCH | `/sites/{slug}/assets` · `/{id}` | `inventory.manage` | R | `{name, category?, location?, acquired_on?, value?, status, assignee?, note?}`. `code` (`DB-0001`) site içinde sıralı, sunucu verir, değişmez. Silme yok: `retired` |
| GET · POST | `/sites/{slug}/stock-items` | `inventory.read` · `inventory.manage` | R | `{name (site içinde tekil, harf duyarsız), unit_label, min_quantity, location?}` → `{…, quantity, is_low}`. Miktarlar **ondalık metin** (`"4.5"`, en çok 3 hane) |
| GET | `/sites/{slug}/stock-items/{id}/moves?limit=10` | `inventory.read` | R | son N hareket (en çok 100), yeniden eskiye: `{id, item_id, direction, quantity, note, moved_at, moved_by}` |
| POST | `/sites/{slug}/stock-items/{id}/moves` | `inventory.manage` | R | `{direction: in\|out, quantity > 0, note?}` → `{item, move}` (201). Çıkış mevcuttan fazlaysa 409 `insufficient_stock` (satır kilidi; eşzamanlıda da). Hareket değişmez; yanlışsa ters hareket |
| GET | `/sites/{slug}/staff?active=true\|false` | `people.read` | R | modül `staff`; dizi, ada göre; `is_active`: ayrılışı yok ya da bugün/ileride |
| POST · PATCH | `/sites/{slug}/staff` · `/{id}` | `staff.manage` | R | `{full_name (3–60 harf), position, employer: site\|contractor, contractor_name (taşeronda zorunlu), phone? (+905…), start_date, end_date?, shift?}`; PATCH kısmi, `{end_date}` ayrılış. T.C. kimlik, maaş, adres, sağlık **tutulmaz**; personel kaydı hesap açmaz |

---

## 3. Sıralama önerisi

Frontend bu sırayla istek açacak:
1. `auth/login`, `me` — her ekran buna bağlı
2. `sites/{slug}`, `dashboard`
3. `units`, `debtors`, `accounts/{id}/statement`
4. `charge-runs/preview`, `charge-runs`, `payments`
5. `expenses`, `cash-accounts`, `reports`
6. `requests`, `announcements`, güvenlik
7. `platform/*`
8. `resident/*`
