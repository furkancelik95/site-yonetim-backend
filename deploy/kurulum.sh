#!/usr/bin/env bash
# Site Yönetim sunucu demosu — kurulum ve güncelleme (Ubuntu 24.04, Müdavim ile aynı sunucu).
# Kullanım: bash kurulum.sh <ALAN_ADI>     ör. site-yonetim.37-230-105-210.sslip.io
#
# Önce iki kod paketi /root'a yüklenir (bkz. deploy/README.md):
#   /root/site-yonetim-backend.tar.gz   /root/site-yonetim-frontend.tar.gz
# Sunucu sertleştirmesi (SSH yalnız anahtar, ufw, fail2ban, Docker) Müdavim kurulumunda yapıldı;
# HTTPS'i Müdavim'in Caddy'si verir. Tekrar çalıştırılabilir: sırlar ve veriler korunur.
set -euo pipefail
DOMAIN="${1:?Kullanım: bash kurulum.sh <ALAN_ADI>}"
DIR=/opt/site-yonetim
MUDAVIM=/opt/mudavim/deploy

command -v docker >/dev/null || { echo "Docker yok (önce Müdavim kurulumu)"; exit 1; }
[ -f "$MUDAVIM/docker-compose.yml" ] || { echo "Ortak Caddy (Müdavim) bulunamadı: $MUDAVIM"; exit 1; }

# 1) Kod: her seferinde temiz açılır (silinen dosya geride kalmasın); sırlar $DIR'de durur.
mkdir -p "$DIR"
rm -rf "$DIR/backend.new" "$DIR/frontend.new"
mkdir -p "$DIR/backend.new" "$DIR/frontend.new"
tar -xzf /root/site-yonetim-backend.tar.gz -C "$DIR/backend.new"
tar -xzf /root/site-yonetim-frontend.tar.gz -C "$DIR/frontend.new"
rm -rf "$DIR/backend" "$DIR/frontend"
mv "$DIR/backend.new" "$DIR/backend"
mv "$DIR/frontend.new" "$DIR/frontend"

# 2) Sırlar (yalnız ilk kurulumda; bu sunucuda üretilir, hiçbir yere gönderilmez).
#    .env → compose (veritabanı parolaları); api.env → yalnız API.
rnd() { openssl rand -hex 32; }
if [ ! -f "$DIR/.env" ]; then
  umask 077
  cat >"$DIR/.env" <<EOF
DOMAIN=$DOMAIN
POSTGRES_PASSWORD=$(rnd)
DB_OWNER_PASSWORD=$(rnd)
DB_APP_PASSWORD=$(rnd)
EOF
  # Demo: herkesçe bilinen Demo1234! parolalı hesaplar yalnız ENVIRONMENT=development'ta
  # kurulur (docs/09 §2). Bu sunucu gerçek veri tutmaz; gerçek kullanımda production.
  cat >"$DIR/api.env" <<EOF
ENVIRONMENT=development
SEED_DEMO_DATA=true
JWT_SECRET=$(rnd)
LOG_LEVEL=INFO
# Demo: yönetici hesaplarında 2FA kurulumu uyarıyla istenir, süre dolup erişim kapanmaz.
MFA_GRACE_DAYS=180
EOF
  umask 022
fi
sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" "$DIR/.env"
ln -sf "$DIR/.env" "$DIR/backend/deploy/.env"
ln -sf "$DIR/api.env" "$DIR/backend/deploy/api.env"

# 3) Ortak ağ: ortak Caddy ↔ bu yığının web servisi
docker network inspect edge >/dev/null 2>&1 || docker network create edge >/dev/null

# 4) Derle, veritabanı, göçler, uygulama (API açılışta demo verisini kurar; önce göç).
cd "$DIR/backend/deploy"
# `migrate` profilli (tools): düz `build` onu derlemez, göç eski kodla çalışır.
docker compose --profile tools build
docker compose up -d db
docker compose run --rm migrate
docker compose up -d api
docker compose up -d --force-recreate web  # Caddyfile yeni kod klasöründen

# 5) Ortak Caddy'ye alan adını ekle — yalnız birleşik yapılandırma geçerliyse. Yapılandırma
#    kod klasörünün dışında, sabit yerde ve yerinde güncellenir (bağlı dosya kopmasın).
mkdir -p /opt/edge/sites  # diğer uygulamaların blokları; doğrulamaya onlar da girer
MUDAVIM_DOMAIN=$(grep -E '^DOMAIN=' "$MUDAVIM/.env" | cut -d= -f2-)
MUDAVIM_EMAIL=$(grep -E '^ACME_EMAIL=' "$MUDAVIM/.env" | cut -d= -f2-)
docker run --rm --network none \
  -e DOMAIN="$MUDAVIM_DOMAIN" -e ACME_EMAIL="$MUDAVIM_EMAIL" -e SITE_YONETIM_DOMAIN="$DOMAIN" \
  -v "$DIR/backend/deploy/edge.Caddyfile:/etc/caddy/Combined.Caddyfile:ro" \
  -v /opt/edge/sites:/etc/caddy/sites:ro \
  restoran-panel-web caddy validate --config /etc/caddy/Combined.Caddyfile --adapter caddyfile
mkdir -p "$DIR/edge"
cat "$DIR/backend/deploy/edge.Caddyfile" >"$DIR/edge/Combined.Caddyfile"
override="$MUDAVIM/docker-compose.override.yml"
new=$(sed "s|__SITE_YONETIM_DOMAIN__|$DOMAIN|" "$DIR/backend/deploy/mudavim-override.yml")
if [ ! -f "$override" ] || [ "$(cat "$override")" != "$new" ]; then
  printf '%s\n' "$new" >"$override"
  (cd "$MUDAVIM" && docker compose up -d web) # yalnız ortak Caddy birkaç saniye yeniden başlar
else
  docker exec restoran-panel-web-1 caddy reload --config /etc/caddy/Combined.Caddyfile \
    --adapter caddyfile
fi

# 6) Doğrula (sertifika ilk açılışta birkaç saniye sürebilir)
for _ in $(seq 1 30); do
  curl -fsS "https://$DOMAIN/api/v1/health/ready" >/dev/null 2>&1 && break
  sleep 3
done
docker compose ps
if ! curl -fsS "https://$DOMAIN/api/v1/health/ready"; then
  echo; echo "Demo açılmadı:"; docker compose logs --tail 30 web api; exit 1
fi
echo
echo "Hazır: https://$DOMAIN  (demo hesapları giriş ekranında; parola Demo1234!)"
