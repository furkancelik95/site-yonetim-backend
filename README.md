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

Docker ile: `docker compose up --build` → `http://localhost:8000/api/v1/health`.

OpenAPI şeması: `GET /api/v1/openapi.json` (frontend tiplerini buradan üretir).

## CI

Her PR'da GitHub Actions: biçim + lint (güvenlik kuralları dahil) + mypy (strict) + bandit,
testler (kapsam ≥ %90), bağımlılık açığı taraması (pip-audit), sır taraması (gitleaks),
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
