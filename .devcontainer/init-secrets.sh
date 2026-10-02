#!/usr/bin/env bash
# Codespace (ya da yerel Dev Container) ilk kurulurken, konteynerler açılmadan önce çalışır.
# Rastgele parolaları bir kez üretir; dosya varsa dokunmaz (veritabanı aynı parolayla kalır).
set -euo pipefail

target="$(dirname "$0")/.env"
[ -f "$target" ] && exit 0

secret() { od -An -tx1 -N24 /dev/urandom | tr -d ' \n'; }

umask 077
cat >"$target" <<EOF
POSTGRES_PASSWORD=$(secret)
DB_OWNER_PASSWORD=$(secret)
DB_APP_PASSWORD=$(secret)
JWT_SECRET=$(secret)$(secret)
EOF
echo "Demo sırları üretildi: $target"
