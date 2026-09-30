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
| POST | `/auth/login` | — | R | `{email, password}` → `{access_token, token_type: "bearer", expires_in}` + `sy_refresh` çerezi. Hatalı: 401 `invalid_credentials`; 5 hatada 15 dk kilit: 429 `account_locked` |
| POST | `/auth/refresh` | çerez | Y | gövde yok; çerezle → yeni `access_token` + yeni çerez. Geçersiz/süresi dolmuş: 401 `session_expired`. Frontend `credentials: "include"` ile çağırır |
| POST | `/auth/logout` | — | R | Bearer ve/veya çerez; oturumu iptal eder, çerezi siler. Her zaman 204 |
| GET | `/me` | giriş | R | `UserAccess` — `05-yetki.md` §4. Frontend yönlendirmesi buna bakar |

### 2.2 Platform (yalnız platform yöneticisi — değilse 404)
| Yöntem | Yol | Durum | Not |
|---|---|---|---|
| GET | `/platform/overview` | R | müşteri sayısı, site sayısı, bölüm sayısı, tavanı aşan siteler, müşteri ve site listeleri |
| GET | `/platform/plans` | R | sayfalı |
| POST | `/platform/customers` | R | `{name, tax_number?, plan_id, admin_full_name, admin_email}` → organizasyon + ilk yetkili (Sahip) + **bir kez gösterilen geçici parola** (`Cache-Control: no-store`). Tek transaction. Aynı e-posta 409 `email_already_exists`, olmayan plan 422 |
| POST | `/platform/sites` | R | `{name, slug?, plan_id, organization_id?, property_kind, city?, district?, iban?, bank_name?}` → site + varsayılan kurulum (`10-demo-veri.md` §3). IBAN TR + mod-97 doğrulanır. Aynı ad (Türkçe büyük/küçük harf duyarsız) ya da slug 409 `site_already_exists` |
| PATCH | `/platform/sites/{id}/plan` | R | `{plan_id}` |

### 2.3 Portföy
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/portfolio` | `portfolio.read` / >1 site | R | site başına: bölüm, tahakkuk, tahsilat, açık bakiye, tahsilat oranı, açık/geciken talep, sağlık durumu |

### 2.4 Site: pano ve yapı
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}` | erişim | R | site bilgisi + açık modüller + kullanıcının o sitedeki izinleri |
| GET | `/sites/{slug}/dashboard` | `finance.read` | R | toplam tahakkuk/tahsilat, bu ay, açık bakiye, borçlu sayısı, ilk 5 borçlu, açık talepler, son duyurular, son giderler. **Özet tablodan**, `08-performans.md` |
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

### 2.6 Finans
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/debtors` | `finance.read` | R | sayfalı; `summary`: toplam açık bakiye, 30+ gün, 60+ gün, ortalama — `04` §13 |
| GET | `/sites/{slug}/accounts/{id}/statement` | `finance.read` veya kendi hesabı | R | cari ekstre: hareketler, bakiye, son tahakkukun kalem dökümü |
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
| POST | `/sites/{slug}/payments` | `finance.payment.record` | R | `{ledger_account_id, amount, date, method, reference?, note?, cash_account_id?}` → `{applied, unapplied, closed_debt_count}` |

### 2.7 Gider
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/expenses` | `expenses.read` | R | sayfalı; filtre `year`, `category_id`, `paid=all\|paid\|unpaid`; `summary`: toplam, ödenmemiş toplam/adet, kategori dağılımı |
| POST | `/sites/{slug}/expenses` | `expenses.manage` | R | multipart: alanlar + `document` (isteğe bağlı) + `paid`, `paid_on`, `cash_account_id` |
| POST | `/sites/{slug}/expenses/{id}/pay` | `expenses.manage` | R | `{cash_account_id, paid_on}` |
| POST | `/sites/{slug}/expenses/{id}/reverse` | `expenses.manage` | R | `{reason}` |
| GET | `/sites/{slug}/expenses/export.xlsx` | `expenses.read` | R | aynı filtreler |
| GET | `/sites/{slug}/files/{id}` | `expenses.read` (belgenin bağlı olduğu kayda göre) | R | |

### 2.8 Kasa ve banka
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/cash-accounts` | `finance.cash.read` | R | hesaplar + bakiye, giren, çıkan; toplam bakiye |
| POST | `/sites/{slug}/cash-accounts` | `finance.cash.manage` | R | `{name, kind, bank_name?, iban?, opening_balance, opening_date?}` |
| GET | `/sites/{slug}/cash-accounts/{id}/statement` | `finance.cash.read` | R | sayfalı, `from`, `to` — `04` §10.5 |
| GET | `/sites/{slug}/cash-accounts/{id}/statement/export.xlsx` | `finance.cash.read` | R | sayfalama yok, tüm aralık |
| POST | `/sites/{slug}/cash-movements` | `finance.cash.manage` | R | elle hareket |
| POST | `/sites/{slug}/cash-transfers` | `finance.cash.manage` | R | `{from_id, to_id, date, amount, note?}` |
| POST | `/sites/{slug}/cash-movements/{id}/reverse` | `finance.cash.manage` | R | `{reason}`; tahsilat/gider kaynaklıysa 409 |

### 2.9 Rapor
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/reports/income-expense?year=2026` | `finance.reports.read` | R | `04` §12 |
| GET | `/sites/{slug}/reports/income-expense/export.xlsx?year=2026` | `finance.reports.read` | R | 3 sayfa: Özet, Kategori, İşletme Projesi |

### 2.10 Talep
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/requests` | `requests.read` | R | sayfalı; filtre durum, kategori, öncelik |
| GET | `/sites/{slug}/requests/{id}` | `requests.read` | R | + olay geçmişi |
| POST | `/sites/{slug}/requests` | `requests.create` | R | numara site içinde artan |
| POST | `/sites/{slug}/requests/{id}/status` | `requests.assign` | R | `{status, resolution?}` — çözüldü/kapandı yapılırken `resolution` zorunlu |
| POST | `/sites/{slug}/requests/{id}/assign` | `requests.assign` | R | `{assignee}` |
| POST | `/sites/{slug}/requests/{id}/comments` | `requests.read` | R | |

### 2.11 Duyuru
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/announcements` | `announcements.read` | R | sabitlenmiş önce, sonra yayın tarihine göre |
| POST | `/sites/{slug}/announcements` | `announcements.publish` | R | `{title, body, importance, audience, audience_block_ids?, channels[], expires_on?, is_pinned}` → hedef kişiler için teslim kayıtları. **Gerçek gönderim: arka plan işi** |

### 2.12 Güvenlik (modül: `visitors`, `packages`)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/packages?status=waiting` | `security.packages` | R | |
| POST | `/sites/{slug}/packages` | `security.packages` | R | 4 haneli teslim kodu üretir, sakine bildirim |
| POST | `/sites/{slug}/packages/{id}/deliver` | `security.packages` | R | `{pickup_code, delivered_to}` |
| GET | `/sites/{slug}/visitors?date=` | `security.visitors` | R | |
| POST | `/sites/{slug}/visitors` | `security.visitors` | R | |
| POST | `/sites/{slug}/visitors/{id}/enter` · `/exit` | `security.visitors` | R | |
| GET | `/sites/{slug}/units/lookup?q=` | `security.*` | R | güvenlik için daire arama: **yalnız bölüm adı ve oturan adı** — borç ve telefon **yok** |

### 2.13 Modüller
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/modules` | `modules.manage` | R | modül, açık mı, planda var mı |
| POST | `/sites/{slug}/modules/{key}/toggle` | `modules.manage` | R | çekirdek (`finance`) kapatılamaz; planda yoksa açılamaz |

### 2.14 Sakin (kendi verisi — `person_id` ile süzülür)
| Yöntem | Yol | İzin | Durum | Not |
|---|---|---|---|---|
| GET | `/sites/{slug}/resident/home` | sakin | R | kendi bölüm(ler)i, bakiye, son hareketler, son duyurular, açık talepleri |
| GET | `/sites/{slug}/resident/statement` | sakin | R | kendi cari ekstresi |
| GET | `/sites/{slug}/resident/announcements` | sakin | R | kendisine hedeflenmiş duyurular |
| GET · POST | `/sites/{slug}/resident/requests` | sakin | R | kendi talepleri / yeni talep |
| GET | `/sites/{slug}/resident/expenses` | sakin | **Y** | sitenin gider dökümü + **fatura görüntüsü**. Şeffaflık vaadinin karşılığı |

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
