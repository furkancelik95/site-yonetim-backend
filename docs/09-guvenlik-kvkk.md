# 09 — Güvenlik ve KVKK

Bu sistem aidat tahsilatı ve sakin kişisel verisi işliyor; güvenlik açığı hem para hem hukuk
sorunu demek. Canlıya çıkmadan önce bağımsız bir güvenlik denetimi yapılacak.

## 1. Kiracı izolasyonu

`02-mimari.md` §3 (iki katman: uygulama filtresi + PostgreSQL RLS) ve `07-test-senaryolari.md` §6.
En ağır hata türü budur: bir yönetim şirketinin, başka bir şirketin sakinlerinin borcunu görmesi.

- İstekten gelen `site_id` asla kullanılmaz; site her zaman URL'deki `slug` + kullanıcının
  erişimi üzerinden çözülür.
- Erişim yoksa **404**.
- "Tüm siteler" kapsamı yalnız açıkça çağrılan yerlerde: platform paneli, portföy, gece işleri.

## 2. Sırlar

- Bağlantı dizesi, JWT anahtarı, SMS/e-posta/ödeme sağlayıcı anahtarları **ortam değişkeninde.**
- `.env` repoya girmez (`.gitignore`'da var). `env.example` yer tutucularla repoda durur.
- Parolalar `argon2id`/bcrypt hash'i ile saklanır; düz metin ya da geri çözülebilir şifreleme yok.
- **Her commit öncesi** staged değişiklik sır açısından taranır. Öneri: `gitleaks` ya da
  `detect-secrets` + pre-commit ve **GitHub Actions'ta da** (yerel kanca atlanabilir, CI atlanamaz).
- Geliştirme ve üretim **ayrı** kimlik bilgisi kullanır.
- Demo verisi ve `Demo1234!` parolalı hesaplar **yalnız geliştirme ortamında** oluşur.

## 3. Dosya yükleme (fatura, fiş, dekont)

| Kural | Neden |
|---|---|
| Uzantı beyaz listesi: `.pdf .jpg .jpeg .png .webp` | |
| **İçerik imzası doğrulanır** — uzantıya güvenilmez | `fatura.pdf` adlı bir HTML/JS dosyası reddedilmeli |
| İmzalar: PDF `25 50 44 46` (`%PDF`) · JPEG `FF D8 FF` · PNG `89 50 4E 47` · WEBP `52 49 46 46` (`RIFF`) | |
| En fazla **10 MB**; beyan edilen boyut **ve** gerçek okunan boyut kontrol edilir | istemci boyut hakkında yalan söyleyebilir |
| Disk adı üretilir: `{site_id}/{file_id}.{uzantı}` — kullanıcının verdiği ad **kullanılmaz** | `../../` yol saldırısı ve tahmin |
| Kullanıcının adı yalnız indirme başlığında, temizlenmiş: yol ayıraçları ve geçersiz karakterler `_`, en fazla 120 karakter | |
| Dosyalar **web kökünün / statik dosya klasörünün dışında** | adresi bilen başka sitenin faturasını indiremesin |
| İndirme her zaman bir uç noktadan, **yetki ve site kontrolüyle** | |
| Diskte okunan yol, depo kökünün içinde mi kontrol edilir | |
| Reddedilen dosyadan diskte **iz kalmaz** | |
| SHA-256 saklanır | aynı belgenin iki kez yüklenmesi görülebilir |
| Üretimde virüs taraması (ör. ClamAV) — **yapılacak** | |

**Uygulama (Python backend):** `domain/files.py` (uzantı + imza + boyut, ad temizleme) ve
`services/files.py` (kök dışına çıkış reddi, `O_EXCL` ile üzerine yazmama, izin 0600).
Kök `FILE_STORAGE_ROOT`; Docker'da salt okunur kök dosya sisteminde yazılabilir tek birim
`files-data:/data/files`. Gider kaydı reddedilirse ya da transaction geri alınırsa yazılmış dosya
silinir. Gövde sınırı gider ucu için 10 MB + pay (§3.2).

### 3.1 Excel içe aktarma dosyası (`11-excel-aktarim.md`)

| Kural | Neden |
|---|---|
| Uzantı `.xlsx` + içerik imzası ZIP (`50 4B 03 04`); en fazla **5 MB** | |
| Açılmış boyut ≤ 50 MB, arşiv girdisi ≤ 500; `xl/workbook.xml` yoksa ret | zip bombası |
| XML `defusedxml` ile ayrıştırılır (openpyxl kurulu olunca kendisi kullanır; testle korunur) | XXE, varlık bombası |
| Yalnız saklanmış hücre değeri okunur; formül, dış bağlantı, makro yok sayılır | |
| Ayrıştırma iş parçacığında (olay döngüsü bloke olmaz); en fazla 5.000 satır, 60 sütun | |
| Geçici dosya adı sunucu üretir (`{site_id}/{user_id}/{uuid}.xlsx`, izin 0600); 6 saat sonra silinir | |

### 3.3 Excel çıktıları — formül enjeksiyonu

Dışa aktarılan Excel'lerde kullanıcıdan gelen her metin (açıklama, tedarikçi, ad…) **metin
hücresi** olarak yazılır (`services/exports.py`). `=HYPERLINK(…)`, `+`, `-`, `@` ile başlayan bir
değer formül olarak çalışmaz. Para `Decimal` olarak yazılır (`#,##0.00`), float kullanılmaz.

### 3.2 İstek gövdesi sınırı

Tüm istekler **1 MB** ile sınırlı; Excel yükleme ucu 5 MB + çok parçalı pay. Sınır ara katmanda,
gövde okunurken uygulanır (`Content-Length` yoksa da sayılır) → **413**. Starlette çok parçalı
dosyaları sınırsız diske yazdığından, sınır ayrıştırmadan önce olmalı.

## 4. Web güvenliği

- **Herkese açık uçlar** yalnız giriş (`/auth/*`) ve sakin kayıt formu
  (`/public/registration/{code}`); hepsi `tests/architecture/test_routes.py` beyaz listesinde.
  Kayıt formu: kod tahmin edilemez (11 karakter rastgele), yalnız site adı döner, IP başına istek
  sınırı (GET dakikada 30, POST 5 — `rate_limits`, PostgreSQL), başvuru yönetici onayı bekler.
- **HTTPS zorunlu**, HSTS açık.
- **CORS:** yalnız frontend'in alan adı; `*` yok.
- Yenileme jetonu **httpOnly + Secure + SameSite** çerezde; çerezle korunan yazma işlemlerinde
  CSRF koruması.
- **Hız sınırı:** giriş denemesi (5 hatada 15 dk kilit), parola sıfırlama, dosya yükleme.
  Uygulandı: hesap kilidi + IP bazlı giriş sınırı (15 dk'da 20 hatalı deneme → 429, `05` §8.1);
  dosya boyutu sınırı (ara katman). Parola sıfırlama ucu henüz yok.
- **Gerçek istemci adresi:** API ters vekil arkasındaysa uvicorn'a vekilin adresi
  `FORWARDED_ALLOW_IPS` ile verilir (yalnız oradan gelen `X-Forwarded-For` güvenilir). Verilmezse
  hız sınırı ve denetim kaydındaki IP vekilin adresi olur — üretimde zorunlu ayar.
- Hata yanıtında yığın izi (stack trace) **yok**; ayrıntı yalnız sunucu logunda.
- SQL her zaman parametreli (ORM). String birleştirerek SQL **yazılmaz.**
- Frontend'e dönen metinde HTML yok; kullanıcı girdisi olduğu gibi döner, kaçışlamayı frontend yapar.

## 5. KVKK

Sistem kişisel veri işler: ad, soyad, telefon, e-posta, TC kimlik (isteğe bağlı), plaka,
ziyaretçi bilgisi, borç bilgisi.

| Konu | Kural |
|---|---|
| **Veri minimizasyonu** | Güvenlik görevlisi daire aramasında yalnız bölüm ve oturan adını görür; telefon ve borç **görmez**. Denetçi finans görür, kişisel veri görmez |
| **TC kimlik** | Zorunlu değil. Tutulursa **şifreli** (`national_id_encrypted`); anahtar ortam değişkeninde; ekranda maskeli (`123******78`) |
| **Loglar** | Telefon, e-posta, TC kimlik loga yazılmaz |
| **Aydınlatma metni** | Kişisel veri toplanan her ekranda bağlantı. Onay kutusu gerekmez |
| **Açık rıza** | İsteğe bağlı; işlem rızaya bağlanmaz. Verildiyse **sürümlü** kaydedilir: `consents(person_id, kind, text_version, granted_at, revoked_at)` |
| **Ticari ileti izni** (SMS/e-posta/arama) | Her kanal ayrı ve isteğe bağlı. İYS'ye bildirilir. Duyuru ve borç bildirimi ticari ileti değildir, izin gerektirmez |
| **İlgili kişi başvurusu** | Kişinin verisini dışa aktarma ve silme talebi — **yapılacak**. Finansal kayıtlar yasal saklama süresince silinmez, kişisel alanlar anonimleştirilir |
| **Saklama süreleri** | Henüz belirlenmedi → `12-acik-kararlar.md` |
| **Barındırma** | Veri Türkiye'de (KVKK m.9) |

## 6. Denetim kaydı (audit log) — uygulandı

Referans uygulamada ayrı bir denetim tablosu yok (değişmez defter ve ters kayıt zinciri var).
Yeni backend'de baştan olmalı:

`audit_log(id, site_id, user_id, action, entity, entity_id, before JSONB, after JSONB, ip, at)`

- Kaydedilecekler: her finansal işlem, yetki/üyelik değişikliği, modül aç/kapa, ayar değişikliği,
  toplu içe aktarma, dosya indirme (kişisel veri içeriyorsa).
- Tek bir yerden yazılsın (SQLAlchemy olayı / servis katmanı ortak fonksiyonu) — her uç noktaya
  elle eklenmesin.
- `audit.read` izni olan görür (Yönetici, Denetçi). Kayıt değiştirilemez ve silinemez.

**Uygulama** (`models/audit.py`, göç 0012, uç `GET /sites/{slug}/audit` — `06` §2.15):
- Oturum olayı (`after_flush`): denetlenen tablolardaki (`AUDITED_TABLES`: tahakkuk koşusu,
  tahsilat, gider, kasa hesabı/hareketi, bütçe planı/kalemi, dönem, borç türü, dağıtım kuralı,
  gecikme politikası, modül, site üyeliği) her ekleme/değişiklik/silme **aynı flush ve aynı
  transaction'da** yazılır — işlem geri alınırsa kaydı da geri alınır. Güncellemede yalnız
  değişen alanlar (`before`/`after`).
- İş olayı düzeyinde: koşunun ürettiği borç/defter satırları (türetilmiş, zaten değişmez)
  ayrıca yazılmaz. Toplu aktarım (`import`) ve belge indirme (`download`) model değişikliği
  olmadığı için `record()` ile tek satır.
- Kim: istekte `current_user` aktörü (kullanıcı, ad, IP) bağlama koyar; istek dışı (demo, CLI)
  `user_id` boş.
- Değişmezlik: `audit_log_immutable` tetikleyicisi (`forbid_history_change`), RLS ile site
  izolasyonu.
- IP, uygulamaya gelen bağlantının adresidir; ters vekil arkasında doğru istemci adresi için
  vekil başlıklarına güven ayarı gerekir (güvenlik sertleştirme işi).

## Referans

`brhnnkaraa6/siteyonetimi`:
- Dosya deposu: `src/SiteYonetimi.Infrastructure/Files/FileStorage.cs`
- Güvenlik ekranının kısıtlı daire araması: `src/SiteYonetimi.Web/Controllers/SecurityController.cs`
- Demo verisinin yalnız geliştirmede kurulması: `src/SiteYonetimi.Web/Program.cs`
