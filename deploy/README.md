# Sunucu demosu

Site Yönetim demosu Turhost VPS'te (Türkiye) çalışır. Aynı sunucuda Müdavim de var; ikisi
ayrı Docker yığınlarıdır, yalnız HTTPS'i veren Caddy ortaktır.

```
İnternet ──443──▶ Müdavim web (Caddy, TLS)
                    ├─ 37-230-105-210.sslip.io               → Müdavim
                    └─ site-yonetim.37-230-105-210.sslip.io  ─edge ağı─▶ site-yonetim web (Caddy :8080)
                                                                           ├─ /api → api:8000 → db
                                                                           └─ /    → frontend derlemesi
```

- Adres: **https://site-yonetim.37-230-105-210.sslip.io**. `sslip.io` IP'yi alan adına
  çevirir; gerçek alan adı alınınca `bash kurulum.sh <alan-adı>` yeterli.
- Demo: `ENVIRONMENT=development` + `SEED_DEMO_DATA=true` → açılışta demo verisi, giriş
  ekranında demo hesapları (parola `Demo1234!`). **Gerçek veri girilmez.** Gerçek kullanımda
  `api.env` içinde `ENVIRONMENT=production`, `SEED_DEMO_DATA=false`.
- Dışarı hiçbir port açılmaz; API ve veritabanı iç ağda. API yalnız RLS'e tabi rolle bağlanır,
  tablo sahibi parolası API'ye verilmez (`.env` compose için, `api.env` yalnız API için).
- Gece işleri (otomatik tahakkuk, tekrarlanan gider, mutabakat, temizlik) bu demoda zamanlanmaz.

## Kurulum ve güncelleme

Bilgisayardan (SSH anahtarı: `~/.ssh/mudavim_deploy`):

```bash
git -C backend archive --format=tar.gz -o /tmp/site-yonetim-backend.tar.gz <dal-ya-da-main>
git -C frontend archive --format=tar.gz -o /tmp/site-yonetim-frontend.tar.gz main
scp -P 23422 -i ~/.ssh/mudavim_deploy /tmp/site-yonetim-*.tar.gz root@37.230.105.210:/root/
ssh -p 23422 -i ~/.ssh/mudavim_deploy root@37.230.105.210 \
  'tar -xzOf /root/site-yonetim-backend.tar.gz deploy/kurulum.sh | bash -s site-yonetim.37-230-105-210.sslip.io'
```

Betik tekrar çalıştırılabilir: kodu temiz açar, sırları (`/opt/site-yonetim/.env`,
`api.env`, ilk kurulumda sunucuda üretilir) ve veritabanını korur, göçleri uygular.

## Ortak Caddy (Müdavim)

`kurulum.sh` Müdavim'in Caddy'sine şunu ekler; Müdavim'in kendi dosyalarına dokunmaz:

- `/opt/mudavim/deploy/docker-compose.override.yml` (`mudavim-override.yml`'den): Caddy'yi
  `edge` ağına bağlar ve birleşik yapılandırmayla başlatır.
- `/opt/site-yonetim/edge/Combined.Caddyfile` (`edge.Caddyfile`): Müdavim'in Caddyfile'ını
  içeri alır, site yönetimi alan adını ekler. Önce `caddy validate` ile denetlenir.
- `/opt/edge/sites/*.caddy`: aynı sunucudaki diğer uygulamaların (ör. Sırdaş) alan adı blokları.
  Birleşik dosya her kurulumda baştan yazıldığı için onların blokları bu klasörde durur;
  `edge.Caddyfile` sonundaki `import` alır, override klasörü salt okunur bağlar. Bir uygulama
  kendi dosyasını yazıp `docker exec restoran-panel-web-1 caddy reload …` çalıştırır.

Müdavim yeniden kurulduğunda override kendiliğinden geçerli kalır. Site yönetimi kaldırılırsa
önce override silinip `docker compose up -d web` (Müdavim klasöründe) çalıştırılmalı.

## İşletme

```bash
cd /opt/site-yonetim/backend/deploy
docker compose ps
docker compose logs -f api
docker compose down          # durdur (veri kalır); `down -v` veriyi de siler
```
