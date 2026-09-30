# 02 — Mimari

Bu dokümanda iki tür madde var:
- **İLKE** — tartışmaya kapalı. Hangi framework seçilirse seçilsin uygulanır.
- **ÖNERİ** — Furkan'ın kararı. Farklı bir şey seçilirse bu doküman güncellenir.

---

## 1. Yığın (ÖNERİ)

| Katman | Öneri | Neden |
|---|---|---|
| Web framework | **FastAPI** | API-first; OpenAPI şemasını kendisi üretir, frontend buradan okur |
| Doğrulama / şema | **Pydantic v2** | FastAPI ile doğal; `Decimal` desteği tam |
| ORM | **SQLAlchemy 2.x** (async) | Olgun; PostgreSQL özelliklerine (RLS, partition) erişim |
| Göç (migration) | **Alembic** | Şema değişikliği her zaman göçle, elle `ALTER` yok |
| Veritabanı | **PostgreSQL 16+** | RLS, partition, `NUMERIC`, güçlü `GROUP BY` |
| Test | **pytest** + `pytest-asyncio` | |
| Parola | **argon2** (`argon2-cffi`) veya bcrypt | |
| Arka plan işleri | Başta basit bir iş kuyruğu (ör. **arq** veya **Celery + Redis**) | Tahakkuk koşusu, gece gecikme hesabı, bildirim |
| Excel | **openpyxl** | İçe/dışa aktarım |
| Dosya depolama | Yerel disk (geliştirme), S3 uyumlu nesne deposu (üretim) | |

Django + DRF da uygundur; seçilirse aynı ilkeler geçerli.

## 2. Katmanlar (İLKE)

```
api/            HTTP: yönlendirme, istek/yanıt şeması, kimlik ve yetki kontrolü
  └── çağırır ↓
services/       Uygulama servisleri: bir işlemi uçtan uca yürütür (transaction burada açılır)
  └── çağırır ↓
domain/         SAF iş kuralları: tahakkuk motoru, para dağıtımı, mahsup, gecikme hesabı
                → veritabanına, HTTP'ye, saate DOKUNMAZ. Girdi alır, çıktı verir.
  ↑ kullanır
repositories/   Veritabanı erişimi (SQLAlchemy). Site kapsamını burada uygular
models/         SQLAlchemy modelleri
```

**En önemli kural:** `domain/` saf Python'dur. İçinde `import sqlalchemy`, `import fastapi`,
`datetime.now()` olmaz. Tarih gerekiyorsa parametre olarak gelir. Böylece iş kuralları
veritabanı olmadan, milisaniyeler içinde test edilir. `07-test-senaryolari.md`'deki "altın
testler" bu katmanı test eder.

Referans uygulamada bu ayrım şöyleydi ve iyi çalıştı:
- `domain/money.py` ← `Money.Distribute`, `Money.Round`
- `domain/charging/engine.py` ← `ChargeEngine.BuildPreview`
- `domain/charging/payment_allocator.py` ← `PaymentAllocator.Allocate`
- `domain/charging/late_fee.py` ← `LateFeeCalculator`
- `domain/imports/unit_validator.py` ← `UnitImportValidator`

## 3. Çok kiracılılık (İLKE)

**Tek veritabanı, her kiracı tablosunda `site_id` sütunu.** Bu karar verildi; ayrıntılı gerekçe
`08-performans.md` §4.

Kiracı tabloları ve global tablolar `03-veri-modeli.md` içinde ayrı işaretli.

İzolasyon **iki katmanda** uygulanır, biri atlanırsa diğeri tutar:

1. **Uygulama katmanı:** her istekte site bağlamı bir kez çözülür (bkz. §4) ve repository
   katmanı her sorguya `WHERE site_id = :current_site` ekler. SQLAlchemy'de bunun için
   `with_loader_criteria` olayı ya da ortak bir taban repository kullanılır. Geliştirici tek tek
   filtre yazmayı **unutamamalı** — filtre varsayılan olmalı, kapatmak için açık bir çağrı gerekmeli.
2. **Veritabanı katmanı — PostgreSQL Row Level Security (RLS):** her kiracı tablosunda
   `USING (site_id = current_setting('app.site_id')::uuid)` politikası. Her istekte bağlantı
   alınınca `SET LOCAL app.site_id = '...'` çalıştırılır. Uygulamada filtre unutulsa bile veritabanı
   başka sitenin satırını döndürmez.

Yazma tarafında da koruma olmalı:
- Yeni kayda geçerli `site_id` **otomatik** damgalanır; istekten gelen `site_id`'ye güvenilmez.
- Başka bir sitenin `site_id`'si ile kayıt eklemek veya başka sitenin kaydını güncellemek
  **istisna fırlatır.**
- Site bağlamı açılmadan kiracı tablosuna yazılamaz.

"Tüm siteler" kapsamı (platform paneli, portföy, gece işleri) **açık ve bilinçli** bir çağrıyla
açılır; varsayılan asla "tüm siteler" değildir.

Bunların her biri için test var: `07-test-senaryolari.md` §6.

## 4. İstek yaşam döngüsü (İLKE)

```
1. Kimlik doğrulama        → kullanıcı kim? (yoksa 401)
2. Site çözümleme          → URL'deki {slug} hangi site? (yoksa 404)
3. Erişim kontrolü         → kullanıcının bu siteye erişimi var mı? (yoksa 404 — 403 DEĞİL)
4. Kapsamı aç              → site_id bağlama yazılır, RLS değişkeni set edilir
5. Modül kontrolü          → uç noktanın modülü sitede açık mı? (kapalıysa 404)
6. İzin kontrolü           → kullanıcının bu işlem için izni var mı? (yoksa 403)
7. İş kuralı               → services/ → domain/
8. Kapsamı kapat
```

3. adımda 403 değil 404 dönmesinin sebebi: yetkisiz biri için site **hiç yok gibi** davranmalı.
6. adımda 403 döner, çünkü kullanıcı siteyi zaten biliyor; sadece o işlemi yapamıyor.

## 5. Finansal işlemler (İLKE)

- **Tek transaction.** Tahakkuk koşusu (yüzlerce `Charge` + `LedgerEntry`), tahsilat
  (`Payment` + `PaymentAllocation` + `LedgerEntry` + `CashMovement`) ya hep birlikte yazılır ya
  hiç yazılmaz.
- **İdempotent.** Aynı istek iki kez gelirse (çift tıklama, ağ tekrarı) ikinci kayıt oluşmaz.
  Para yazan uç noktalar `Idempotency-Key` başlığını kabul eder; aynı anahtarla gelen ikinci
  istek ilk isteğin sonucunu döndürür. Ayrıca iş kuralı düzeyinde de koruma var: aynı döneme
  ikinci tahakkuk kesilemez, aynı koşu iki kez ters kaydedilemez.
- **Değişmez.** Bkz. `04-is-kurallari.md` §8.
- **Bakiye saklanmaz, hareketten hesaplanır** — ama büyük sitede her seferinde toplamak
  yavaştır. Çözüm: hareket yazılırken aynı transaction içinde güncellenen **özet bakiye tablosu**.
  Hareket defteri tek doğruluk kaynağıdır; özet tablo ondan her an yeniden üretilebilir.
  Ayrıntı: `08-performans.md` §2.

## 6. Arka plan işleri

Şunlar HTTP isteği içinde **değil**, arka plan işi olarak çalışır:

| İş | Neden |
|---|---|
| 1.000+ bölümlü sitede tahakkuk kaydı | Uzun sürer; kullanıcı "hazırlanıyor" görür, bitince bildirim alır |
| Gece gecikme tazminatı hesabı | Ay sonunda tek kayıt kesilir (`04` §6) |
| Bildirim gönderimi (SMS/e-posta/push) | Dış servise bağlı, yavaş, başarısız olabilir |
| Büyük Excel dışa aktarımı | |
| Planlı rapor gönderimi | |

Her iş: **idempotent** (iki kez çalışırsa zarar vermez), **site bazında** çalışır, sonucu kayıt
altına alınır, başarısız olursa yeniden denenir.

Önizleme (tahakkuk önizlemesi) HTTP içinde kalır: kullanıcı sonucu hemen görmeli ve hiçbir şey
yazılmaz.

## 7. Yapılandırma (İLKE)

- Her şey ortam değişkeninden: `DATABASE_URL`, `JWT_SECRET`, `FILE_STORAGE_ROOT`, SMS/e-posta
  sağlayıcı anahtarları.
- `env.example` repoda durur (değerler yer tutucu), `.env` asla. Kurulum: `cp env.example .env`.
  Dosyanın adında baştaki nokta **bilinçli olarak yok**: sır tarayıcısı `.env` ile başlayan her
  dosyayı engelliyor; örnek dosya bu yüzden `env.example`.
- Geliştirme, test ve üretim **ayrı kimlik bilgisi** kullanır.
- Demo/seed verisi **yalnızca geliştirme ortamında** yüklenir. Üretimde herkesçe bilinen
  `Demo1234!` parolasıyla giriş yapılabilen hesap oluşmamalı — referans uygulamada bu kontrol
  var, burada da olmalı.

## 8. Hata biçimi, sayfalama, sürüm

`06-api-sozlesmesi.md`.

## 9. Gözlemlenebilirlik

- Her log satırında `site_id`, `user_id`, `request_id`.
- Yavaş sorgu logu (> 300 ms).
- Kişisel veri (telefon, e-posta, TC kimlik) loga **yazılmaz.**

## Referans

`brhnnkaraa6/siteyonetimi`:
- Tenant filtresi ve yazma koruması: `src/SiteYonetimi.Infrastructure/Persistence/AppDbContext.cs`
  (`BuildTenantFilter`, `ApplyTenantGuard`)
- Kapsam nesnesi: `src/SiteYonetimi.Infrastructure/Tenancy/TenantScope.cs`
- İstek yaşam döngüsü: `src/SiteYonetimi.Web/Infrastructure/SiteContext.cs` (`TenantResolutionFilter`)
