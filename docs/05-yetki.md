# 05 — Kimlik ve yetki

## 1. İlke: rol değil, izin

Kod **izne** bakar, rol adına bakmaz:

```python
# YANLIŞ
if membership.role == "Yönetici": ...
# DOĞRU
if access.can("finance.charge.post"): ...
```

Rol, izinlerin bir kümesidir. Yeni bir rol eklemek kod değişikliği gerektirmemeli, yalnız izin
kümesi tanımlanmalı.

## 2. İzinler

| İzin | Açıklama |
|---|---|
| `finance.read` | Finans görüntüleme (pano, borçlular, cari ekstre, işletme projesi) |
| `finance.charge.post` | Tahakkuk kesme ve ters kaydetme |
| `finance.payment.record` | Tahsilat kaydetme |
| `finance.budget.manage` | İşletme projesi yönetimi |
| `finance.cash.read` | Kasa/banka görüntüleme |
| `finance.cash.manage` | Kasa/banka yönetimi (hesap aç, hareket, aktarım) |
| `finance.reports.read` | Rapor görüntüleme ve Excel |
| `units.read` / `units.manage` | Daire görüntüleme / yönetimi (Excel içe aktarma dahil) |
| `people.read` / `people.manage` | Kişi görüntüleme / yönetimi |
| `requests.read` / `requests.create` / `requests.assign` | Talep görüntüleme / oluşturma / atama ve durum değiştirme |
| `announcements.read` / `announcements.publish` | Duyuru görüntüleme / yayınlama |
| `expenses.read` / `expenses.manage` | Gider görüntüleme / kaydetme, ödeme, geri alma |
| `security.visitors` | Ziyaretçi kaydı |
| `security.packages` | Kargo kaydı |
| `modules.manage` | Modül aç/kapa |
| `members.manage` | Kullanıcı yönetimi |
| `portfolio.read` | Portföy görüntüleme |
| `audit.read` | Denetim kaydı görüntüleme |

## 3. Site rolleri (şablon)

| Rol | İzinler |
|---|---|
| **Yönetici** | `finance.read`, `finance.charge.post`, `finance.payment.record`, `finance.budget.manage`, `finance.cash.read`, `finance.cash.manage`, `finance.reports.read`, `units.read`, `units.manage`, `people.read`, `people.manage`, `requests.read`, `requests.create`, `requests.assign`, `announcements.read`, `announcements.publish`, `expenses.read`, `expenses.manage`, `security.visitors`, `security.packages`, `modules.manage`, `members.manage`, `audit.read` |
| **Yönetim Kurulu Üyesi** — her şeyi görür, hiçbirini değiştirmez | `finance.read`, `units.read`, `people.read`, `requests.read`, `announcements.read`, `expenses.read`, `finance.cash.read`, `finance.reports.read` |
| **Denetçi** (KMK m.41) — salt okunur finans, **kişisel veri görmez** | `finance.read`, `expenses.read`, `audit.read`, `finance.cash.read`, `finance.reports.read` |
| **Muhasebe** | `finance.read`, `finance.charge.post`, `finance.payment.record`, `finance.budget.manage`, `expenses.read`, `expenses.manage`, `finance.cash.read`, `finance.cash.manage`, `finance.reports.read`, `units.read`, `people.read` |
| **Güvenlik** — yalnızca kapıdaki işi. Borç, kişi listesi, muhasebe **yok** | `security.visitors`, `security.packages`, `requests.create`, `announcements.read` |
| **Teknik Personel** | `requests.read`, `requests.assign`, `requests.create`, `announcements.read` |
| **Sakin** | `announcements.read`, `requests.create` + kendi dairesine ait okuma (§6) |

Rol adları Türkçe saklanır (yukarıdaki gibi); kodda sabit olarak tanımlanır.

## 4. Üyelik: iki kaynak

Bir kullanıcının bir sitedeki erişimi iki yoldan gelir:

1. **Açık site üyeliği** — `site_memberships(site_id, user_id, role, person_id?)`
2. **Yönetim şirketi üyeliğinden türetilmiş** — `organization_memberships(organization_id, user_id, role)`:
   kullanıcı, şirketin **bütün sitelerine** aşağıdaki eşlemeyle erişir.

| Şirket rolü | Sitedeki karşılığı |
|---|---|
| `Sahip` | Yönetici |
| `Yönetici` | Yönetici |
| `Muhasebe` | Muhasebe |
| `İzleyici` | Yönetim Kurulu Üyesi |

**Açık üyelik türetilmişi ezer.** Şirketin muhasebecisini tek bir sitede daraltmak için o siteye
açık bir üyelik (ör. Denetçi) eklenir; o sitede türetilmiş rol yerine açık rol geçerlidir.

Pasif üyelik (`is_active = false`) ve pasif kullanıcı hiç erişim vermez.

**Çözümlenmiş erişim** (her istekte bir kez hesaplanır, isteğe bağlanır):
```
UserAccess
  user_id, full_name, kind (staff|resident), is_platform_admin
  sites: [ SiteAccess(site_id, name, slug, role, permissions[], person_id?,
                      is_derived, organization_id?, organization_role?) ]
  can_see_portfolio = len(sites) > 1
```

## 5. Platform yöneticisi

`users.is_platform_admin = true`. Ürünü işleten taraf (Burhan ve ortakları).

- `/api/v1/platform/...` uç noktalarına yalnız o erişir. Başkası **404** alır (panelin varlığı sızmasın).
- **Site üyeliği yoktur.** Hiçbir müşterinin borcunu, sakin bilgisini, yazışmasını görmez; yalnız
  kullanım ölçüsünü görür (site sayısı, bölüm sayısı, plan, tavan).
- Müşteri paneline destek amaçlı girmek **ayrı bir yetki** olacak ve her giriş denetim kaydına
  geçecek — henüz tasarlanmadı (`12-acik-kararlar.md`).

## 6. Sakin

- `users.kind = resident`, sitede `role = Sakin` üyeliği, üyelikte **`person_id` dolu**.
- Sakinin gördüğü her şey `person_id` üzerinden süzülür: kendi cari hesapları, kendi bölümü,
  kendi talepleri, kendisine hedeflenen duyurular.
- **Sakin ekranı kuralı:** bir sakinin bakiyesini yalnızca (a) o sakinin kendisi ya da (b)
  `finance.read` izni olan personel görebilir. Güvenlik görevlisi siteye erişebilir ama sakin
  bakiyesi uç noktasından **403** alır. (Referans uygulamada bu bir sızıntıydı, test eklendi.)

## 7. Yanıt kodları

| Durum | Kod |
|---|---|
| Giriş yapılmamış / oturum düşmüş | **401** |
| Site yok, **ya da kullanıcının o siteye erişimi yok** | **404** — 403 değil |
| Modül sitede kapalı ya da planda yok | **404** |
| Siteye erişimi var ama bu işlem için izni yok | **403** |
| Platform ucu, kullanıcı platform yöneticisi değil | **404** |

## 8. Giriş ve oturum (ÖNERİ — Furkan'ın kararı)

- E-posta + parola. E-posta küçük harfe çevrilir.
- Parola en az 8 karakter; hash `argon2id` (ya da bcrypt).
- **5 hatalı denemede 15 dakika kilit.**
- Web için: kısa ömürlü **erişim jetonu (JWT, 15 dk)** bellekte + **yenileme jetonu httpOnly,
  Secure, SameSite=Lax çerezde.** Jetonu `localStorage`'a koymak XSS'te çalınır.
- Mobil için: aynı JWT, `Authorization: Bearer`.
- Oturum ömrü 8 saat, kayan pencere.
- Giriş sonrası `GET /api/v1/me` → `UserAccess` (§4). Frontend yönlendirmeyi buna göre yapar:
  platform yöneticisi → platform paneli, sakin → sakin ekranı, tek siteli personel → o site,
  çok siteli → portföy.
- Sonra eklenecek: telefonla tek kullanımlık kod (OTP), iki adımlı doğrulama (yöneticiler için).

### 8.1 Uygulama notları (karar: Furkan, 30.09.2026)

- **Kendi kendine kayıt yok.** Kullanıcıyı platform (`POST /platform/customers`) ya da
  `members.manage` izni olan yönetici oluşturur.
- Erişim jetonu: HS256 JWT (`iss=site-yonetim`, `aud=site-yonetim-api`, `sub`, `sid`, `typ=access`).
  Her istekte `sid` oturumunun iptal edilmediği ve kullanıcının aktif olduğu kontrol edilir —
  **çıkış ve hesap pasifleştirme anında etkilidir.**
- Yenileme jetonu: çerez `sy_refresh`, `Path=/api/v1/auth`, httpOnly, SameSite=Lax, üretimde
  Secure. Her `/auth/refresh`'te döner; eski jeton tekrar gelirse oturum iptal edilir.
  Çerezle çalışan uçlar `Origin` başlığı varsa `CORS_ORIGINS` dışını 403 ile reddeder (CSRF).
- Yanlış parola ile bilinmeyen e-posta **aynı** yanıtı (401, "E-posta veya parola hatalı.") ve
  yaklaşık aynı süreyi alır. Pasif hesap da aynı yanıtı alır. Kilitli hesap 429.
- Kilit sayacı paralel denemelerle atlatılamaz (kullanıcı satırı `FOR UPDATE`).
- **Pasif açık üyelik** o sitedeki türetilmiş (şirket) erişimini de kapatır — bir sitedeki
  erişimi kaldırmanın güvenilir yolu.
- **Platform yöneticisinin** kayıtta site üyeliği olsa bile yok sayılır (§5).
- **IP bazlı hız sınırı** (paylaşılan depo PostgreSQL, `login_throttle`): bir adresten 15 dakikada
  20 hatalı deneme (`LOGIN_IP_MAX_FAILURES`, `LOGIN_IP_WINDOW_MINUTES`) → 429
  `too_many_attempts`, parola denenmez. Parola püskürtmeye (çok hesaba az deneme) karşı; hesap
  kilidinin yanında ikinci katman. Adresin sayaç satırı giriş boyunca kilitlenir — aynı
  adresten girişler sıraya girer, paralel denemeler sınırı aşamaz. Yalnız hatalı deneme
  sayılır; başarılı giriş sayacı **sıfırlamaz** (geçerli bir hesapla araya giriş sokarak sınır
  atlatılamaz). Ters vekil arkasında `FORWARDED_ALLOW_IPS` verilmezse bütün istekler vekilin
  adresinden gelmiş sayılır.
- **Geçici parola:** platformun açtığı ilk yetkili `must_change_password` ile başlar. Değiştirene
  kadar yalnız `/me`, `/auth/*` ve `POST /auth/change-password` çalışır; diğer her uç 403
  `password_change_required` (varsayılan kapı `password_changed`, `tests/architecture`).
  Değişiklikte mevcut parola doğrulanır (yanlışsa hesap kilidi sayacı artar), diğer oturumlar
  iptal edilir.
- Henüz yok: mobil için gövdeyle yenileme, OTP/2FA, parola sıfırlama (e-posta altyapısı yok).

## 9. Demo hesapları

`10-demo-veri.md`.

## Referans

`brhnnkaraa6/siteyonetimi`:
- İzinler ve roller: `src/SiteYonetimi.Domain/Access/Permissions.cs`
- Erişim çözümleme: `src/SiteYonetimi.Infrastructure/Identity/AccessService.cs`
- İstek filtresi (404/403): `src/SiteYonetimi.Web/Infrastructure/SiteContext.cs`
- Platform yöneticisi: `src/SiteYonetimi.Web/Controllers/PlatformController.cs` (`RequirePlatformAdminAttribute`)
