# site-yonetim-backend

Site, apartman, iş merkezi ve AVM yönetim platformunun **API ve iş mantığı**.
Çok kiracılı (her site ayrı kiracı), JSON API; web ve mobil arayüzler bu API'yi kullanır.

- **Dil:** Python 3.14
- **Yığın:** FastAPI · SQLAlchemy 2 · Alembic · PostgreSQL · Pydantic v2 · pytest · uv · Docker
  (`docs/02-mimari.md`)
- **Frontend:** [site-yonetim-frontend](https://github.com/furkancelik95/site-yonetim-frontend)

## Hızlı başlangıç

Gerekenler: Python 3.14, [uv](https://docs.astral.sh/uv/), isteğe bağlı Docker.

```bash
uv sync                       # bağımlılıklar (uv.lock'tan)
uv run pre-commit install     # commit öncesi sır taraması, lint, main'e commit engeli
cp env.example .env           # yerel ayarlar — .env repoya girmez

uv run uvicorn site_yonetim.main:app --reload   # http://localhost:8000/api/v1/docs
uv run pytest --cov                              # testler
```

### Veritabanı

PostgreSQL 16. Uygulama **süper kullanıcı olmayan, RLS'e tabi** `site_yonetim_app` rolüyle,
göçler tablo sahibi `site_yonetim_owner` ile çalışır. Roller `docker/postgres/10-roles.sh` ile kurulur.

```bash
docker compose up -d db                 # .env'deki POSTGRES_/DB_*_PASSWORD ile rolleri kurar
docker compose run --rm migrate         # ya da: uv run alembic upgrade head
```

Entegrasyon testleri (ayrı veritabanı):

```bash
docker compose exec -u postgres -e APP_DB_NAME=site_yonetim_test db bash /docker-entrypoint-initdb.d/10-roles.sh
export TEST_DATABASE_URL=postgresql+asyncpg://site_yonetim_app:…@localhost:5432/site_yonetim_test
export TEST_DATABASE_ADMIN_URL=postgresql+asyncpg://site_yonetim_owner:…@localhost:5432/site_yonetim_test
uv run pytest --cov
```

Değişkenler yoksa entegrasyon testleri yerelde atlanır; CI'da atlanmaz.

### Demo verisi (yalnız geliştirme)

`ENVIRONMENT=development` ve `SEED_DEMO_DATA=true` iken uygulama açılışta demo verisini yükler
(idempotent; veri varsa dokunmaz). Elle: `uv run python -m site_yonetim.cli seed-demo`.
Üretimde ve testte **reddedilir**.

| E-posta (parola `Demo1234!`) | Rol | Görür |
|---|---|---|
| `platform@demo.local` | Platform yöneticisi | site verisi yok |
| `yonetici@demo.local` | Kent Yönetim — Sahip | 3 site, portföy |
| `muhasebe@demo.local` | Kent Yönetim — Muhasebe | 3 sitede finans |
| `mimoza@demo.local` | Mimoza — Yönetici | yalnız Mimoza |
| `guvenlik@demo.local` · `denetci@demo.local` · `teknik@demo.local` | Aksu — Güvenlik / Denetçi / Teknik Personel | yalnız Aksu |

Siteler: Aksu Konakları (48 bölüm), Yıldız Sitesi (24), Mimoza Apartmanı (12).
Docker ile API: `docker compose up --build api` → `http://localhost:8000/api/v1/health`
(canlılık) ve `/api/v1/health/ready` (veritabanı dahil hazırlık; ulaşılamazsa 503).

OpenAPI şeması: `GET /api/v1/openapi.json` (frontend tiplerini buradan üretir).

## Codespaces demosu

**Code → Codespaces → Create codespace on main**: PostgreSQL, API, demo verisi ve frontend tek
tıkla açılır, paylaşılabilir bir bağlantı verir (`bash .devcontainer/share.sh public`). Yalnız demo
içindir. Ayrıntı: [`.devcontainer/README.md`](.devcontainer/README.md).

## CI

Her PR'da GitHub Actions: biçim + lint (güvenlik kuralları dahil) + mypy (strict) + bandit,
testler (birim + mimari + PostgreSQL 16 entegrasyon, kapsam ≥ %90), bağımlılık açığı taraması (pip-audit), sır taraması (gitleaks),
Docker imajı derleme + imaj açık taraması + duman testi, CodeQL. `main` korumalıdır:
doğrudan push yok, CI geçmeden PR birleştirilmez.

## Yapay zekâ ile çalışıyorsan

Önce **[AGENTS.md](AGENTS.md)** — kurallar, doküman haritası, çalışma şekli.

## Dokümanlar

| | |
|---|---|
| [01 — Ürün](docs/01-urun.md) | Ne yapıyoruz, kim kullanıyor, modüller, planlar, hukuki çerçeve |
| [02 — Mimari](docs/02-mimari.md) | Katmanlar, çok kiracılılık, istek yaşam döngüsü, arka plan işleri |
| [03 — Veri modeli](docs/03-veri-modeli.md) | Bütün tablolar, alanlar, ilişkiler |
| [04 — İş kuralları](docs/04-is-kurallari.md) | **Para, tahakkuk, tahsilat, gider, kasa — sistemin kalbi** |
| [05 — Yetki](docs/05-yetki.md) | İzinler, roller, giriş, 404/403 |
| [06 — API sözleşmesi](docs/06-api-sozlesmesi.md) | Biçim kuralları ve uç nokta kataloğu |
| [07 — Test senaryoları](docs/07-test-senaryolari.md) | Beklenen değerleriyle altın testler |
| [08 — Performans](docs/08-performans.md) | Ölçek kuralları, ölçülmüş rakamlar |
| [09 — Güvenlik ve KVKK](docs/09-guvenlik-kvkk.md) | İzolasyon, sırlar, dosya yükleme, kişisel veri |
| [10 — Demo verisi](docs/10-demo-veri.md) | Örnek siteler, demo hesapları, site kurulumu |
| [11 — Excel aktarımı](docs/11-excel-aktarim.md) | Daire/sakin içe aktarma kuralları |
| [12 — Açık kararlar](docs/12-acik-kararlar.md) | Henüz karar verilmemiş konular |

## Çalışma şekli

Frontend bir ekran için servis gerektiğinde bu repoda **Issue** açar
(şablon: *Servis isteği*). Servis yazılıp testleri geçince issue kapatılır.

## Referans uygulama

Aynı kurallar daha önce .NET 10 ile yazıldı ve 101 testle doğrulandı:
`brhnnkaraa6/siteyonetimi` (gizli repo). `docs/` kendi başına yeterli olacak şekilde yazıldı.
