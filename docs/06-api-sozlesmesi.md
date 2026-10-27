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
| POST | `/auth/login` | — | R | `{email, password}` → `{access_token, token_type: "bearer", expires_in, must_change_password, mfa_required, mfa_setup_deadline}` + `sy_refresh` çerezi. **2FA kuruluysa** `mfa_required: true`: oturum beklemede (10 dk), kod `POST /auth/mfa/verify` ile girilene dek her uç 401 `mfa_required`. Yönetici rolünde 2FA kurulmamışsa `mfa_setup_deadline` (geçiş süresi, `MFA_GRACE_DAYS`); süre dolunca `/me`, `/me/security`, `/auth/mfa/setup|enable` dışında 403 `mfa_setup_required`. Hatalı: 401 `invalid_credentials`; 5 hatada 15 dk kilit: 429 `account_locked`; aynı adresten 15 dk'da 20 hatalı deneme: 429 `too_many_attempts` (+ `Retry-After`, parola denenmez) |
| POST | `/auth/change-password` | giriş | R | `{current_password, new_password}` → 204. Geçici parolada (`must_change_password: true`) zorunlu; o zamana kadar `/me` dışındaki uçlar 403 `password_change_required`. Hatalar 422: `invalid_current_password` (hesap kilidi sayacını artırır), `weak_password` (en az 8), `password_unchanged`. Başarılıysa **diğer oturumlar iptal**, bu oturum sürer |
| POST | `/auth/refresh` | çerez | Y | gövde yok; çerezle → yeni `access_token` + yeni çerez. Geçersiz/süresi dolmuş: 401 `session_expired`. Frontend `credentials: "include"` ile çağırır |
| POST | `/auth/logout` | — | R | Bearer ve/veya çerez; oturumu iptal eder, çerezi siler. Her zaman 204 |
| GET | `/me` | giriş | R | `UserAccess` — `05-yetki.md` §4 + `must_change_password`. Site başına `role` (ana rol), `roles[]` (`{key, name, level, scope_kind, block_id, ends_at}`), `permissions` (birleşim) ve `scoped_permissions` (blokla sınırlı izinler). `GET /sites/{slug}` aynı alanları taşır. Frontend yönlendirmesi buna bakar (geçici paroladayken de çalışır) Ayrıca (P5) `email`, `phone`, `mfa_enabled`, `mfa_required`, `mfa_setup_deadline`, `mfa_setup_required`; 2FA kurulumu beklerken de çalışır |
| POST | `/auth/otp/request` | — | Y | **P5, PLT-01.** `{phone}` (E.164 ya da 05XX…) → `Written[{sent_to (maskeli), expires_at, resend_after_seconds, test_code}]`. Numara kayıtlı olsun olmasın **aynı yanıt**. Sınır (§4.7, hedef başına, amaçtan bağımsız): 60 sn'de bir → 429 `code_too_soon`; günde 10 → 429 `code_daily_limit`; kilitliyken 429 `code_locked` (+ `Retry-After`). IP başına 15 dk'da 20 istek (429 `too_many_attempts`). `test_code` yalnız geliştirme/demo (test sağlayıcısı, K5); üretimde her zaman `null` |
| POST | `/auth/otp/verify` | — | Y | `{phone, code}` → `TokenResponse` + çerez. 6 hane, 3 dk; hatalı 422 `invalid_code` ("Kalan deneme: N"), süresi dolmuş/kullanılmış 422 `code_expired`, 5. hata 429 `code_locked` (numara 15 dk). Kod doğru ama hesap yok → 404 `no_account` (kayıt başvurusuna yönlendir); pasif hesap 403 `account_disabled` |
| POST | `/auth/mfa/verify` | bekleyen oturum | Y | girişin 2. adımı: `{code}` (TOTP 6 hane ya da yedek kod `xxxx-xxxx`) → `TokenResponse`. Hatalı 422 `invalid_code`; 5 hatada hesap 15 dk kilit (429 `account_locked`) ve bekleyen oturum kapanır |
| POST | `/auth/mfa/setup` | giriş (kurulum kapısı açık) | Y | yeni sır → `{secret, otpauth_uri, account, issuer}` (QR'ı frontend çizer). Kurulu doğrulayıcı varsa 409 `mfa_already_enabled` |
| POST | `/auth/mfa/enable` | giriş (kurulum kapısı açık) | Y | `{code}` → `Written[{codes: [10 yedek kod]}]` — kodlar **yalnız bu yanıtta**. Kurulum başlatılmadıysa 409 `mfa_setup_missing` |
| POST | `/auth/mfa/recovery-codes` | giriş | Y | `{code}` (TOTP) → yeni 10 yedek kod; eskiler geçersiz |
| POST | `/auth/mfa/disable` | giriş | Y | `{code}` → 204. Yönetici rolünde 409 `mfa_required_for_role` |
| POST | `/auth/step-up` | giriş | Y | `{password?, code?}` (2FA kodu, yedek kod ya da `/auth/step-up/code` SMS kodu) → `Written[{verified_at, valid_until}]`; hassas uçlar 5 dk açılır (aksi 403 `step_up_required`) |
| POST | `/auth/step-up/code` | giriş | Y | kayıtlı telefona step-up kodu (2FA'sı olmayan telefon kullanıcısı); sınırlar SMS koduyla aynı |
| POST | `/auth/password/forgot` | — | Y | `{email}` ya da `{phone}` → aynı `Written[CodeSentOut]`; kod **yalnız kayıtlı hesaba** gider, yanıt hesabın varlığını sızdırmaz |
| POST | `/auth/password/reset` | — | Y | `{email\|phone, code, new_password}` → `Written[{reset: true}]`. Parola değişir, kilit ve geçici parola işareti kalkar, **bütün oturumlar kapanır**. 422 `weak_password` / `invalid_code` / `code_expired` |
| GET | `/auth/invitations/{token}` | — | Y | hesap daveti (PLT-09, 72 saat): `{full_name, login (maskeli), site_name, role_label, invited_by_name, expires_at, status: pending\|accepted\|expired}`; bilinmeyen 404 |
| POST | `/auth/invitations/{token}/accept` | — | Y | `{password}` → `TokenResponse` (parola belirlenir, oturum açılır). Kullanılmış/süresi dolmuş 410 `invitation_gone` |
| GET | `/me/security` | giriş (kurulum kapısı açık) | Y | `{email, phone, phone_changed_at, has_password, mfa: {enabled, enabled_at, required, setup_deadline, setup_required, recovery_codes_left}}` |
| POST | `/me/phone-change` | giriş | Y | `{phone}` → yeni numaraya kod (`CodeSentOut`) |
| POST | `/me/phone-change/confirm` | giriş | Y | `{phone, code}` → `Written[{phone, step_up_until}]`; eski numaraya bilgilendirme SMS'i; 24 saat finansal işlemde step-up. Numara başka hesaptaysa (kod doğrulandıktan sonra) 409 `phone_taken` |
| GET | `/me/sessions` | giriş | R | açık oturumlarım, son kullanılan üstte: `[{id, device ("Chrome · Windows"), user_agent, ip, created_at, last_used_at, expires_at, is_current}]` (Yazılım Dokümanı PLT-10) |
| DELETE | `/me/sessions/{id}` | giriş | R | bir cihazı kapatır → 204; o cihazın jetonu bir sonraki istekte 401. Başkasının ya da kapalı oturum 404 |
| DELETE | `/me/sessions?keep_current=true` | giriş | R | "tüm cihazlardan çık" → `{revoked, current_revoked}`; `keep_current=false` bu oturumu da kapatır (frontend girişe döner) |
| GET | `/me/security-events` | giriş | R | giriş geçmişim, son 50: `[{at, kind, label, device, ip}]`; `kind`: `login_succeeded` · `login_failed` · `account_locked` · `logout` · `password_changed` · `session_revoked` · `token_reused` · `code_sent` · `code_failed` · `code_locked` · `mfa_enabled` · `mfa_disabled` · `mfa_failed` · `recovery_code_used` · `recovery_codes_regenerated` · `password_reset` · `phone_changed` · `step_up` · `invitation_sent` · `invitation_accepted` |

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
| PATCH | `/sites/{slug}/units/{id}` | `units.manage` | Y | m², arsa payı, tip değişirse **bugünden geçerli** öznitelik geçmişi satırı yazılır (üzerine yazılmaz, YAP-02 S3); ileri tarih için `…/attributes` |
| POST | `/sites/{slug}/units/{id}/parties` | `people.manage` | Y | `{role, start_date, share_percent?, person_id \| person}` — gerekli cari hesapları da açar (`03` §5); malik hisseleri toplamı ≤ 100 (409) |
| POST | `/sites/{slug}/units/{id}/parties/{party_id}/end` | `people.manage` | Y | `{end_date}` — kiracı çıkışı. **Silme değil, bitiş tarihi** |
| POST · PATCH | `/sites/{slug}/blocks`, `/blocks/{id}` | `units.manage` | Y | blok ekle/güncelle; ad sitede benzersiz (Türkçe harf duyarsız) |
| GET | `/sites/{slug}/people` | `people.read` | Y | sayfalı; `q` ad soyad içinde (Türkçe harf duyarsız). Birleştirilmiş kişi listelenmez. `phone`/`email` **maskeli** (`+90 532 *** ** 33`, `a***@ornek.com`) ve yalnız `people.contact` ile; aksi halde null (bölüm ayrıntısındaki taraflarda da aynı) |
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
  (onayda atlanır); telefon/e-posta maskeli ve yalnız `people.contact` ile. `person_merges[]`
  (K16): `{full_name, phone, row_numbers, existing}` — ad + telefonu aynı satırlar tek kişi
  olur, sitede kayıtlı aynı kişi yeniden açılmaz (`existing: true`); kişi birden çok bölüme bağlanır. Sorun: `{row_number | null,
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
| POST | `/sites/{slug}/budget-plans/{id}/finalize` | `finance.budget.manage` | Y | itiraz süresi **dolduktan sonra** (son günün ertesi); öncekiler `superseded`. "Bütçe" onay kuralı varsa **202** ve `finalize_request_id` dolar, onaylanınca kesinleşir (D-16); 409 `finalize_pending` |
| GET · POST | `/sites/{slug}/budgets[?fiscal_year=]` | `finance.read` · `finance.budget.manage` | Y | **yıllık bütçe** (FIN-17, B17) sayfalı · `{fiscal_year, name, income_target?, budget_plan_id?}` → taslak (proje verilirse kalemler kategori × aya `distribute`) |
| GET · PUT | `/sites/{slug}/budgets/{id}` | `finance.read` · `finance.budget.manage` | Y | `{…, lines[{expense_category_id, category_name, months[12], total}], month_totals[12], total}` · `{name?, income_target?, lines?[{expense_category_id, months[12]}]}` yalnız taslak (409 `budget_not_editable`) |
| POST | `/sites/{slug}/budgets/{id}/submit` · `/revise` | `finance.budget.manage` | Y | "Bütçe" onay kuralı varsa 202, yoksa yürürlüğe girer (aynı yılın öncekisi `superseded`); 409 `budget_empty` · revizyon yeni sürüm taslağı (409 `budget_not_active`, `budget_draft_exists`) |
| GET | `/sites/{slug}/budgets/{id}/actuals?month=` | `finance.read` | Y | bütçe–gerçekleşme: `rows[{expense_category_id, category_name, month_budget, month_actual, cumulative_budget, cumulative_actual, annual_budget, remaining, usage_percent, deviation_percent, level ok/warn/over}]`, `totals`, `warn_percent` |
| GET · POST | `/sites/{slug}/billing-rules` | `finance.read` · `finance.budget.manage` | Y | **aidat kuralları** (FIN-02, K23): kural + `current_version` + `versions[]` · `{name, charge_type_id, owner_item, version{effective_period, kind, area_basis, amount, frequency, components[], type_coefficients[], exemptions[], scope_kind, scope_*, discontinued, basis_note, basis_document_id?}}` → taslak |
| GET · PATCH | `/sites/{slug}/billing-rules/{id}` | `finance.read` · `finance.budget.manage` | Y | ayrıntı · `{name, charge_type_id, owner_item}` (yürürlükteki kuralda tip değişmez: 409 `rule_in_use`) |
| POST · PUT | `/sites/{slug}/billing-rules/{id}/versions[/{vid}]` | `finance.budget.manage` | Y | yeni sürüm taslağı (409 `open_version_exists`) · taslak/reddedilmiş sürümü düzenle |
| POST | `/sites/{slug}/billing-rules/{id}/versions/{vid}/submit` | `finance.budget.manage` | Y | "Aidat kuralı" onay kuralı varsa 202, yoksa yürürlükte. 422 geçmiş dönem; 409 `period_locked`, `missing_unit_data` (eksik m²/arsa payı/tip), `scope_overlap` (aynı kalem tipinde çakışan kapsam) |
| POST | `/sites/{slug}/billing-rules/trial` · `/{id}/versions/{vid}/trial` | `finance.read` | Y | **deneme hesabı** (kayıt yok): `{lines[{unit_id, unit_name, amount, explanation}], unit_count, period_total, pool, exempted, annual_total, budget_income_target, warnings}` |
| GET | `/sites/{slug}/charge-types` · `/allocation-rules` · `/expense-categories` | `finance.read` | Y | seçim listeleri (site açılışında varsayılanlar kurulur — `10` §3) |
| GET | `/sites/{slug}/charge-runs` · `/charge-runs/{id}` | `finance.read` | R | geçmiş koşular (sayfalı, en yeni üstte) · ayrıntı |
| GET | `/sites/{slug}/charge-runs/{id}/charges` | `finance.read` | Y | koşunun borçları + kalem dökümü (sayfalı) |
| GET | `/sites/{slug}/charge-runs/preview?charge_date=&due_date=` | `finance.charge.post` | R | varsayılan **sıradaki dönem** — `04` §4, §7.2. Hiçbir şey yazılmaz. Özet + uyarılar + kalem toplamları (`exempted`) + `not_due_items` + sayfalı `charges` (`days`/`period_days` kıst). `source` (K23), `proration` (K9), `approval_required`, `open_draft_id`; `budget_plan` aidat kuralı kaynağında null. Vade varsayılanı `due_day` (K25) |
| POST | `/sites/{slug}/charge-runs` | `finance.charge.post` | R | `{charge_date?, due_date?}` → "Tahakkuk" onay kuralı yoksa kaydet (201); varsa taslak onaya gider (**202**, `status: approval_pending`). 409: dönem zaten kesilmiş, kaynak yok (`no_finalized_budget`, `no_billing_rules`), kesilecek tahakkuk yok, `draft_exists`. `Idempotency-Key` |
| POST | `/sites/{slug}/charge-runs/drafts` | `finance.charge.post` | R | taslak hazırla (201, deftere yazılmaz; önizleme saklanır) |
| POST | `/sites/{slug}/charge-runs/{id}/refresh` · `/submit` · `/cancel` | `finance.charge.post` | R | taslağı yeniden hesapla · gönder (201 kesinleşti / 202 onaya gitti; `Idempotency-Key`) · iptal (onay bekleyen 409) |
| GET | `/sites/{slug}/charge-runs/{id}/diff` · `/summary` | `finance.read` | R | taslağın fark raporu · uyarılar + kalem toplamları |
| POST | `/sites/{slug}/charge-runs/lines/{line_id}/adjustments` | `finance.charge.post` | R | **satır düzeltmesi** (D-10): `{new_amount, reason}` → 201 uygulandı / 202 "Tahakkuk düzeltme" onayı. Kilitli dönemde `prior_period: true`. 409 `charge_not_posted`, `adjustment_pending`. `Idempotency-Key` |
| GET | `/sites/{slug}/charge-runs/{id}/adjustments` | `finance.read` | R | koşunun düzeltmeleri; `/charges` satırlarında `id`, `current_amount` |
| POST | `/sites/{slug}/charge-runs/{id}/reverse` | `finance.charge.post` | R | `{reason}` (3–500). 409: zaten ters kaydedilmiş / ters kayıt koşusu. `Idempotency-Key` |
| GET | `/sites/{slug}/charge-schedule` | `finance.read` | R | **otomatik aylık tahakkuk**: `{enabled, charge_day, draft_lead_days, due_day (salt okunur, K25), approval_required, notify_on_run, next_run_on, last_run{run_id, period, ran_at, status, message}}`. Kaydedilmediyse varsayılan (kapalı, gün 1, taslak −5). `last_run.status`: `posted` · `drafted` · `skipped` · `failed` |
| PUT | `/sites/{slug}/charge-schedule` | `finance.charge.post` | R | `{enabled, charge_day (1–28), draft_lead_days (0–15), notify_on_run}` → aynı nesne + mesaj (`due_days` kalktı — vade finans parametresi). Gece işi: onaylı taslakları tahakkuk tarihinde kesinleştirir; "Tahakkuk" kuralı varsa kesimden `draft_lead_days` gün önce taslağı ayarı açan adına onaya gönderir (`drafted`); yoksa kesim günü **elle kaydetmeyle aynı servis**le keser (uyarı varsa `skipped`) |
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

### 2.8a Finans Mod P — dönem kapanışı, kasa günü, borç takibi, ödeme planı, dönem dışı ücret, muhasebe aktarımı, parametreler (servis istekleri 27–34)
**Dönem kilidi (D-11, FIN-13):** kapanıştaki (`closing`) ya da kilitli (`locked`) döneme **hiçbir
finansal kayıt yazılamaz/değişemez** — tahakkuk ve ters kaydı, tahsilat, iade, devir bakiye, gider
(kayıt/ödeme/düzeltme), kasa hareketi (elle, aktarım, geri alma, hesap açılışı), dönem dışı ücret ve
iptali, ödeme planı. Hepsi **409 `period_locked`** ("Tarih kilitli döneme düşüyor (Eylül 2026); kayıt
yazılamaz — en erken Ekim 2026 (01.10.2026)."). Kilitli döneme ait kaydın ters kaydı da reddedilir.
Süreli açık (`temp_open`) dönemde yazma serbest; denetim kaydına `reason = period_unlocked` satırı
düşer. Merkezi kontrol: `services/period_lock.ensure_writable`.

**Onay (K21, yeni modüller):** dönem kilidi açma, kasa farkı, ödeme planı, dönem dışı ücret, finansal
etkili parametre ve ihtarname için yürürlükte onay kuralı varsa uç **202** döner; kayıt
`status: "approval_pending"` + `approval_request_id` taşır, onaylanınca yürür, ret/geri çekmede
`rejected`. Kural yoksa doğrudan yürür (201/200).

| Yöntem | Yol | İzin | Not |
|---|---|---|---|
| GET | `/sites/{slug}/fiscal-periods?year=` | `finance.read` | yılın dönemleri (yeniden eskiye, bu aya kadar): `{period: "2026-09", label, status: open\|closing\|locked\|temp_open, is_past, exceptions, closing_started_by/_at, locked_by/_at, unlock_until, unlock_reason, unlock_request_id, history[{at, by, action, note}]}` |
| GET | `/sites/{slug}/fiscal-periods/{YYYY-MM}/close-checklist` | `finance.read` | `{period, items[{key, label, ok, detail, link, required, exception}]}` — `charge`, `cash_days`, `approvals`, `accounting` zorunlu; `expenses`, `bank` uyarı |
| POST | `…/fiscal-periods/{p}/start-close` | `finance.charge.post` | `{exceptions?: {madde: gerekçe ≥ 10}}` → Kapanışta. 409 `period_not_ended`, `checklist_incomplete`, `invalid_status` |
| POST | `…/fiscal-periods/{p}/lock` | `finance.charge.post` | **iki kişi kuralı**: başlatan kilitleyemez (409 `four_eyes`); süreli açık dönemi herkes kilitler |
| POST | `…/fiscal-periods/{p}/reopen` | `finance.charge.post` | Kapanışta → Açık |
| POST | `…/fiscal-periods/{p}/unlock-requests` | `finance.charge.post` | `{reason ≥ 20, hours: 4\|24\|72}` → süreli açık (200) ya da "Dönem kilidi açma" kuralıyla 202. 409 `unlock_pending` |
| GET | `/sites/{slug}/cash-accounts/{id}/days` | `finance.cash.read` | `{kind, open_days[] (son 60 gün, hareketli sayılmamış), closed[CashDay] (son 90)}` |
| POST | `/sites/{slug}/cash-accounts/{id}/days/close` | `finance.cash.manage` | `{date, counted, reason?}` → CashDay `{id, date, system_balance, counted, difference, reason, status, approval_request_id, closed_by, closed_at}` (201/202). 422 `difference_reason`; 409 `day_closed`, `not_cash`. Kapanan güne kasa hareketi 422 `cash_day_closed` |
| GET | `/sites/{slug}/receivables/aging` | `finance.read` | `{as_of, total, buckets[{key, label, count, amount}], by_block[{block, 0_30, 31_60, 61_90, 90_plus, total}]}` — toplam = borçlular toplamı |
| GET | `/sites/{slug}/collection-status` | `finance.read` | `{account_id: {plan_status, letter_status, last_action_at, reminder_count}}` |
| GET | `/sites/{slug}/accounts/{id}/collection-timeline` | `finance.read` | sayfalı, yeniden eskiye: `{id, kind: reminder\|call\|note\|demand_letter\|payment_plan, channel, note, stage, by, at, letter?, plan?}` |
| POST | `/sites/{slug}/accounts/{id}/collection-actions` | `finance.payment.record` | `{kind, channel?, note?}` — **gönderim yok (K5)**: `delivered: false`, mesaj bunu söyler |
| POST | `/sites/{slug}/collection-actions/bulk` | `finance.payment.record` | `{account_ids[≤500], channel}` → `{count}` |
| POST | `/sites/{slug}/accounts/{id}/demand-letters` | `finance.payment.record` | `{method: hand\|registered_mail\|notary, sent_on, tracking_no?, note?}` (iadeli/noterde takip no zorunlu) → 201/202 |
| PATCH | `/sites/{slug}/demand-letters/{id}` | `finance.payment.record` | `{served_on}` (gönderimden önce/gelecekte 422) |
| GET | `/sites/{slug}/payment-plans?status=` | `finance.read` | sayfalı + `counts{active, broken, completed, cancelled, approval_pending, remaining_total}` |
| GET · POST | `/sites/{slug}/accounts/{id}/payment-plans` | `finance.read` · `finance.payment.record` | POST `{amount ≤ açık bakiye, installment_count 2–36, first_due_date ≥ bugün, note?}`; `Idempotency-Key`. 409 `plan_exists`. Plan: `{…, total, installments[{no, due_date, amount, paid, status: paid\|partial\|open\|overdue}], status, paid_total, remaining, tolerance_days}`. Yürürlükte: yapılandırılan kalemler kapanır (alacak + `payment_plan_closures`), taksitler vadeli borç (`source: payment_plan`) |
| POST | `/sites/{slug}/payment-plans/{id}/cancel` | `finance.payment.record` | `{reason ≥ 5}`; ödenmemiş tutar ilk vadesiyle yeniden açılır. Onay bekleyen 409 `plan_pending`, tamamlanan 409 `plan_completed` |
| GET | `/sites/{slug}/resident/payment-plans` | sakin (kendi hesapları) | yürürlükteki/iptal planları |
| GET · PUT | `/sites/{slug}/settings/ad-hoc-charges` | `finance.read` · `finance.charge.post` | `{sources: {reservation\|service\|renovation\|penalty\|other: {mode: immediate\|next_period, due_days 0–60}}}` (varsayılan anında, 10 gün) |
| GET · POST | `/sites/{slug}/ad-hoc-charges[?status=]` | `finance.read` · `finance.charge.post` | sayfalı · `{unit_id, account_kind, source, description, amount, due_date?}` → 201/202; `Idempotency-Key`; 422 `no_account`. `next_period` ücret sonraki tahakkukta "ek kalemler" olarak yazılır (koşu ters kaydedilirse yeniden bekler) |
| POST | `/sites/{slug}/ad-hoc-charges/{id}/cancel` | `finance.charge.post` | `{reason}` — yazılmışsa ters kayıt; 409 `already_cancelled`, `charge_pending` |
| GET · PUT | `/sites/{slug}/accounting/mappings` | `finance.read` · `finance.charge.post` | `{items[{key, label, debit_account, credit_account}]}` — versiyonlu; kod rakam+nokta (422 `fields.{key}.debit_account`) |
| GET | `/sites/{slug}/accounting/exports/preview?period=` | `finance.read` | `{period, vouchers[{key, source, source_id, date, description, debit_account, credit_account, amount}], already_exported, total, missing_mappings[]}` |
| POST | `/sites/{slug}/accounting/exports` | `finance.charge.post` | `{period}` → 201 ExportBatch; 409 `mapping_missing`, `nothing_to_export`. Fiş bir kez aktarılır (kilit) |
| GET | `/sites/{slug}/accounting/exports` · `/{id}/file?format=csv\|xlsx` | `finance.read` | sayfalı geçmiş · dosya (CSV `;`, UTF-8 BOM; Kivi biçimi K7 sonrası) |
| GET | `/sites/{slug}/finance-parameters` | `finance.read` | `{current, current_period, earliest_financial_period, defaults, financial_keys, versions (son 100), scheduled}` |
| POST | `/sites/{slug}/finance-parameters/versions` | `finance.charge.post` | `{effective_period, values: {değişen alanlar}}` → 201/202. Finansal etkili erken dönem 422 `period_locked` (`fields.effective_period`); 422 `no_change` |
| GET | `/sites/{slug}/charge-runs/preview/diff?charge_date=&due_date=` | `finance.read` | **fark raporu**: `{period, base_period, threshold, compared, counts{increase, decrease, new, missing}, rows[{account_id, unit_name, person_name, account_kind, previous, current, change_percent, flag}]}` |

Finans parametreleri (varsayılan): `reminder_days [3,10,30]`, `demand_letter_day 60`, `proration day`
(K9), `diff_threshold_percent 10`, `bank_auto_match high`, `plan_tolerance_days 10`,
`plan_suspends_late_fee true`, `budget_warn_percent 90`, `due_day 10` (K25), `charge_source
budget_plan` (K23), `late_fee_enabled false` · `late_fee_monthly_rate_percent "5.00"` (≤ 5) ·
`late_fee_method daily` · `late_fee_grace_days 5` (K2), `allocation_order late_fee_first` (K24),
`debtor_restriction_enabled false` · `_min_amount "0.00"` · `_min_days 90` (K11). Finansal etkili
(sonraki dönemden, onaya tabi): kıst, vade günü, tahakkuk kaynağı, gecikme alanları, mahsup sırası,
plan süresince gecikme, gecikme tavanı (`late_fee_cap_percent 0`) ve asgari tutarı
(`late_fee_minimum_amount "0.00"`). Tahakkuk motoru (P03b) kaynak, kıst ve vade gününü; tahsilat
mahsup sırasını; gecikme işi gecikme alanlarını dönemin sürümünden okur.

Gece/saatlik iş `run-finance-jobs`: süresi dolan süreli açmayı kilitler, borç takibi kademelerini
(aynı açık vade için bir kez; ödeme planındaki hesaba yazılmaz) kaydeder, bozulan planı bildirir,
gecikme açıksa önceki ayın ay sonu gecikme kaydını bir kez yazar (`04` §6), bütçe %90/%100 eşik
uyarısını kategori ve eşik başına bir kez bildirir (FIN-17).

Cari ekstre satırı (`/accounts/{id}/statement`, sakin `/me/statement`): `allocated` (borca yapılan
mahsup) ve `advance_applied` (önceden yatırılmış avanstan mahsup, D-6) alanları.

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
| GET | `/sites/{slug}/requests?status=&category=&priority=&department_id=&sla_breached=` | `requests.read` veya kendi talepleri | R | sayfalı, yeni üstte. İzni olmayan sakin yalnız kendi açtıklarını görür. Satırda `department_id`, `department_name`, `category_name`, `allowed_transitions` (personelin seçebileceği durumlar), `can_cancel`, `sla {due_at, hour_type, target_hours, used_minutes, remaining_minutes, state: ok\|warning\|breached\|met\|missed, paused}`, `outage` (ana olay özeti), `feedback` (son sakin değerlendirmesi), `quote` (son ücretli hizmet teklifi), `auto_close_at`, `closed_at` |
| GET | `/sites/{slug}/requests/{id}` | `requests.read` veya kendi talebi | R | + olay geçmişi (`events`, eskiden yeniye). Başkasının talebi 404 |
| POST | `/sites/{slug}/requests` | `requests.create` | R | `{title, description?, category (site kategorisi anahtarı), priority?, unit_id?, location?, reported_by_person_id?}` → numara site içinde artan. Öncelik boşsa kategorinin varsayılanı; **sakinde öncelik her zaman kategoriden** (HIZ-01). Kategorinin ekibi varsa talep o departmana gider; SLA kural anlık görüntüsü talebe yazılır. Pasif/olmayan kategori 422 `fields.category`. Sakin kendi adına ve yalnız kendi bölümü (ya da ortak alan) için açar (422 `unit_not_yours`) |
| POST | `/sites/{slug}/requests/{id}/status` | `requests.assign` | R | `{status, resolution?}` — geçiş tablosu (K18): Açık → Atandı → İşlemde ⇄ Beklemede → Çözüldü → Kapandı; İptal (açık/atandı/işlemde/beklemede'den). Tablo dışı 409 `invalid_transition`; kapandı/iptal 409 `request_finished`; atanmamış talep "Atandı" olamaz 409 `assignee_required`; sakinin açtığı çözüldü talep personelce kapatılamaz 409 `awaiting_confirmation`. Çözüldü ve iptalde `resolution` zorunlu (422); aynı durum 409 |
| POST | `/sites/{slug}/requests/{id}/assign` | `requests.assign` | R | `{assignee}` — açık talep "Atandı" olur; kapanmış talep 409 |
| GET · POST | `/sites/{slug}/requests/{id}/internal-notes` | `requests.assign` | R | iç not (K26): `{body}` → `{id, body, author_name, created_at}`; değişmez. Sakin uçlarında, talep yanıtında, olay geçmişinde ve bildirimlerde **hiç dönmez** |
| GET | `/sites/{slug}/requests/{id}/merge-suggestions` | `requests.assign` | R | `{linked_outage, similar: [{id, number, title, unit_name, location, created_at}], open_outages: [{id, title, request_count}], window_hours}` — kural tabanlı: aynı kategori + (aynı konum metni ya da ikisi de ortak alan ya da aynı blok) + pencere; bitmemiş ve olaya bağlı olmayan talepler |
| POST | `/sites/{slug}/requests/{id}/quotes` | `requests.assign` | R | ücretli hizmet teklifi `{lines: [{description, qty, unit_price}], note?}` → `ServiceQuoteOut`. Ortak alan talebi 409 `not_unit_request`; açık teklif varken 409 `quote_open`; bölümde açık cari hesap yoksa 409 `no_account`; kalem 422 `fields.lines` |
| POST | `/sites/{slug}/requests/{id}/department` | `requests.assign` | R | `{department_id \| null}` → talep ayrıntısı; geçmişe `department_changed` olayı. Pasif ya da başka sitenin departmanı 422 `fields.department_id` |
| GET | `/sites/{slug}/departments` | `requests.read` | R | dizi `{id, name, is_active, request_count}`; site açılışında Teknik, Temizlik, Güvenlik, Bahçe, Yönetim |
| POST · PATCH | `/sites/{slug}/departments[/{id}]` | `requests.assign` | R | `{name}` / `{name?, is_active?}`. Ad site içinde benzersiz (Türkçe harf duyarsız) → 409 `already_exists`. Silme yok, pasifleştirme var |
| POST | `/sites/{slug}/requests/{id}/comments` | `requests.read` veya kendi talebi | R | `{body}` |

Modül kapalıysa tüm talep uçları 404. `reporter_name` `people.read` izni ya da kendi talebi
değilse `null`. Durum geçişleri dokümandaki akışla (K18, 25.10.2026).

### 2.10a Hizmetler — kategori, SLA, hazır yanıt, ana olay, ücretli hizmet, tadilat (HIZ-02..05)
Modül `requests`. Ayar yazma izni `requests.settings` (yalnız Yönetici).

| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/request-categories?active=` | talep açabilen herkes (sakin dahil) | R | dizi `{id, key, name, hour_type: 24x7\|business, resolve_hours: {low, normal, high, urgent}, default_priority, department_id, is_active}`. Site açılışında HIZ-03 varsayılanları (eski sabit liste anahtarları korunur) |
| POST · PATCH | `/sites/{slug}/request-categories[/{id}]` | `requests.settings` | R | `{name, hour_type, resolve_hours, default_priority, department_id?, is_active}`; anahtar addan üretilir, değişmez; ad site içinde benzersiz (422 `fields.name`); saat 1–2160 (422 `fields.resolve_hours.{öncelik}`). Silme yok, pasifleştirme var |
| GET · PUT | `/sites/{slug}/settings/requests` | okuma `requests.read`, yazma `requests.settings` | R | `{categories: {anahtar: {hour_type, resolve_hours}}, calendar: [{closed, open, close}] × 7 (0 = pazartesi), auto_close_days 1–30, merge_window_hours 1–168, low_rating_threshold 1–4}`. Değişiklik yalnız yeni talepleri etkiler |
| GET · POST · PATCH | `/sites/{slug}/reply-templates[/{id}]` | okuma `requests.assign`, yazma `requests.settings` | R | `{title, category?, body, is_active}`; değişkenler `{{sakin.ad}}`, `{{talep.no}}`, `{{talep.konu}}` ekranda doldurulur |
| GET | `/sites/{slug}/outages?status=` | `requests.read` | R | sayfalı ana olaylar `{id, title, category, location, public_description, status: open\|resolved, request_ids, created_by, created_at, resolved_at, resolution}` |
| POST | `/sites/{slug}/outages` | `requests.assign` | R | `{title, category, location?, public_description, request_ids?}`; bağlı talep 409 `already_linked`, bitmiş talep 409 `request_not_active` |
| POST | `/sites/{slug}/outages/{id}/links` · `/unlink` | `requests.assign` | R | `{request_id}`; çözülmüş olay 409 `outage_resolved` |
| POST | `/sites/{slug}/outages/{id}/resolve` | `requests.assign` | R | `{resolution}` — bağlı açık talepler aynı işlemde "çözüldü" olur, her sakine bildirim |
| GET | `/sites/{slug}/service-tariffs` | `requests.assign` | R | dizi `{id, name, unit, price, is_active}` |
| POST · PATCH | `/sites/{slug}/service-tariffs[/{id}]` | `requests.settings` | R | `{name, unit, price, is_active}` → `{status: saved\|approval_pending, tariff, approval_request_id}`; "Ücretli hizmet tarifesi" onay kuralı varsa **202** |
| POST | `/sites/{slug}/quotes/{id}/complete` | `requests.assign` | R | `{actual_total}` — yalnız sakinin onayladığı teklif (409 `invalid_status`); gerçekleşen ≤ teklif → cariye yazılır (kaynak `service`); aşarsa `extra_pending` (sakin onayı) |
| GET · PUT | `/sites/{slug}/settings/permits` | okuma `requests.read`, yazma `requests.settings` | R | `{weekday_hours "SS:DD-SS:DD", saturday_hours? (boş = yasak), sunday_allowed, deposit_amount, checklist[]}` |
| GET | `/sites/{slug}/permits?status=` | `requests.read` | R | sayfalı başvurular `{id, unit_id, unit_name, type: renovation\|move_in\|move_out, date_from, date_to, contractor, description, status: pending\|approved\|rejected\|completed, approval_request_id, approval_status, reject_reason, checklist, deposit, deposit_refunded, …}` |
| POST | `/sites/{slug}/permits/{id}/approve` | `requests.assign` | R | "Tadilat / taşınma izni" onay kuralı varsa **202** `{status: approval_pending, approval_request_id}`; yoksa onaylanır: sakine, komşu bölümlere (aynı blok, ±1 kat) ve güvenliğe bildirim; tadilatta depozito > 0 ise malik hesabına borç (kaynak `deposit`) |
| POST | `/sites/{slug}/permits/{id}/reject` · `/complete` | `requests.assign` | R | ret `{reason}` (422 `fields.reason`); tamamlama `{checklist: [{item, ok}], refund_deposit}` — giriş taşınması dışında liste zorunlu; depozito iadesi ters kayıt |

### 2.11 Duyuru
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/announcements` | `announcements.read` | R | sabitlenmiş önce, sonra yayın tarihine göre. Yayınlayan hepsini (süresi geçenler dahil) + `recipient_count`/`read_count`; **sakin yalnız kendisine teslim edilen**, süresi geçmemiş olanları + `read_at`; diğer personel yürürlükteki hepsini |
| GET | `/sites/{slug}/announcements/{id}` | `announcements.read` | Y | aynı görünürlük; görmediği 404 |
| POST | `/sites/{slug}/announcements` | `announcements.publish` | R | `{title, body, importance, audience, audience_block_ids?, channels[], expires_on?, is_pinned, requires_ack?, ack_hours? (1–720), sms_fallback?, publish_at?}` → hedef kişiler için teslim kayıtları. `publish_at` ileride ise **zamanlanır** (`published_at` null, `scheduled_for`); sessiz saate (22:00–08:00, P16c ayarı gelene dek varsayılan) düşen zaman bitişe kayar, `critical` aşar. Geçmiş 422 `fields.publish_at`; süre 422 `fields.ack_hours`. **Gerçek gönderim: arka plan işi** — henüz yok (`12` K5): `in_app` hemen teslim, diğer kanallar bekler |
| GET | `/sites/{slug}/announcements/{id}/read-report` | `announcements.publish` | Y | okuma raporu (servis isteği 71): `{recipient_count, read_count, requires_ack, ack_deadline, acks[{name, units, at, channel}], not_acked, sms_fallback{due, count, sent, note}, recipients[{name, units, delivered_at, read_at, acked_at, channel}], generated_at}` |
| GET | `/sites/{slug}/scheduled-announcements` | `announcements.publish` | Y | yayın bekleyenler `{id, title, importance, publish_at, requested_at, shifted, requires_ack, created_by}` |
| POST | `/sites/{slug}/scheduled-announcements/{id}/cancel` | `announcements.publish` | Y | yalnız yayınlanmamışsa; değilse 409 `invalid_status` |
| GET | `/sites/{slug}/resident/pending-acks` | sakin | Y | onaylamadığım okuma onaylı duyurular `{announcement_id, title, deadline}` (ana sayfada sabit) |
| POST | `/sites/{slug}/resident/announcements/{id}/acknowledge` | sakin | Y | "Okudum, anladım" (201); okundu da sayılır; tekrar 409 `already`; onay istemeyen 404 |
| POST | `/sites/{slug}/announcements/{id}/read` | `announcements.read` | Y | sakin okundu işaretler (ilk okuma anı korunur); kendisine teslim edilmemişse 404 |

Hedef kitle bugünkü etkin malik/kiracı/oturanlardan seçilir (vekil hariç): `blocks` seçilen
blokların bölümleri, `owners_only` malikler, `tenants_only` kiracılar, `debtors_only` bakiyesi
borçlu bir cari hesabı olanlar. Yayından sonra taraf değişse de teslim listesi değişmez.

Okuma onayı (16.3): son onay = yayın + `ack_hours`; yalnız teslim kaydı olan kişi onaylar (hane
üyesi teslim almaz). Süre dolunca onaylamayanlara **bir kez** SMS yedeği — `run-announcement-jobs`
(dakikalık; zamanlanmış duyuruyu da yayınlar). SMS sağlayıcısı P16c ile bağlanacak
(`services/outbound.register_sms_sender`); o zamana dek kişi sayısı kayda geçer, `sent=false`.

### 2.11a İletişim ve acil durum (servis istekleri 72–74, ILT-02/04/06 — modül `announcements`)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/notification-templates` | `announcements.publish` | Y | sistem + duyuru şablonları `{code, name, channel: push\|sms\|email\|announcement, variables, required, variants[{lang, subject, body}], version, history[{version, at, by}], customized, sms[{lang, encoding, chars, segments}]}` |
| PUT | `/sites/{slug}/notification-templates/{code}` | `announcements.publish` | Y | `{variants[]}` → yeni sürüm (değişmez tablo). Türkçe zorunlu (422 `fields.variants`), zorunlu/tanımsız değişken ve SMS 3 parça sınırı 422 `fields.variants.{lang}` |
| POST | `/sites/{slug}/notification-templates/{code}/reset` | `announcements.publish` | Y | sistem metnine dön (yeni sürüm) |
| GET | `/sites/{slug}/emergency-info-card` · `/resident/emergency-info-card` | personel · sakin | Y | `{muster_points[{id, name, directions, map}], phones[{label, number}], instructions{earthquake, fire, flood, gas}, block_notes[{block_id, block_name, note}], version, updated_at, updated_by}`; `ETag` + `If-None-Match` → 304 (çevrimdışı önbellek) |
| PUT | `/sites/{slug}/emergency-info-card` | `announcements.publish` | Y | yeni sürüm; ≥1 toplanma alanı, telefon 3–13 hane, kroki PNG/JPEG/WEBP data URL (≤ ~300 KB) |
| POST | `/sites/{slug}/emergencies` | `announcements.publish` ya da `security.incidents` | Y | `{kind, scope: all\|blocks, block_ids, message}`; tek aktif (409 `already_active`); kapsam sabitlenir; onay gerekmez, denetime yazılır; kapsamdakilere `urgent` bildirim |
| GET | `/sites/{slug}/emergencies/active` | + `security.visitors` | Y | pano: `rows[{unit_id, unit_name, block_id, block_name, status: safe\|muster\|help\|none, note, muster_point, by, method, at}]` (yardım isteyen üstte), `totals` · yoksa `null` |
| GET | `/sites/{slug}/emergencies` | + `security.visitors` | Y | geçmiş: özet, `purge_on`, `purged` |
| POST | `/sites/{slug}/emergencies/{id}/muster-checks` | + `security.visitors` | Y | `{unit_ids, muster_point}` (karttaki alan) → `{marked, skipped_help}`; yardım isteyen kapanmaz |
| POST | `/sites/{slug}/emergencies/{id}/end` | başlatma izni | Y | özet + `purge_on` = bitiş + `retention_days`; aktif değilse 409 `not_active` |
| GET · PUT | `/sites/{slug}/emergency-settings` | okuma başlatma izni · yazma `announcements.publish` | Y | `{retention_days (30), auto_purge (false)}` — K8; `run-emergency-purge` gece işi yalnız açıksa siler |
| GET | `/sites/{slug}/resident/emergency` | sakin | Y | kapsamdaysam `{id, kind, kind_label, message, started_at, units[{unit_id, unit_name, status}], muster_points}` · yoksa `null` |
| POST | `/sites/{slug}/resident/emergency/status` | sakin | Y | `{status: safe\|help\|muster, note?, muster_point?}` kapsamdaki tüm bölümlerime; kapsam dışı 403, aktif yok 409 `not_active` |

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
| GET · POST | `/sites/{slug}/resident/requests` | sakin | R | kendi talepleri / yeni talep — her zaman kendi adına, yalnız kendi bölümü ya da ortak alan (422 `unit_not_yours`); öncelik kategoriden. Modül kapalıysa 404 |
| POST | `/sites/{slug}/resident/requests/{id}/confirm` | sakin (kendi talebi) | R | `{solved, score?, comment?}` — yalnız çözüldü talep (409 `not_resolved`); çözüldü → 1–5 puan zorunlu (422 `fields.score`), talep kapanır, puan ≤ eşik yöneticiye bildirim; devam ediyor → açıklama zorunlu (422 `fields.comment`), talep yeniden açılır |
| POST | `/sites/{slug}/resident/requests/{id}/cancel` | sakin (kendi talebi) | R | `{reason?}` — yalnız iş başlamadan (açık/atandı); yoksa 409 `not_cancellable` |
| POST | `/sites/{slug}/resident/quotes/{id}/decision` | sakin (teklifin bölümünde) | R | `{decision: accept\|reject}` — teklif aşamasında onay/ret; ek tutar aşamasında onay → gerçekleşen, ret → teklif tutarı cariye yazılır; karar beklenmiyorsa 409 `invalid_status` |
| GET · POST | `/sites/{slug}/resident/permits` | sakin | R | `{rules, items}` kendi bölümlerinin başvuruları / yeni başvuru `{unit_id?, type, date_from, date_to?, contractor?, description}` — tarih bugünden önce olamaz (422) |
| GET | `/sites/{slug}/resident/transparency-reports[/{id}]` | sakin | Y | **K32:** yayınlanmış şeffaflık raporları (kişi/bölüm yok). Eski `resident/expenses` ve `resident/files/{id}` (tüm giderler + fatura görüntüsü) **kaldırıldı** |
| GET | `/sites/{slug}/resident/packages` | sakin | Y | dairelerine gelen, teslim bekleyen kargolar **teslim koduyla** (modül: `packages`) |

"Sakin" = üyeliğinde `person_id` olan kullanıcı; yoksa **403** ("Bu ekran yalnız sakinler
içindir"). Sakin, `requests.read` izni olmadan da kendi taleplerini görür (`05` §6).

### 2.15 Denetim kaydı (`09` §6)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/audit?entity=&entity_id=&action=&user_id=&from=&to=` | `audit.read` | R | sayfalı, yeni üstte. Satır: `{id, at, user_id, actor_name, actor_type, action, entity, entity_id, before, after, ip, user_agent, request_id, reason}`. `action`: `create` · `update` · `delete` · `import` · `download` · `export`. `actor_type` (süzgeç de): `user` · `system` · `delegate`. Telefon, e-posta, IBAN, vergi no `before`/`after` içinde maskelidir. Bölüm, kişi, bölüm–kişi ilişkisi de denetlenir (bir kaydın "Geçmiş" sekmesi: `?entity=units&entity_id=…`). `entity` tablo adıdır (`payments`, `expenses`, `budget_items`, `site_modules` …). Güncellemede `before`/`after` yalnız değişen alanları taşır. `from`/`to` iş günü (İstanbul). `user_id` null: sistem (demo, gece işi) |

### 2.16 Site kullanıcıları (servis isteği 12)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/permissions` | giriş | R | izin ağacı `[{group, permissions: [{key, description, sensitivity: normal\|sensitive\|critical}]}]` (PLT-09) |
| GET | `/sites/{slug}/roles?include_archived=` | `members.manage` ya da `approvals.rules.read` | R | roller (sakin hariç), düzey sırasıyla: `{key, name, description, kind: system\|custom, base_key, level, permissions, is_active, is_critical, holder_count}`. Şablonlar: `manager` · `board` · `auditor` · `accounting` · `hr` · `security` · `technical` · `attendant` |
| POST | `/sites/{slug}/roles` | `members.manage` | R | kopyala ve özelleştir `{base_key, name, description?, permissions?, level?}` → 201. Ad değişmez, sitede benzersiz. 403 `exceeds_own_permissions`: kendinizde olmayan izin |
| PATCH | `/sites/{slug}/roles/{key}` | `members.manage` | R | özel rol `{description?, permissions?, level?, is_active?}`; hazır şablonda yalnız `{level}` (aksi 409 `system_role`). Arşivde etkin atama varsa 409 `role_in_use`; son yönetici kuralı (409 `last_manager`) |
| GET | `/sites/{slug}/members` | `members.manage` | R | personel (sakinler hariç), dizi: `{id, full_name, email, role_key, role_name, roles: [{assignment_id, role_key, role_name, level, scope_kind, block_id, block_name, starts_at, ends_at, is_current}], is_active, source: site\|organization, last_login_at, invited_at}`. `role_key`/`role_name` ana rol (geriye uyum; rolü yoksa null). `organization`: yönetim şirketinden gelen erişim |
| POST | `/sites/{slug}/members` | `members.manage` | R | `{full_name, email, role_key, scope_kind?, block_id?, starts_at?, ends_at?}` → `{member, temporary_password}` (201, `Cache-Control: no-store`). Yeni e-posta → yeni kullanıcı + geçici parola (yalnız bu yanıtta; ilk girişte değiştirme zorunlu). Kayıtlı e-posta → mevcut kullanıcıya rol, `temporary_password: null`. Zaten sitede 409 `already_member` |
| PATCH | `/sites/{slug}/members/{id}` | `members.manage` | R | `{role_key?, is_active?}`. `role_key`: **ana rolü değiştir** (diğer tesis kapsamlı personel rolleri kapanır). Kapatma kullanıcının oturumlarını sonlandırır. 409: `derived_membership` (şirketten gelen erişim), `last_manager` (`members.manage` izinli en az bir etkin kullanıcı kalır), `self_change` (kendi kaydı); 403 `exceeds_own_permissions` |
| POST | `/sites/{slug}/members/{id}/role-assignments` | `members.manage` | R | rol ata `{role_key, scope_kind: site\|block, block_id?, starts_at?, ends_at?}` → 201 güncel üye. 422 (rol, blok, tarih; `gate` kapı tanımları gelene kadar 422), 403 `exceeds_own_permissions`, 409 `assignment_exists` · `self_change` |
| DELETE | `/sites/{slug}/members/{id}/role-assignments/{assignment_id}` | `members.manage` | R | rolü kaldır: atama bitiş zamanıyla kapanır (silinmez). 403 `exceeds_own_permissions`, 409 `assignment_ended` · `last_manager` · `self_change` |
| GET | `/sites/{slug}/role-assignments?user_id=&page=` | `members.manage` | R | rol atama geçmişi (sayfalı, yeni üstte): `{id, user_id, user_name, role_key, role_name, scope_kind, block_name, starts_at, ends_at, assigned_by, assigned_at, removed_by, removed_at}` |

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
| GET | `/sites/{slug}/polls?status=open\|closed` | `announcements.read` (sakin hariç) | R | sayfalı, yeni üstte: `{id, question, description, options (ilk soru), questions[{id, order, text, kind, required, options[{id, label, votes}], answered, rating{average, counts}, texts}], audience, block_ids, is_anonymous, one_per_unit, ends_on, status, total_votes, created_by, created_at, notice}` — toplamlar; `texts` yalnız `polls.manage` |
| POST | `/sites/{slug}/polls` | `polls.manage` | R | `{question (başlık), description?, options[2–8] **ya da** questions[{text, kind: single\|multiple\|rating\|text, options, required}] (≤20), audience: all\|owners\|tenants, block_ids?, ends_on ≥ bugün, is_anonymous (true), one_per_unit (true)}`; hedef kitleye bildirim |
| GET | `/sites/{slug}/polls/{id}/responses` | `polls.manage` | Y | **K31:** isimli ankette kim ne yanıtladı `[{person_name, unit_name, at, answers[]}]`; açılış denetime yazılır; anonimde 409 `anonymous_poll` |
| POST | `/sites/{slug}/polls/{id}/close` | `polls.manage` | R | erken kapatır; kapalıysa 409 `already_closed` |
| GET | `/sites/{slug}/resident/polls` | sakin | R | açık + son 30 günde kapananlar, dizi; ek `my_votes[{unit_id, unit_name, option_id, voted}]` (bölüm başına ankette), `can_respond`, `responded`. Yanıt vermeden ve anket açıkken sonuç alanları **null** |
| POST | `/sites/{slug}/resident/polls/{id}/vote` | sakin | R | `{unit_id?, option_id}` (eski) ya da `{unit_id?, answers[{question_id, option_ids, rating, text}]}` → `{option_id}`. Bölüm başına ankette `unit_id` zorunlu, **bir bölüm = bir yanıt**; değilse kişi başına bir. Kapalı 409 `poll_closed`, tekrar 409 `already_voted`, bölüm sakinin değil 404, hedef dışında 403 `not_eligible`, eksik/yanlış yanıt 422 `fields.answers.{question_id}` (eski biçimde `fields.option_id`) |

`ends_on` günü dahil açık. `audience`: `all` bölümdeki malik/kiracı/oturan (ilk veren), `owners`
malik, `tenants` kiracı ya da oturan; vekil oy vermez. **Gizlilik anket bazında (K31):** anonim
ankette kimin neye yanıt verdiği tutulmaz (kişi başına ankette yalnız katılım ayrı tabloda);
isimli ankette yanıt veren kişi tutulur ve dökümü yalnız `polls.manage` görür. Yanıt tabloları
denetim kaydına yazılmaz. Sonuç danışma niteliğindedir — resmî oylama değildir (`notice`).

### 2.19a Şeffaflık raporu (RAP-02, K32 — modül `finance`)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/transparency-reports[/{id}]` | `finance.reports.read` ya da `transparency.manage` | Y | `{year, month, period, status: draft\|pending\|published\|rejected, scope, content{period, income, expense, difference, categories[{name, amount, count, share}], collection{charged, collected, rate}, cash_balance}, note, prepared_by, approval_request_id, published_at, published_by, rejection_reason}` |
| POST | `/sites/{slug}/transparency-reports` | `transparency.manage` | Y | `{year, month, scope{include_categories, include_collection, include_cash_balance}, note?}` → taslak; tutarlar o an donar. Gelecek ay 422 `fields.month`; dönemde reddedilmemiş rapor varsa 409 `exists` |
| DELETE | `/sites/{slug}/transparency-reports/{id}` | `transparency.manage` | Y | yalnız taslak/reddedilen (204); değilse 409 |
| POST | `/sites/{slug}/transparency-reports/{id}/publish` | `transparency.manage` | Y | "Şeffaflık raporu" onay kuralı varsa 202 `{status: approval_pending}`; yoksa yalnız `transparency.publish` (yönetici) doğrudan yayınlar, diğerine 403. Yayında sakinlere bildirim. Ret → `rejected` + gerekçe |

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

### 2.20b Operasyon ekleri ve teknik varlık (servis istekleri 53–58; Yazılım Dokümanı OPS-01..06, HIZ-06, HIZ-07; K28, K29, K14)
Çekirdek (modül yok). Küçük listeler **dizi** döner (envanter uçlarıyla aynı; üst sınır uçta yazılı).
Endeks, tüketim, miktar ve çarpan **ondalık metin**; para `Money`. Saha fotoğrafı istekte veri adresi
(`data:image/jpeg;base64,…`, içerik imzası JPEG/PNG/WebP, ≤ 2 MB), dosya deposuna yazılır; yanıtta
yalnız `photo_id` döner.

| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET · POST · PATCH | `/sites/{slug}/keys` · `/{id}` | `requests.assign` **ya da** `security.visitors` | R | `{kind: key\|card\|remote, code (site içinde tekil, Türkçe büyük harf), name, area?, authorized_roles[] (boş: herkes), is_active}` → `{…, loan, overdue}` |
| POST | `/sites/{slug}/keys/{id}/loans` | aynı | R | `{to_name, to_phone?, role?, due_at (ileri)}` → 201. Açık teslim tekil (kısmi indeks): 409 `key_out`; rol yetkili değil 409 `not_authorized`; pasif anahtar 409 `key_inactive` |
| POST · GET | `/sites/{slug}/key-loans/{id}/return` · `/key-loans` | aynı | R | iade (409 `already_returned`); geçmiş son 100 |
| GET · POST | `/sites/{slug}/deposited-keys` · `/{id}/receive\|hand\|return` | aynı | R | emanet; `hand {name, id_checked}` — ad yetkili listesinde (harf duyarsız, Türkçe harf katlanır) değilse 409 `not_authorized` (+ sakine bildirim), kimlik kontrolü yoksa 422 `fields.id_checked`, süre geçmişse 409 `expired`. Verilince sakine bildirim. `status: requested\|held\|handed\|returned\|expired` (expired okumada) |
| GET · POST | `/sites/{slug}/resident/deposited-keys` | sakin | R | `{authorized: [{name, phone?}], valid_until ≥ bugün, note?, unit_id?}` (boş: ilk bölüm) |
| GET · POST | `/sites/{slug}/stock-counts` | `inventory.read` · `inventory.manage` | R | `{lines: [{item_id, counted}]}` → `{id, status, approval_request_id, lines: [{item_id, name, system, counted, diff}], created_by, created_at, applied_at}`. Fark "Sayım farkı" hareketi; **"Stok sayım farkı" onay kuralı varsa 202** `approval_pending`, hareket onaydan sonra (ret → `rejected`, geri çekme → `withdrawn`, yürütülemedi → `failed`). 422 `fields.lines` / `fields.lines.{item_id}`, bilinmeyen malzeme 404 |
| GET · POST · PATCH | `/sites/{slug}/checklist-templates` · `/{id}` | `requests.assign` | R | `{name, items: [{text, type: yesno\|number\|score, photo_required, min?, max?}], is_active}`; ilk listede 5 hazır şablon |
| GET · POST | `/sites/{slug}/inspections` | `requests.assign` | R | `{template_id, location, answers: [{value, photo?}], open_requests=true}` → `{…, answers: [{text, type, value, ok, photo_id}], score, failed, opened_request_ids}`. Olumsuz her madde ortak alan talebi (talep modülü açıksa). Denetim değişmez |
| GET · POST · PATCH | `/sites/{slug}/insurance-policies` · `/{id}` | `expenses.read` · `expenses.manage` | R | `{kind, insurer, policy_no, coverage?, premium, start, end > start, area?, note?}` → `+ days_left, warning: "60"\|"30"\|"expired"\|null, documents[]` |
| GET · POST · PATCH | `/sites/{slug}/insurance-claims` · `/{id}` | aynı | R | `{policy_id, source?, description, incident_date ≤ bugün, claimed?}`; PATCH `{status, paid?, paid_on?}` — açık → iletildi/ret; iletildi → ödendi/ret/açık; ödendi tutar zorunlu (422 `fields.paid`); diğer geçiş 409 `invalid_status` |
| GET · POST · PATCH | `/sites/{slug}/meters` · `/{id}` | `units.manage` | R | `{kind, serial (aktif sayaçlarda tekil), unit_id \| area, unit_label?, multiplier, installed_on?, status: active\|faulty}` (`?include_replaced=true`) |
| GET · POST | `/sites/{slug}/meter-readings?period=YYYY-AA` | `units.manage` | R | dönem: `{period, rows: [{meter, reading, last}], missing}`; okuma `{meter_id, period, value \| unreadable_reason, photo?, replacement?: {old_final, new_serial, new_initial}}`. Endeks düşerse 422 `index_decreased`; okunmuş dönem 409 `already_read` (okunamadı ve anormal yeniden okunabilir); ±%X (ayar, varsayılan 50) sapma `anomaly` |
| POST | `/sites/{slug}/meter-readings/{id}/approve\|reread` | `units.manage` | R | yalnız `anomaly` (409 `invalid_status`) |
| GET · PUT | `/sites/{slug}/operation-settings` | `units.manage` | R | `{meter_anomaly_percent (1–1000), meter_photo_required}` |
| GET | `/sites/{slug}/resident/meter-readings` · `/{id}/photo` | sakin | R | bölümlerinin sayaçları ve son 24 okuma; fotoğraf |
| GET · POST · PATCH | `/sites/{slug}/task-templates` · `/{id}` | `requests.read` · `requests.assign` | R | `{name, description?, frequency: daily\|weekdays\|monthly, weekdays[0..6], month_day 1–28, time_from, time_to > time_from, location?, assignee, staff_member_id? (K29), photo_required, checklist_template_id?, is_active}` |
| GET · POST | `/sites/{slug}/tasks?date=&staff_member_id=` · `/tasks/{template_id}/complete` | `requests.read` | R | günün görevleri `[{template, done, status: upcoming\|due\|late\|done\|failed}]`; kapatma `{date?, status: done\|failed, reason?, note?, photo?}` → 201; 404 "Bu gün için görev yok", 409 `already_done`, 422 `photo_required` / `fields.reason`, ileri tarih 409 `future_task` |
| GET | `/sites/{slug}/tasks/report` | `requests.read` | R | son 7 gün `{from, to, rows: [{template_id, name, assignee, expected, done, failed, rate}], rate}` |
| GET · POST | `/sites/{slug}/me/tasks?date=` · `/me/tasks/{template_id}/complete` | personel kartı (K29) | R | yalnız kendi kartına atanmış görevler; kartı yoksa 403 |
| GET | `/sites/{slug}/asset-inspection-templates` | `inventory.read` | R | yasal muayene şablonları (kategori, önerilen periyot) |
| GET · POST · PATCH | `/sites/{slug}/technical-assets` · `/{id}` | `inventory.read` · `inventory.manage` | R | **K28: mali demirbaştan (`/assets`) ayrı.** `{category, name, location?, brand?, model?, serial_no?, installed_on?, warranty_until?, contract_id?, service_company?, status: working\|faulty\|out_of_service\|nonconforming, note?, inspections?: [...]}` → `TV-0001`, `next_inspection`; ayrıntı `{asset, inspections, records, plans, documents}` |
| POST · PATCH | `/sites/{slug}/technical-assets/{id}/inspection-schedules` · `/asset-inspection-schedules/{id}` | `inventory.manage` | R | `{template_key?, name?, period_months 1–120, last_inspected_on? \| next_due_on?, is_active?}`; sonraki tarih = son muayene + periyot |
| GET | `/sites/{slug}/asset-inspections/calendar?within_days=` | `inventory.read` | R | yaklaşan önce; `warning: "60"\|"30"\|"7"\|"overdue"` |
| POST | `/sites/{slug}/asset-inspection-schedules/{id}/records` | `inventory.manage` | R | **multipart** `inspected_on, result: pass\|fail, inspector?, note?, cost?, report (zorunlu)`; rapor belge arşivine bağlanır; olumsuz → yüksek öncelikli düzeltme talebi + varlık `nonconforming`. Kayıt değişmez |
| GET · POST · PATCH | `/sites/{slug}/maintenance-plans` · `/{id}` | `inventory.read` · `inventory.manage` | R | `{asset_id, title, description?, period_unit: week\|month, period_count 1–60, next_due_on, responsible?, contract_id?, checklist_template_id?, is_active}` |
| POST | `/sites/{slug}/maintenance-plans/{id}/runs` | `inventory.manage` | R | sıradaki bakımın iş emrini şimdi aç (409 `run_exists` / `plan_inactive`) |
| GET · POST | `/sites/{slug}/maintenance-runs?status=&asset_id=` · `/{id}/complete` | `inventory.read` · `inventory.manage` | R | kapatma `{status: done\|skipped, done_on?, cost?, note?, issue_found, issue_description?}` — sorun → arıza talebi + varlık `faulty`; sonraki tarih hesaplanır |
| GET | `/sites/{slug}/technical-assets/{id}/timeline` | `inventory.read` | R | `{events: [{date, at, kind, title, detail, cost, request_id, request_number, request_status, document_id}], annual_costs: [{year, total}]}` |
| POST · GET | `/sites/{slug}/subject-documents` · `?subject_type=&subject_id=` · `/{id}/download` | kaydın izni (sigorta: `expenses.*`, varlık: `inventory.*`) | R | **multipart** `subject_type: insurance_policies\|insurance_claims\|technical_assets, subject_id, title?, file` → belge arşivine (`documents.subject_type/subject_id`, gizlilik "yönetim") yazılır; belge modülü kapalı olsa da kayıttan indirilir |
| GET | `/sites/{slug}/operation-photos/{id}` | sayaç: `units.manage` · denetim: `requests.assign` · görev: `requests.read` | R | saha fotoğrafı; bağlı olmadığı kayıttan 404 |

Zamanlanmış işler: `run-operations-jobs` (günde bir, gece) — yasal muayene 60/30/7 gün ve
gecikme uyarısı (`inventory.manage` sahiplerine), planı gelen bakımın iş emri, poliçe 60/30 gün ve
bitiş uyarısı (`expenses.manage`), dünün görev özeti (`requests.assign`); `run-key-reminders`
(saatlik) — iade zamanı geçen anahtar. Hepsi tekrar güvenli (bildirim tekil anahtarı). **K14:**
sayaç okumaları tahakkuk motoruna (`by_meter_consumption`) henüz beslenmez.

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
| GET · PUT | `/me/notification-preferences` | giriş | R | kategori × kanal (`push` · `sms` · `email`) matrisi: `{in_app: true, delivery_enabled: true, test_provider: true, categories: [{category, label, rule, channels: [{channel, enabled, locked}]}]}`. PUT `{changes: [{category, channel, enabled}]}`. Acil ve yönetişimin varsayılan kanalları kapatılamaz (`locked`), finansal ve güvenlikte en az bir kanal açık kalır; aksi 422 `preference_not_allowed` |
| POST | `/me/push-tokens` | giriş | R | `{token, platform: ios\|android\|web}` → 204; tekrar güvenli; aynı jeton başka hesapla gelirse ona taşınır |
| POST | `/me/push-tokens/remove` | giriş | R | `{token}` → 204 (çıkışta); başkasınınki 404 |

Uygulama içi bildirim **her zaman** yazılır. Modüller `services/notifications.notify()` ile,
olayı üreten kayıtla aynı transaction'da yazar; aynı olay aynı kişiye ikinci kez yazılmaz (tekil
anahtar). Aynı çağrı **gönderim katmanında** (`services/delivery.py`, NotificationService) dış
kanal teslim kayıtlarını üretir: kişinin tercihi ve adresi (push jetonu, telefon, e-posta),
sitenin sessiz saatleri (acil aşar). Kritik (acil) bildirimde push 2 dk'da teslim edilmezse SMS
yedeği; push jetonu yoksa SMS hemen. Gönderim isteğin hemen ardından ve dakikalık
`run-notification-jobs` ile yapılır. Sağlayıcı K5'e kadar **test sağlayıcısı** (kayda geçer,
dışarı gitmez; `test_provider: true`).

### 2.21a SMS hesabı, sessiz saatler, teslim kayıtları, planlı raporlar (PLT-05, ILT-05, RAP-05)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET · PUT | `/sites/{slug}/communication-settings` | GET site üyesi · PUT `notifications.manage` | Y | `{quiet_from: "22:00", quiet_to: "08:00"}` (servis isteği 71 biçimi). Aynı saatler 422 |
| GET · PUT | `/sites/{slug}/settings/sms` | `notifications.manage` | Y | `{mode: platform\|own, sender, provider, api_key_last4, monthly_quota, quota_source, providers, test_provider, platform_sender}`. PUT `{mode, sender?, provider?, api_key?}`; kendi hesabında başlık (3–11), sağlayıcı (`netgsm`·`iletimerkezi`·`verimor` — K5 adayları) ve anahtar zorunlu; anahtar şifreli, yanıtta yalnız son 4 karakter |
| POST | `/sites/{slug}/settings/sms/test` | `notifications.manage` | Y | `{phone?}` (boş → kendi numaram) → `Written[{delivery_id, recipient, status, segments, test_provider}]` |
| GET | `/sites/{slug}/settings/sms/usage` | `notifications.manage` | Y | bu ay: `{month, quota, used, remaining, overage, percent, forecast, messages, queued, by_category, warn_percents}`. Kota dolunca gönderim durmaz, aşım işaretlenir; %80/%100'de yöneticiye bildirim |
| GET | `/sites/{slug}/notification-deliveries?channel=&status=` | `notifications.manage` | Y | sayfalı teslim kayıtları: `{channel, category, kind, recipient (maskeli), subject, body, status, status_label, is_critical, deferred, scheduled_for, sent_at, provider, cost_units, is_overage, is_fallback, error}` |
| GET | `/sites/{slug}/scheduled-reports/options` | `reports.schedule` | Y | rapor türleri + tür başına **yetkili** alıcı adayları |
| GET · POST | `/sites/{slug}/scheduled-reports` | `reports.schedule` | Y | tanım: `{name, report_key, frequency: daily\|weekly\|monthly, weekday?, day_of_month? (1–28), send_time "SS:DD", format: pdf\|xlsx, recipient_user_ids, is_active}`. Yetkisiz alıcı 422 (RAP-05 adım 2) |
| PUT · DELETE | `/sites/{slug}/scheduled-reports/{id}` | `reports.schedule` | Y | güncelle / pasifleştir (204; geçmiş korunur) |
| POST | `/sites/{slug}/scheduled-reports/{id}/run` | `reports.schedule` | Y | "Şimdi gönder" → `Written[{run_at, period_label, status: sent\|skipped\|failed, recipient_count}]` |
| GET | `/sites/{slug}/scheduled-reports/{id}/runs` | `reports.schedule` | Y | çalışma geçmişi (sayfalı) |
| GET | `/platform/test-inbox?to=` | platform yöneticisi | Y | test sağlayıcısının son 100 mesajı (kodlar ve davet bağlantıları dahil, süreç belleği). **Yalnız geliştirme/demo**; üretimde ya da `TEST_INBOX_ENABLED=false` iken 404 |

Zamanlanmış işler: `run-notification-jobs` (dakikalık: kritik SMS yedeği + sessiz saat kuyruğu +
yeniden deneme), `run-scheduled-reports` (dakikalık). Rapor içerikleri sonra bağlanır; e-posta
şimdilik rapor adı, dönem ve panel bağlantısını taşır.

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
| GET · POST | `/sites/{slug}/delegations` | personel | R | devirlerim `[{…, state: scheduled\|active\|ended, direction: given\|received}]`; POST `{to_user_id, roles: [kendi rollerim (ad)], starts_at, ends_at, note?}`; devralan aynı ya da üst düzeyde role sahip olmalı (422 `to_user_id`) |
| GET | `/sites/{slug}/delegations/candidates` | personel | R | `{my_roles, candidates: [{user_id, name, role, roles, can_take}]}` — aynı ya da üst düzeyde role sahip kişiler; `can_take`: rollerimden alabildikleri (ONY-03, K20) |
| GET | `/sites/{slug}/approval-rules/escalation-suggestion?roles=&roles=` | `approvals.rules.read` | R | `{role}` — adımlardaki en üst rolün hemen üst düzeyinden rol önerisi (K20); yoksa null |
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
için şemada `subject_type/subject_id` var; poliçe, hasar dosyası, teknik varlık ve muayene raporu bunu kullanır (§2.20b `subject-documents`).
"Okudum" onayı (ILT-01, istek 71) sonraki adım.

### 2.24 Yapı, kişiler ve kayıt (servis istekleri 19–26, Yazılım Dokümanı YAP-01..10)
| Yöntem | Yol | İzin | Not |
|---|---|---|---|
| GET · POST · PATCH | `/sites/{slug}/common-areas` · `/{id}` | okuma `units.read`, yazma `units.manage` | dizi (az sayıda); `{name, kind: pool\|hall\|gym\|garden\|playground\|parking\|technical\|roof\|other, block_id?, is_reservable, is_active, note}` + `block_name`. Ad sitede tekil (422 `fields.name`); silinmez, pasifleşir |
| POST · PATCH | `/sites/{slug}/unit-types` · `/{id}` | `units.manage` | `{name, weight ("1.25"), sort_order}`; ad tekil (422) |
| PATCH | `/sites/{slug}/units/{id}/attributes` | `units.manage` | `{effective_from, gross_area?, net_area?, land_share_numerator?, land_share_denominator?, unit_type_id?}` — yalnız gönderilen alanlar. Kapanmış döneme (en erken içinde bulunulan ay başı; ay kapalıysa sonraki açık ay) 422 `period_locked` (`fields.effective_from`); değişen alan yoksa 422 `no_change`; pay+payda birlikte. Bugün/önce → hemen uygulanır; ileri tarih `applied=false`, tarihinde gece işi `run-structure-jobs` uygular (tahakkuk önizlemesi de tarihi gelenleri uygular). Geçmiş tahakkuk değişmez → `Written[UnitDetail]` |
| GET | `/sites/{slug}/units/{id}/attribute-history` | `units.read` | dizi, yeniden eskiye: `{id, effective_from, changes: {alan: {from, to}} (tipte ad), changed_by, created_at, applied}`. Geçmiş satırı silinmez, uygulanmış satır değişmez (tetikleyici) |
| POST | `/sites/{slug}/units/bulk-update` | `units.manage` | `{unit_ids[], changes: {unit_type_id?, usage?, is_active?}}` → `{updated, failed: [{unit_id, message}]}`; m²/arsa payı toplu değişmez; satır satır, başarısızlar diğerlerini geri almaz; tip değişikliği geçmişe yazılır |
| GET | `/sites/{slug}/structure/summary` | `units.read` | `{unit_count, active_unit_count, block_count, land_share_sum, land_share_denominator (etkin bölümlerde en sık), land_share_mismatch, units_without_land_share, units_without_owner[{id, display_name}], units_shares_incomplete[{id, display_name, total}], common_area_count}` — veritabanında toplanır |
| GET | `/sites/{slug}/units/{id}/overview` | `units.read` | `{balance (finance.read yoksa null), share_total, warnings: no_owner\|shares_incomplete\|no_land_share, packages_waiting, vehicles, household}` |
| POST | `/sites/{slug}/people/{id}/contact-views` | `people.contact` | açık `{phone, email}` döner; denetim kaydına `action=contact_view` (kim, hangi kişi, ne zaman; değerin kendisi yazılmaz) |
| GET | `/sites/{slug}/requests?unit_id=` · `/visitors?unit_id=` | ilgili modül izni | bölüm süzgeci; ziyaretçide `date` yoksa son 30 gün |
| GET · POST | `/sites/{slug}/units/{id}/household` | `people.manage` | hane üyeleri (bitenler dahil) / davet `{full_name, phone, permissions{visitors, packages, reservations, requests}}` — bölüm başına en fazla 6 etkin (409 `household_limit`), aynı telefon 409 `already_member`. Yanıttaki `invite_link` (`/davet/{token}`) yalnız bu yanıtta (belirtecin özeti saklanır) |
| PATCH · POST | `/sites/{slug}/household/{id}` · `/{id}/end` | `people.manage` | yetkiler / bitirme (silmez; 409 `already_ended`). Bitince oturan ilişkisi kapanır, başka ilişkisi yoksa giriş erişimi kapanır |
| GET · POST · POST | `/sites/{slug}/resident/household` · `/{id}/end` | sakin (malik/kiracı olduğu bölüm) | başka bölüme 404 |
| GET | `/sites/{slug}/people/duplicates` | `people.manage` | dizi `[{key (maskeli), kind: phone\|email, people[]}]` — otomatik birleştirme yok |
| POST | `/sites/{slug}/people/merge` | `people.manage` | `{keep_id, merge_id}` → `{kept, merged}`; geri alınamaz: kişiye bağlı tüm kayıtlar (şemadan bulunur) kalan kişiye taşınır, birleşen kişi `merged_into_id` ile işaretlenir, listelerde görünmez. Aynı kişi 422; iki ayrı giriş hesabı 409 `two_logins`; aynı bölümde aynı tür cari 409 `accounts_conflict` |
| POST | `/sites/{slug}/units/{id}/ownership-transfer` | `units.manage` (+ yeni kişi için `people.manage`) | `{reason: sale\|inheritance, transfer_date, debt_policy: "stays_with_seller", new_owners: [{person_id \| person, share_percent}]}` — atomik: eski malikler `transfer_date − 1` gün kapanır, yeniler `transfer_date`'te başlar; hisse toplamı tam %100 (422 `fields.new_owners`); devir öncesi borç eski malikte (K10; başka politika 422 `not_supported`); `period_locked`; yeni kişinin telefonu/e-postası kayıtlıysa 409 `person_exists` (`fields.existing_person_id`). → `{status: done\|approval_pending, unit, approval_request_id}`; `ownership_transfer` onay kuralı varsa **202** |
| GET · PUT | `/sites/{slug}/settings/vehicles` | okuma `units.read`, yazma `units.manage` | `{plate_limit_per_unit 1–10 (varsayılan 2), approval_required}` |
| GET | `/sites/{slug}/vehicles?unit_id=&status=&q=` | `units.read` | **sayfalı**; `{id, unit_id, unit_name, plate (34ABC123), plate_display (34 ABC 123), brand_model, color, kind: permanent\|guest, valid_until, status: active\|pending\|inactive, owner_person_id, owner_name, created_at}` |
| POST · PATCH | `/sites/{slug}/vehicles` · `/{id}` | `units.manage` | kalıcı plaka sınırı 422 `plate_limit`; misafirde `valid_until` zorunlu; başka bölümde etkin 409 `plate_active_elsewhere` (`force: true` ile eklenir); aynı bölümde 409 `already_registered`; PATCH `{brand_model, color, valid_until, status}` (onay / pasif) |
| GET · POST · POST | `/sites/{slug}/resident/vehicles` · `/{id}/remove` | sakin | `{limit, approval_required, items}`; sakin `force` kullanamaz; onay ayarı açıksa `pending` |
| GET | `/sites/{slug}/units/lookup?q=` | güvenlik | etkin araç plakasıyla da eşleşir (yanıt yine bölüm + oturan adı) |
| GET · PUT | `/sites/{slug}/settings/registration` | `people.manage` | `{invite_enabled, qr_enabled, code_enabled, owner_can_approve_tenant, owner_document_required, invite_valid_days 1–30, kvkk_published}` (iki "owner_*" alanı yalnız saklanır) |
| GET · POST | `/sites/{slug}/invitations?status=` | `people.manage` | **sayfalı** (hane daveti hariç); POST `{full_name, phone, email?, unit_id, relation: owner\|tenant}` → `link` yalnız bu yanıtta. Kurulumdaki site 409 `site_not_live`; yöntem kapalı 409 `method_disabled`; açık davet 409 `already_invited`. SMS yok (K5) |
| POST | `/sites/{slug}/invitations/{id}/resend` · `/cancel` | `people.manage` | yeni belirteç (eskisi geçersiz) / iptal; tamamlanmış 409 `already_completed` |
| GET | `/public/invitations/{token}` | **oturumsuz** | IP sınırı; `{site_name, full_name, unit_name, relation, phone_hint (son 4), status, expires_at, email_required}`; geçersiz/dolmuş/iptal 404 |
| POST | `/public/invitations/{token}/accept` | **oturumsuz** | `{phone, kvkk_ack, email?}` → 201 `{unit_name, login_email, temporary_password \| null}`; telefon tam eşleşmeli (422 `fields.phone`), giriş e-postayla olduğundan davette e-posta yoksa istenir (422 `fields.email`), ikinci kabul 409 `already_completed`. Kişi telefonla eşleşir ya da açılır, bölüme ilişkisi eklenir (hane üyesi: oturan, cari yok), sakin hesabı açılır/bağlanır |
| GET · POST | `/public/registration/{code}` | **oturumsuz** | yanıta `blocks[{id, name, units[{id, name}]}]` (kişi adı yok), `document_required`, `relations`; POST gövdesine `unit_id?`. QR ve site kodu kapalıysa 404 `method_disabled`; kurulumdaki site 409 `site_not_live` |
| GET · POST · PUT | `/sites/{slug}/custom-fields?entity=&archived=` · `/{id}` · `/{id}/archive` | tanım `modules.manage`; okuma ayrıca `units.read`/`people.read` (yalnız `view_roles` içindeki etkin alanlar) | `{entity: unit\|person, label, key (değişmez), type: text\|number\|date\|bool\|select, options, required, kvkk_category, view_roles, edit_roles, resident_can_view, resident_can_edit, sort_order, is_archived}`; nesne başına 30 etkin (409 `field_limit`); tip değişmez (422); özel nitelikli çağrışım 422 `sensitive_label` → `acknowledge_sensitive` + `sensitive_reason` ile geçer (denetimde gerekçe) |
| GET · PUT | `/sites/{slug}/units/{id}/custom-values` · `/people/{id}/custom-values` | `units.read` / `people.read` + alan rolleri | `{values: {anahtar: değer}}` — görünmeyen alan dönmez; PUT yalnız gönderilen anahtarlar, boş siler, `edit_roles` dışı 403; tipe göre 422 `fields.{key}`; değişiklik denetimde |
| GET | `/sites/{slug}/imports` | `units.manage` ya da `finance.payment.record` (türüne göre) | **sayfalı** aktarım geçmişi `{id, type: units\|vehicles\|opening_balances, file_name, status: imported\|rolled_back, row_count, created_count, total_amount, created_by, created_at, rollback_until, rollback_blocked_by, can_rollback}` |
| POST | `/sites/{slug}/imports/vehicles` · `/opening-balances` | `units.manage` · `finance.payment.record` | multipart `file` (.xlsx / .csv, 5 MB) → önizleme `{import_id, type, total_rows, importable_count, total_amount, issues[], rows[], expires_at}` (6 saat); hiçbir kayıt yazılmaz. Şablonlar `…/vehicles/template.xlsx`, `…/opening-balances/template.xlsx` |
| POST | `/sites/{slug}/imports/{id}/confirm` | türüne göre | araç: hatalı satır atlanır. Açılış bakiyesi: tüm satırlar temiz (409 `rows_invalid`), `reconciliation_total` zorunlu ve kuruşuna eşit (409 `reconciliation_mismatch`); her satır devir bakiye kuralıyla (hesap başına bir) → `{created, skipped[], total_amount}`. Aynı önizleme ikinci kez 404 |
| POST | `/sites/{slug}/imports/{id}/rollback` | türüne göre | 7 gün; araç → pasif, bölüm aktarımı → oluşturulan kayıtlar silinir, açılış bakiyesi → ters kayıt. Sonradan hareket/ilişki/bağlı kayıt varsa 409 `rollback_blocked`; süre 409 `rollback_expired` |
| GET | `/sites/{slug}/setup` | `modules.manage` | `{status: setup\|live, went_live_at, finance_mode: "platform", ready, opening_reconciled_at, checklist[{key, label, ok, detail, link, required}]}` — her çağrıda gerçek veriden; zorunlu: yapı, malik, **hisseler tam %100**, banka IBAN, otomatik tahakkuk, açılış mutabakatı, yönetici, KVKK |
| POST | `/sites/{slug}/setup/go-live` · `/opening-reconciled` | `modules.manage` | canlıya al (eksik 409 `checklist_incomplete`, tekrar 409 `already_live`; geri alınmaz, denetimde) / mutabakat işareti `{note}` |

Platform panelinden açılan site `setup` durumunda başlar (sakin daveti ve herkese açık kayıt
kapalı); mevcut siteler ve demo `live`. Gece işi: `run-structure-jobs` (günde bir).

---

### 2.25 Satın alma (servis istekleri 62–67, Yazılım Dokümanı SAT-01..06)
Modül kapısı yok (finans çekirdeği; `docs/12` K-SAT). Okuma `expenses.read`. "Alıcı" =
`expenses.read` + (`requests.assign` ya da `expenses.manage`). Listeler sayfalı; miktar **metin**
(`"2.5"`, en çok 3 ondalık), para metin.

| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/vendors?q=&status=` | `expenses.read` | R | sayfalı `{id, name, tax_no, phone, email, iban_masked ("TR33 •••• •••• 1326"), category, status: active\|passive\|blacklist, note, rating_avg ("4.5"), rating_count, order_count}`; IBAN hiçbir okumada açık dönmez |
| POST · PATCH | `/sites/{slug}/vendors[/{id}]` | `expenses.manage` | R | `{name 2–120, tax_no (10 VKN / 11 TCKN, site içinde tekil → 422 fields.tax_no), phone?, email?, iban? (TR + mod-97), category?, status, note?}`; düzenlemede boş IBAN mevcut olanı korur |
| GET · PUT | `/sites/{slug}/purchase-settings` | okuma `expenses.read`, yazma `expenses.manage` | R | `{thresholds: [{max, quotes}], above_quotes, tolerance_percent ("1.00"), link_days}`; varsayılan < 10.000 → 1, < 100.000 → 2, üstü 3; %1; 7 gün |
| GET | `/sites/{slug}/purchase-budget?category_id=` | `expenses.read` | R | `{has_budget, annual, spent, committed, remaining}` — yürürlükteki işletme projesi kalemleri; bu yılın gerçekleşen gideri; onay bekleyen/onaylı talepler (seçilen teklif tutarı) + açık siparişlerin ödenmemiş kısmı |
| GET | `/sites/{slug}/purchase-requests?status=` | `expenses.read` | R | sayfalı satır `{id, number "ST-2026-0001", title, category_name, over_budget, estimated_total, quote_count, required_quotes, status, order_number, …}` |
| GET | `/sites/{slug}/purchase-requests/{id}` | `expenses.read` | R | kalemler, bütçe anlık görüntüsü, teklifler (`lines[].unit_price`, `source: staff\|vendor_link`, `file_id`, puan), linkler (`id, vendor_name, expires_at, used_at, state: open\|used\|expired` — belirteç dönmez), kararlar (`stage: exception\|purchase\|order`), `approval` (açık onay talebi), `order_id/number` |
| POST | `/sites/{slug}/purchase-requests` | alıcı | R | `{title, reason, category_id, items: [{description, qty, unit?, est_unit_price, stock_item_id?}]}`; 422 `fields.items.{k}.qty` … |
| POST | `/sites/{slug}/purchase-requests/{id}/quotes` | alıcı | R | `{vendor_id, lines: [{unit_price}], delivery_days, payment_terms, valid_until, note?}`; yalnız `quoting` (409 `invalid_status`); pasif/kara liste ya da ikinci teklif 422 `fields.vendor_id`; toplam kalemlerden |
| POST | `/sites/{slug}/purchase-requests/{id}/quote-links` | alıcı | R | `{vendor_id}` → `{token, expires_at, vendor_name, vendor_email}` (belirteç yalnız bu yanıtta; e-posta K5); açık link 409 `link_open`, teklif var 409 `quoted` |
| GET · POST | `/public/quote-links/{token}` | **herkese açık** | R | IP başına 30/5 dk⁻¹; bilinmeyen 404, kullanılmış/süresi dolmuş/talep kapanmış 410 `link_closed`. GET yalnız `{site_name, vendor_name, number, title, items: [{description, qty, unit}], expires_at}`; POST teklif alanları + `terms_ack` |
| POST | `/public/quote-links/{token}/file` | **herkese açık** | R | multipart `file`; gönderimden sonraki 1 saat, bir kez (410) |
| POST | `/sites/{slug}/purchase-requests/{id}/select-quote` | alıcı | R | `{quote_id, exception_reason?}`; eksik teklif → gerekçe ≥ 10 (422 `quote_exception` + `fields.exception_reason`) → `exception_pending`; yeterli → `approval_pending`. Onay kuralı varsa onay talebi açılır (`approval`) |
| POST | `/sites/{slug}/purchase-requests/{id}/decide` | onay kuralı varsa onaycı; yoksa `expenses.manage` | R | `{decision: approve\|reject, note}`; talep eden 403 `self_approval`; ret gerekçesi 422 `fields.note`; kural varsa karar onay mekanizmasına iletilir (403 `not_an_approver`) |
| POST | `/sites/{slug}/purchase-requests/{id}/cancel` | alıcı (açan ya da `expenses.manage`) | R | açık linkler kapanır, bekleyen onay talebi geri çekilir; sipariş/ret/iptal 409 |
| GET | `/sites/{slug}/purchase-orders?status=` · `/{id}` | `expenses.read` | R | ayrıntı: kalemler (`qty, received, invoiced`), kabuller (`lines[].qty`, `rating`, `file_id`), faturalar |
| POST | `/sites/{slug}/purchase-orders` | `expenses.manage` | R | `{pr_id}` → `{status: created\|approval_pending, order, approval_request_id}`; onaysız 409 `not_approved`, tedarikçi pasif 409 `vendor_inactive`; "Sipariş" kuralı varsa **202** |
| POST | `/sites/{slug}/purchase-orders/{id}/receipts` | `expenses.read` + (`requests.assign` \| `inventory.manage` \| `expenses.manage`) | R | `{lines: [{qty}], note?, rating? 1–5}`; kalandan fazlası 422 `fields.lines.{k}`; stoklu kalem **sistem adına** stoğa girer (stok yetkisi aranmaz) |
| POST | `/sites/{slug}/purchase-quotes/{id}/file` · `/purchase-receipts/{id}/file` · `/vendor-invoices/{id}/file` | alıcı · kabul · `expenses.manage` | R | multipart `file` (PDF/JPG/PNG/WEBP ≤ 10 MB, içerik imzası); kayda bir kez (409 `file_exists`); indirme `GET …/files/{id}` |
| GET | `/sites/{slug}/vendor-invoices?status=` | `expenses.read` | R | sayfalı `{…, lines, total, expected_total, diffs[], status: matched\|diff_pending\|disputed\|paid, decision, approval_request_id, file_id, paid_on, expense_id, overdue}` |
| POST | `/sites/{slug}/vendor-invoices` | `expenses.manage` | R | `{order_id, number, date, due_date, lines: [{qty, unit_price}]}`; numara büyük harf, tedarikçide tekil; teslim alınmamış (faturalanmamış) miktar yoksa 409 `no_receipt`; bekleyen fark varken 409 `invoice_pending`; eşleşme: miktar tam, toplam ± tolerans |
| POST | `/sites/{slug}/vendor-invoices/{id}/decide` | kural varsa onaycı; yoksa `expenses.manage` | R | `{decision, note (≥ 5)}`; faturayı giren 403 `self_approval`; onay → `matched`, ret → `disputed` |
| GET | `/sites/{slug}/payables` | `expenses.read` | R | `{invoices (eşleşmiş, vadeye göre, overdue), direct_expenses {count, total}, total}` |
| POST | `/sites/{slug}/vendor-invoices/{id}/mark-paid` | `expenses.manage` | R | `{paid_on, cash_account_id}` + **Idempotency-Key**; aynı işlemde gider (talebin kategorisi, tedarikçi, belge no, fatura görüntüsü) + hesaptan çıkış (`expenses.create(paid=True)`); eşleşmemiş/ödenmiş 409 `not_matched`; dönem kapalı 409 `period_closed` |

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
