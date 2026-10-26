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
    "fields": { "amount": "Tutar sıfırdan büyük olmalı." },
    "request_id": "0f6c2e1a9b7d4c3e8a5f1b2c3d4e5f60"
  }
}
```
- `code`: makine için, İngilizce `snake_case`, sabit.
- `message`: **Türkçe**, kullanıcıya olduğu gibi gösterilebilir, ne yapması gerektiğini söyler.
- `fields`: yalnız doğrulama hatalarında; alan adı → Türkçe mesaj. Frontend bunu ilgili alanın
  altında gösterir.
- `request_id`: isteğin kimliği — yanıttaki `X-Request-ID` başlığıyla aynı (istemci kendi
  kimliğini `X-Request-ID` ile gönderirse o kullanılır). Hata ekranında "destek için kod" olarak
  gösterilir; destek bu kimlikle sunucu logunu ve denetim kaydını bulur (Yazılım Dokümanı §4.3
  "korelasyon kimliği").

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
| GET | `/me/sessions` | giriş | R | açık oturumlarım, son kullanılan üstte: `[{id, device ("Chrome · Windows"), user_agent, ip, created_at, last_used_at, expires_at, is_current}]` (Yazılım Dokümanı PLT-10) |
| DELETE | `/me/sessions/{id}` | giriş | R | bir cihazı kapatır → 204; o cihazın jetonu bir sonraki istekte 401. Başkasının ya da kapalı oturum 404 |
| DELETE | `/me/sessions?keep_current=true` | giriş | R | "tüm cihazlardan çık" → `{revoked, current_revoked}`; `keep_current=false` bu oturumu da kapatır (frontend girişe döner) |
| GET | `/me/security-events` | giriş | R | giriş geçmişim, son 50: `[{at, kind, label, device, ip}]`; `kind`: `login_succeeded` · `login_failed` · `account_locked` · `logout` · `password_changed` · `session_revoked` · `token_reused` |

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
| POST | `/sites/{slug}/bank-imports` | `finance.payment.record` + `finance.cash.manage` | R | **banka hareketi aktarımı — önizleme**, multipart `file` (.xlsx/.xls/.csv, ≤ 5 MB) + `cash_account_id` (banka türü). Hiçbir tahsilat yazılmaz; dosya diske yazılmaz, satırlar 6 saat saklanır. Başlık ilk 30 satırda aranır (Tarih, Açıklama, Tutar ya da Borç/Alacak, Dekont No). Satır `status`: `matched` (açıklamada referans kodu) · `suggested` (gönderen adı tek hesapla ya da aynı kişinin tek borçlu hesabıyla eşleşiyor) · `unmatched` (satırda `reason`: birden çok referans kodu · ad birden çok hesapla eşleşiyor · referans kodu ya da kayıtlı ad yok) · `ignored` (çıkış) · `duplicate` (aynı hesaba daha önce aktarıldı: tarih + tutar + dekont no, yoksa açıklama). Sütun tanınmazsa 422 `fields.file` hangi sütun olduğunu söyler |
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

**Kapılar, ön davet, sakin onayı, plaka, nöbet defteri, devriye** (servis istekleri 40–46,
Yazılım Dokümanı GUV-02..09, OTP-02; 25.10.2026). Modül `visitors` (kargo yetkilisi ve uzun
bekleyen kargo `packages`); kapalıysa 404. Bildirimler uygulama içi (dış kanal K5).

| Yöntem | Yol | İzin | Not |
|---|---|---|---|
| GET | `/sites/{slug}/gates` | `modules.manage` ya da `security.*` | küçük liste (sayfasız) `[{id, name, kind: vehicle\|pedestrian\|service, block_ids, always_open, is_active}]` |
| POST · PATCH | `/sites/{slug}/gates` · `/gates/{id}` | `modules.manage` | ad harf duyarsız tekil (422 `fields.name`); kapı silinmez, pasifleşir |
| GET · PUT | `/sites/{slug}/settings/security` | okuma `security.*`, yazma `modules.manage` | `{unannounced_method: info\|approval, approval_timeout_seconds 30–600, gate_methods {gate_id: yöntem}, parcel_method code\|qr\|both, visitor_fields {plate, phone, id_seen}, retention_months 1–24, auto_anonymize, parcel_long_wait_days 1–60, guest_parking_capacity 0–500, version, updated_at, updated_by}`. 24 ay üstü 422 `retention_limit`. Kapalı ziyaretçi alanı kaydedilmez. Her kayıt `version` artırır (geçmiş denetim kaydında) |
| GET · POST | `/sites/{slug}/resident/visitor-invites` | sakin (kişiye bağlı üyelik) | `{unit_id?, visitor_name, type: once\|range\|recurring, date_from, date_to, weekdays (0=pzt), time_from, time_to, plate, note}`. **Kod (6 hane) yalnız oluşturma yanıtında**; sunucuda HMAC özeti. Plakalı davet misafir parkına sayılır: dolu 409 `guest_parking_full` |
| POST | `…/resident/visitor-invites/{id}/cancel` · `/new-code` | sakin | iptal (etkin değilse 409 `invalid_status`); yeni kod (eski kod hemen geçersiz) |
| GET | `/sites/{slug}/visitor-invites/expected` | `security.visitors` | bugün geçerli davetler: ad, bölüm, tip, saat, plaka (kod ve not yok) |
| POST | `/sites/{slug}/visitor-invites/check-in` | `security.visitors` | `{code}` → ziyaretçi kaydı `entered`, davet sahibine bildirim; yok 404 `invite_not_found`, kullanılmış/geçerlilik dışı 409 `invite_not_valid`. Kullanıcı başına 5 dakikada 30 deneme (429) |
| POST | `/sites/{slug}/visit-requests` | `security.visitors` | `{unit_id, full_name, kind, plate, gate_id}` — yöntem: kapı istisnası → site ayarı. `info`: hemen `allowed` + ziyaretçi kaydı + sakine bilgi; `approval`: `awaiting`, `expires_at = şimdi + süre`, sakine onay isteği |
| GET | `/sites/{slug}/visit-requests?status=` | `security.visitors` | bugünkü (tesis saati) ve bekleyen talepler; `timed_out`, `seconds_left` |
| POST | `/sites/{slug}/visit-requests/{id}/decide` | `security.visitors` | `{decision: allow\|deny, reason}` yalnız süre dolunca (409 `awaiting_resident`); gerekçe zorunlu (422 `fields.reason`) |
| GET · POST | `/sites/{slug}/resident/visit-approvals` · `/{id}` | sakin | yalnız kendi bölümünün talepleri; `{decision}` süre içinde (dolunca 409 `timed_out`), verilmişse 409 `already_decided`. Kararlar denetim kaydında (ad/plaka olmadan) |
| GET | `/sites/{slug}/plates/lookup?plate=` | `security.visitors` ya da `units.read` | `{plate, plate_display, result: registered\|guest\|unknown, unit_name, owner_name, valid_until, detail}`; kaynak: geçerli ya da bugün kullanılmış davet plakası, bugün içerideki ziyaretçi. **Kayıtlı araç P02 araç tablosu gelince** bağlanır. Hız sınırlı (5 dakikada 120) |
| GET · POST | `/sites/{slug}/gate-logs?date=` | okuma: aynı; yazma `security.visitors` | sayfalı; `{plate, direction: in\|out, gate_id}` — sorgu sonucu kayda dondurulur |
| GET · POST | `/sites/{slug}/parking-violations` | okuma: aynı; yazma `security.visitors` | sayfalı; `{plate, location, note}` — yetkili plakada not zorunlu (409 `plate_authorized`); bölüme bağlıysa o bölümün sakinlerine, değilse yönetime bildirim (`notified`) |
| GET · POST | `/sites/{slug}/resident/parcel-delegates` · `/{id}/remove` | sakin (modül `packages`) | `{name, unit_id?}`; bölüm başına en fazla 3 (409 `delegate_limit`) |
| GET | `/sites/{slug}/units/{id}/parcel-delegates` | `security.packages` | yalnız `[{id, name}]` |
| GET | `/sites/{slug}/packages/long-waiting` | `security.packages` | `{days, items: [{id, unit_id, unit_name, carrier, received_at, waiting_days, delegates}]}` (en eski üstte, en fazla 200) |
| GET · POST | `/sites/{slug}/shift-log?date=` | `security.incidents` | nöbet defteri (gün, tesis saati); `{kind: note\|event\|handover, text ≥ 5, gate_id}`; yanıtta `by_me`. **Değişmez** (tetikleyici) |
| POST | `…/shift-log/{id}/addenda` · `/acknowledge` | `security.incidents` | ek not; devri yazan onaylayamaz (409 `same_person`), onay bir kez (409 `already_acknowledged`) |
| GET · POST · PATCH | `/sites/{slug}/patrol-points` · `/{id}` | okuma `modules.manage` ya da `security.incidents`; yazma `modules.manage` | `{name, location, area, tag: qr\|nfc\|both, is_active}`; `code` 6 hane, sunucu üretir (QR: `SY-DEVRIYE:{kod}`) |
| GET · POST · PATCH | `/sites/{slug}/patrol-plans` · `/{id}` | aynı | `{name, point_ids (sıralı), ordered, start_times ["22:00"], allowed_minutes 5–240, assignee, is_active}` |
| GET | `/sites/{slug}/patrol-schedule?date=` | `security.incidents` | `[{plan_id, plan_name, slot, status: upcoming\|due\|not_started\|running\|overdue\|done\|done_with_deviation, run_id, report}]` — 15 dakikada başlamayan `not_started` |
| GET · POST | `/sites/{slug}/patrol-runs?date=` | `security.incidents` | başlat `{plan_id, slot?}` — açık yürütme varsa 409 `run_open`; plan dondurulur |
| POST | `…/patrol-runs/{id}/scans` · `/finish` | `security.incidents` | `{code, note}`: kod yok 422 `fields.code`, planda yok 409 `not_in_plan`, tekrar 409 `already_scanned`, sıralı turda beklenmeyen nokta `out_of_order` (kabul edilir). Rapor `{missed, out_of_order, duration_minutes, overtime, late_start_minutes, total_points, scanned_points, ok}`; bitişte yöneticiye özet |

**Otopark — park yeri ve tahsis** (PM kararı K30, OTP-01; modül kapısı yok — yapı verisi):

| Yöntem | Yol | İzin | Not |
|---|---|---|---|
| GET | `/sites/{slug}/parking-spaces?kind=&q=&unit_id=` | `units.read` ya da `security.visitors` | sayfalı; `{id, code, kind: standard\|disabled\|ev\|guest, location, is_paid, is_active, note, current}` (`current`: bugünkü tahsis ya da `null`) |
| POST · PATCH | `/sites/{slug}/parking-spaces` · `/{id}` | `units.manage` | kod site içinde tekil (büyük harf) |
| POST | `/sites/{slug}/parking-spaces/{id}/allocations` | `units.manage` | `{unit_id, start_date, end_date?, note}` — tarih çakışırsa 409 `space_taken` |
| GET | `/sites/{slug}/parking-allocations?space_id=&unit_id=` | okuma | sayfalı tahsis geçmişi |
| POST | `/sites/{slug}/parking-allocations/{id}/end` | `units.manage` | `{end_date}`; bitmişse 409 `allocation_ended` |
| GET | `/sites/{slug}/resident/parking-spaces` | sakin | "Park yerim": bugünkü tahsisler |

Zamanlanmış işler: `run-security-daily` (günde bir: uzun bekleyen kargo hatırlatması ve yönetime
rapor; `auto_anonymize` açık sitelerde saklama süresi dolan ziyaretçi/plaka verisini anonimleştirir
— **varsayılan kapalı**, K8) · `run-patrol-checks` (5 dakikada bir: başlamayan / süresi aşan tur).


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
| GET | `/sites/{slug}/audit?entity=&entity_id=&action=&user_id=&from=&to=` | `audit.read` | R | sayfalı, yeni üstte. Satır: `{id, at, user_id, actor_name, actor_type, action, entity, entity_id, before, after, ip, user_agent, request_id, reason}`. `action`: `create` · `update` · `delete` · `import` · `download` · `export`. `actor_type` (süzgeç de): `user` · `system` · `delegate`. Telefon, e-posta, IBAN, vergi no `before`/`after` içinde maskelidir. Bölüm, kişi, bölüm–kişi ilişkisi de denetlenir (bir kaydın "Geçmiş" sekmesi: `?entity=units&entity_id=…`). `entity` tablo adıdır (`payments`, `expenses`, `budget_items`, `site_modules` …). Güncellemede `before`/`after` yalnız değişen alanları taşır. `from`/`to` iş günü (İstanbul). `user_id` null: sistem (demo, gece işi) |

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
| POST | `/sites/{slug}/registrations/{id}/approve` | `people.manage` | R | `{unit_id, start_date, share_percent?}` → bölüme malik/kiracı eklenir (bölüme kişi ekleme kuralları). `share_percent` yalnız malik başvurusunda (0 < x ≤ 100, boşsa %100; kiracıda 422 `share_only_for_owner`); mevcut maliklerle toplam %100'ü aşarsa 409 `owner_shares_exceed`. Telefon/e-postayla eşleşen kişi varsa yeni kişi açılmaz. E-posta varsa sakin giriş hesabı açılır: `temporary_password` yalnız bu yanıtta (`no-store`; SMS/e-posta K5'e kadar elden). İkinci karar 409 `already_decided` |
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
| — | personel kartı ek alanları (istek 35, K29) | | R | `payroll_no?` (harf, rakam, tire; ≤ 20; büyük harfe çevrilir; site içinde tekil → 409 `payroll_no_taken`), `user_id?` (bağlı giriş hesabı; sakin olmayan site üyesi, tek karta → 409 `user_taken`). Yanıtta + `user_name`, `access_closed_at`. Ayrılış tarihi **geçince** bağlı hesabın site üyeliği pasifleşir (gece `run-staff-jobs`; erişim çözümlemesi de ertesi gün düşürür); kartı yeniden açmak erişimi açmaz |
| GET | `/sites/{slug}/staff/linkable-users` | `staff.manage` | R | karta bağlanabilecek hesaplar `[{user_id, full_name, email, role_name, staff_id?}]` |
| GET | `/sites/{slug}/staff/{id}/open-items?after=` | `staff.manage` | R | `{future_shifts, first_shift, pending_leaves}` — ayrılışta bilgi; kayıt engellenmez |

### 2.20a İnsan kaynakları (servis istekleri 36–39, Yazılım Dokümanı IK-02..05, K29)
Modül `staff`. Saatler `"SS:DD"`, saat toplamları **metin** (`"6.67"`), dakikalar tam sayı.

| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET · POST · PATCH | `/sites/{slug}/shift-templates` · `/{id}` | `people.read` · `staff.manage` | R | `{name (tekil), start, end (≤ start → ertesi gün), break_minutes 0–180, position?, location?, is_active}` → + `overnight`. Silme yok, kapatılır |
| GET | `/sites/{slug}/shift-plans?from=&to=` | `people.read` | R | `{from, to, staff, templates, assignments, leaves[{staff_id, from, to, type}], conflicts[{assignment_id, kind: overlap\|leave\|rest, message}], published?, changed_after_publish, final_days}`; en çok 42 gün. İzin `type` yalnız `staff.manage`'e (raporlu izin) |
| POST · DELETE | `/sites/{slug}/shift-plans/assignments` · `/{id}` | `staff.manage` | R | `{staff_id, date, template_id}` → + `conflicts` (çakışma engellemez). 409 `duplicate`, `timesheet_final`, `has_attendance` |
| POST | `/sites/{slug}/shift-plans/copy-week` | `staff.manage` | R | `{from_week, to_week}` (pazartesi) → `{created}`; var olanı korur |
| POST | `/sites/{slug}/shift-plans/publish` | `staff.manage` | R | `{week, acknowledge_conflicts}`; 409 `conflicts`, `empty_plan`. Bağlı hesabı olan personele uygulama içi bildirim (yayın ve yayın sonrası değişiklik) |
| GET · POST | `/sites/{slug}/leave-requests?status=&staff_id=` | `staff.manage` | R | sayfalı. `{staff_id, type: annual\|excuse\|sick\|unpaid, from, to, note?}` → `{…, days (takvim), status, source, created_by, decided_by, reject_reason, clashing_shifts, approval_request_id, approval_number}`. 409 `leave_overlap`, `timesheet_final`; raporluda `note` 422 (tanı tutulmaz) |
| POST | `/sites/{slug}/leave-requests/{id}/approve` · `/reject` | `staff.manage` ya da onaycı | R | "İzin talebi" onay kuralı varsa karar **onay mekanizmasına** yazılır (403 `not_an_approver`); yoksa `staff.manage`. Kendi izni 403 `own_request`. Ret `{reason}` zorunlu |
| POST | `/sites/{slug}/leave-requests/{id}/cancel` | `staff.manage` | R | onaylı izni iptal (kesin gün varsa 409 `timesheet_final`); bekleyen + onay talebi → ONY'de geri çekme |
| GET | `/sites/{slug}/leave-balances?year=` | `staff.manage` | R | `[{staff_id, staff_name, used: {annual, excuse, sick, unpaid}}]` — onaylı gün, bilgi amaçlı |
| GET | `/sites/{slug}/timesheets?date=` | `staff.manage` | R | `{date, is_final, finalized_by, finalized_at, attendance_method, rows}`; satır: atama `id` (izinli+vardiyasız satırda null), `planned_minutes`, `night_minutes`, `status: worked\|absent\|leave`, `leave_type`, geç/erken/fazla mesai dakikası, `worked_minutes`, `reason`, `fixed_by`, `is_exception`, `flags`. Canlı hesap; kesin gün donmuş satırlardan |
| PATCH | `/sites/{slug}/timesheets/{assignment_id}` | `staff.manage` | R | `{status: worked\|absent, late_minutes, early_leave_minutes, overtime_minutes (0–960), reason (zorunlu)}`; denetim kaydında gerekçeyle. 409 `timesheet_final`, `day_not_started` |
| POST | `/sites/{slug}/timesheets/finalize` | `staff.manage` | R | `{date}` → satırlar donar (değişmez). 409 `day_not_ended`, `already_final` |
| GET | `/sites/{slug}/timesheet-exports/preview?month=` | `staff.manage` · `finance.read` | R | `{month, rows, missing_days, missing_payroll, exported?}`; satır saatleri metin |
| GET · POST | `/sites/{slug}/timesheet-exports` | okuma `staff.manage` · `finance.read`; yazma `staff.manage` | R | sayfalı geçmiş `{id, month, staff_count, created_by, created_at, file_name}`. POST `{month}`: 409 `month_not_ended`, `days_not_final`, `payroll_missing`, `already_exported` (ay kilitli) |
| GET | `/sites/{slug}/timesheet-exports/{id}/file` | okuma | R | CSV (`;`, UTF-8 BOM, ondalık virgül). Mod K (Kivi) A-01 sonrası |
| GET · PUT | `/sites/{slug}/attendance-settings` | `staff.manage` | R | `{method: none\|qr\|location, latitude?, longitude?, radius_m 20–5000, late/early_tolerance_minutes 0–120}` → + `qr_code`, `qr_rotated_at` |
| POST | `/sites/{slug}/attendance-settings/rotate-code` | `staff.manage` | R | kulübe kodunu yeniler |
| GET | `/sites/{slug}/me/shifts?from=&to=` | kartı bağlı personel (K29) | R | Vardiyam: yalnız yayınlanmış haftalar + giriş-çıkış kayıtları |
| POST | `/sites/{slug}/me/attendance-events` | kartı bağlı personel | R | `{kind: in\|out, assignment_id?, occurred_at? (çevrimdışı), code? (QR), latitude?, longitude?}` → `{…, verified, clock_skew_minutes}`. Doğrulanamayan kayıt reddedilmez, istisna olur; konum saklanmaz |
| GET · POST | `/sites/{slug}/me/leave-requests` · `/{id}/cancel` | kartı bağlı personel | R | kendi izin taleplerim; geri çekme yalnız bekleyende |

Kart bağlı değilse `/me/*` 403. Puantaj canlıdır: belgedeki "gece 01:00 puantaj üretimi" işinin
yerine her an hazır; gün kesinleşince `timesheet_lines` donar.

### 2.21 Bildirim merkezi (Yazılım Dokümanı PLT-05)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/notifications?unread=&category=` | site üyesi (sakin dahil) | R | yalnız **kendi** bildirimlerim, son 90 gün, sayfalı, yeni üstte: `{id, category, kind, title, body, link_entity, link_id, created_at, read_at}` + `unread_count`. `category`: `urgent` · `financial` · `security` · `operations` · `governance` · `announcement`. `kind` olay türü (`approval.requested` …) — ikon seçimi; `link_entity` + `link_id` tıklayınca gidilecek kayıt |
| GET | `/sites/{slug}/notifications/unread-count` | site üyesi | R | sayı (zil rozeti) |
| POST | `/sites/{slug}/notifications/read` | site üyesi | R | `{ids?: [..]}` → `{marked, unread_count}`; `ids` yoksa okunmamışların hepsi. Başkasının kimliği etkisiz (0) |
| GET | `/me/notifications/summary` | giriş | R | erişebildiğim her site için `{site_id, slug, unread_count}` (site seçici rozeti) |
| GET · PUT | `/me/notification-preferences` | giriş | R | kategori × kanal (`push` · `sms` · `email`) matrisi: `{in_app: true, delivery_enabled: false, categories: [{category, label, rule, channels: [{channel, enabled, locked}]}]}`. PUT `{changes: [{category, channel, enabled}]}`. Acil ve yönetişimin varsayılan kanalları kapatılamaz (`locked`), finansal ve güvenlikte en az bir kanal açık kalır; aksi 422 `preference_not_allowed` |
| POST | `/me/push-tokens` | giriş | R | `{token, platform: ios\|android\|web}` → 204; tekrar güvenli; aynı jeton başka hesapla gelirse ona taşınır |
| POST | `/me/push-tokens/remove` | giriş | R | `{token}` → 204 (çıkışta); başkasınınki 404 |

Uygulama içi bildirim **her zaman** yazılır. Push/SMS/e-posta gönderimi açık karar **K5**'e kadar
yok (`delivery_enabled: false`); tercihler ve jetonlar şimdiden saklanır. Bildirimi modüller
`services/notifications.notify()` ile, olayı üreten kayıtla aynı transaction'da yazar; aynı
olay aynı kişiye ikinci kez yazılmaz (tekil anahtar). İlk üretici: onay mekanizması (§2.22).

### 2.22 Onay mekanizması (Yazılım Dokümanı Bölüm 6, ONY-01..03)
Tüm modüllerin onayları tek servisten geçer (`services/approvals.request_approval`); modül komutu
"onay bekliyor" bırakır, onay tamamlanınca kayıtlı yürütücü aynı transaction'da çalışır. **Şu an
bağlı işlemler "onay kuralı değişikliği" ve "izin talebi"** (IK-03; ret/geri çekmede modülün
kapatıcısı çalışır — `register_closer`); kayıt başvurusu, iade, tahakkuk, rol değişikliği
henüz bağlı değil (K21). Diğer işlem tipleri için kural şimdiden tanımlanabilir.

| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/approval-action-types` | `approvals.rules.read` | R | `[{key, label, connected}]` — `connected: true` olan tip şu an onaydan geçiyor |
| GET | `/sites/{slug}/approval-rules?archived=false` | `approvals.rules.read` | R | dizi: `{id, action_type, action_label, is_active, is_template ("Önerilen"), applicable (adım rolünde aktif kullanıcı var mı), in_force_version, current: {version, effective_from, amount_min, amount_max, source, mode, reject_behavior, remind_after_days, escalate_after_days, day_basis, escalation_role, basis_note, created_by, created_at, steps: [{roles, quorum, count, eligible (N), required (M)}]}}` |
| POST · PUT | `/sites/{slug}/approval-rules` · `/{id}` | `approvals.rules.manage` | R | `{action_type, steps: [{roles: [rol adı], quorum: any\|all\|majority\|count, count?}], mode: sequential\|parallel, reject_behavior: single_rejects\|counts, amount_min? (hariç), amount_max? (dahil), source?, remind_after_days 1–30, escalate_after_days > remind, day_basis: calendar\|business, escalation_role?, effective_from ≥ bugün, basis_note?}` → `{status: saved\|approval_pending, rule, approval_request_id, warnings}`. PUT yeni **versiyon** yazar; bekleyen talepler eski versiyonla sürer. Aynı tipte tutar aralığı çakışırsa 422 `fields.amount_min`; boşluk uyarı (`warnings`). Adım rolünde aktif kullanıcı yoksa ya da `count > N` ise 422 `fields.steps.{n}`. "Onay kuralı değişikliği" için kural varsa **202** `approval_pending` |
| POST | `/sites/{slug}/approval-rules/{id}/archive` | `approvals.rules.manage` | R | kural yeni taleplere uygulanmaz (ya da 202) |
| POST | `/sites/{slug}/approval-rules/restore-defaults` | `approvals.rules.manage` | R | dokümanda adımları tanımlı "Önerilen" şablonları kurar (aidat kuralı, tahakkuk, iade, satın alma ×3, kayıt başvurusu); kuralı olan tipe dokunmaz; rolde kullanıcı yoksa `skipped` |
| POST | `/sites/{slug}/approval-rules/simulate` | `approvals.rules.read` | R | `{action_type, amount?, source?}` → uygulanacak kural ve adımların onaycı adları; `rule_id: null` → varsayılan (site yöneticisi 1/1) |
| GET | `/sites/{slug}/approvals?tab=pending\|mine\|history` | personel (sakin 403) | R | sayfalı: `{id, number, action_type, action_label, title, amount, status, requested_by, created_at, progress ("2/3 onay"), post_approval, escalated, remind_on, escalate_on}`. `pending`: kararımı (ya da vekâleten) bekleyenler, "sonradan onay" en üstte |
| GET | `/sites/{slug}/approvals/{id}` | talep eden · onaycı · vekili · `approvals.rules.manage` | R | + `summary`, `revision`, `failure_reason`, `can_decide`, `acting_for` (vekâleten kimin yerine), `can_withdraw`, `can_resubmit`, `steps: [{index, roles, required, eligible, approvals, rejections, status: waiting\|open\|approved\|rejected, quorum_unreachable, approvers: [{name, role, source: rule\|escalation, decision, decided_by, comment, decided_at}]}]`, `history` (tüm sürümlerin kararları, `on_behalf_of`), `rule_change` (yalnız `approval_rule_change`: `{op: create\|update\|archive, rule_id, rule}` — `rule` önerilen kuralın son sürümü, `RuleIn` biçiminde; yeniden gönderme formu bununla dolar, arşivlemede `null`; diğer tiplerde alan `null`). Başkası 404 |
| POST | `/sites/{slug}/approvals/{id}/approve` | onaycı | R | `{comment?}` |
| POST | `/sites/{slug}/approvals/{id}/reject` · `/request-revision` | onaycı | R | `{comment}` zorunlu (422 `fields.comment`) |
| POST | `/sites/{slug}/approvals/{id}/withdraw` | talep eden | R | bekleyen ya da revizyondaki talep |
| POST | `/sites/{slug}/approvals/{id}/resubmit` | talep eden | R | revizyondan sonra: `{title?, amount?, rule? (kural değişikliğinde yeni öneri)}` → yeni sürüm, zincir baştan |
| GET · POST | `/sites/{slug}/delegations` | personel | R | devirlerim `[{…, state: scheduled\|active\|ended, direction: given\|received}]`; POST `{to_user_id, roles: [kendi rolüm], starts_at, ends_at, note?}` |
| GET | `/sites/{slug}/delegations/candidates` | personel | R | devredebileceğim kişiler (aynı rol) |
| POST | `/sites/{slug}/delegations/{id}/end` | devreden | R | erken sonlandırma |

`status`: `pending` · `revision_requested` · `approved` · `executed` · `execution_failed`
(onaylandı – yürütülemedi) · `rejected` · `withdrawn`. 409: `not_pending`, `delegation_overlaps`,
`rule_archived`; 403: `not_an_approver`, `own_request`, `role_changed`, `not_requester`.

Kurallar: talep eden onaycı listesinden çıkar (3/5 → 3/4); aynı kişi bir talepte bir kez karar
verir; "tek ret reddeder" ya da "ret hesaba sayılır"; hatırlatma X, eskalasyon Y gün sonra
(saatlik iş `run-approval-jobs`) — **otomatik onay/ret yok**; eskalasyon hedefi seçilmemişse
yalnız hatırlatma (K20). Yetki devri yalnız **aynı role** (K20); devralan, devredenin kendi
talebini onaylayamaz; kararlar "vekâleten" ve denetimde `actor_type: delegate`.

### 2.23 Belge arşivi (Yazılım Dokümanı RAP-03, 18.2 — servis isteği 78, modül `documents`)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/documents?q=&kind=&expiring=1` | `documents.read` | R | sayfalı, son yüklenen üstte: `{id, title, kind, kind_label, scope: site\|block\|unit, scope_id, scope_name, confidentiality: public\|unit\|management\|secret, valid_until, days_left, warning: "60"\|"30"\|"expired"\|null, retention_years, version, file_name, content_type, size, sha256, uploaded_by, updated_at, versions: [{v, file_name, content_type, size, sha256, uploaded_by, at}]}`. `expiring`: 60 gün içinde biten ya da bitmiş. Gizli belge yalnız `documents.confidential` sahibine listelenir |
| POST | `/sites/{slug}/documents` | `documents.manage` | R | **multipart**: `title (3–120), kind, scope, scope_id? (blok/bölüm), confidentiality, valid_until?, retention_years? (1–100), file` → 201 `Written`. Aynı ad (Türkçe harf duyarsız, boşluklar tekleştirilir) + aynı kapsam → **yeni sürüm** (eski sürümler korunur; boş bırakılan bitiş/saklama korunur). Son sürümle aynı SHA-256 → 409 `same_file`. 422 `fields.title/kind/scope/confidentiality/valid_until/retention_years/file`; `unit` gizliliği bölüm kapsamı ister. Gizli belgeyi yalnız `documents.confidential` sahibi yükler |
| GET | `/sites/{slug}/documents/{id}/versions/{v}/download` | `documents.read` | R | dosya (`inline`, temiz ad). Gizli belge: yetkisize **404**; her açılış `document_access_logs`'a. Her indirme denetim kaydında (`download`) |
| GET | `/sites/{slug}/documents/{id}/access-log` | `documents.read` (gizlide `documents.confidential`) | R | sayfalı, yeni üstte: `{by, at, v}` — yalnız gizli belgenin açılışları |
| GET | `/sites/{slug}/resident/documents` | sakin | R | sayfalı: sakinlere açık belgeler (tüm site; blok kapsamlıysa o bloktaki, bölüm kapsamlıysa o bölümün sakinleri) + bölümüne özel belgeler. Yükleyen, özet ve sürüm geçmişi yok |
| GET | `/sites/{slug}/resident/documents/{id}/download` | sakin | R | son sürüm; göremediği belge 404 |

Dosya türü **içerikten** belirlenir (PDF, PNG, JPEG, WebP imzası; uzantı yanlışsa ad düzeltilir),
en fazla 10 MB, web kökü dışında `stored_files` ile saklanır, SHA-256 tutulur. **Virüs taraması
yok** (ilk sürüm) — hiçbir yanıt "tarandı" demez. Sürüm ve açılış kaydı değişmez; belge silinmez.
Saklama süresi (K8) bilgi amaçlıdır, otomatik silme yapılmaz. Geçerlilik uyarısı günlük iş
`run-document-expiry`: 60 ve 30 gün kala ve bitince belge yöneticilerine (`documents.manage`;
gizlide ayrıca `documents.confidential`) uygulama içi bildirim (`operations`, `document.expiring`
· `document.expired`, `link_entity: documents`); aynı uyarı bir kez. Belgeyi başka kayda bağlamak
için şemada `subject_type/subject_id` var (bugün uçta yok; modüller servis üzerinden bağlar).
"Okudum" onayı (ILT-01, istek 71) sonraki adım.

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
