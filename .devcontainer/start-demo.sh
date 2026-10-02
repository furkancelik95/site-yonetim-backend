#!/usr/bin/env bash
# Codespace her açıldığında: göçler → API (açılışta demo verisini kurar) → frontend.
# Elle yeniden başlatmak için: bash .devcontainer/start-demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONT="/workspaces/site-yonetim-frontend"
LOGS="/tmp/site-yonetim"
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$LOGS"

# Dışarıdan görünen adres: Codespaces'te https://<codespace>-5173.app.github.dev
if [ -n "${CODESPACE_NAME:-}" ]; then
  PUBLIC_HOST="${CODESPACE_NAME}-5173.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  PUBLIC_URL="https://${PUBLIC_HOST}"
  COOKIE_SECURE=true
else # yerel Dev Container
  PUBLIC_HOST="localhost"
  PUBLIC_URL="http://localhost:5173"
  COOKIE_SECURE=false
fi

# Backend ayarı. Parolalar ve JWT sırrı konteyner ortamından gelir (.devcontainer/.env).
# Demo verisi yalnız ENVIRONMENT=development'ta kurulur (docs/09 §2) — bu bir demo ortamıdır.
cat >"$ROOT/.env" <<EOF
ENVIRONMENT=development
SEED_DEMO_DATA=true
DATABASE_URL=postgresql+asyncpg://site_yonetim_app:${DB_APP_PASSWORD}@db:5432/site_yonetim
DATABASE_ADMIN_URL=postgresql+asyncpg://site_yonetim_owner:${DB_OWNER_PASSWORD}@db:5432/site_yonetim
REFRESH_COOKIE_SECURE=${COOKIE_SECURE}
CORS_ORIGINS=${PUBLIC_URL},http://localhost:5173
ALLOWED_HOSTS=localhost,127.0.0.1,${PUBLIC_HOST}
API_DOCS_ENABLED=false
FILE_STORAGE_ROOT=${ROOT}/var/files
IMPORT_STORAGE_DIR=${ROOT}/var/imports
LOG_LEVEL=INFO
EOF

pkill -f "uvicorn site_yonetim.main:app" 2>/dev/null || true
pkill -f "vite --config vite.codespaces.config.ts" 2>/dev/null || true

echo "==> Göçler"
cd "$ROOT"
uv run alembic upgrade head >"$LOGS/migrate.log" 2>&1 || { cat "$LOGS/migrate.log"; exit 1; }

echo "==> API (127.0.0.1:8000, dışarıya kapalı)"
setsid nohup uv run uvicorn site_yonetim.main:app --host 127.0.0.1 --port 8000 \
  --proxy-headers --no-server-header >"$LOGS/api.log" 2>&1 </dev/null &
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS http://127.0.0.1:8000/api/v1/health/ready >/dev/null \
  || { echo "API açılmadı:"; tail -n 40 "$LOGS/api.log"; exit 1; }

if [ ! -d "$FRONT/node_modules" ]; then
  echo "Frontend kurulu değil (bkz. .devcontainer/README.md). Yalnız API çalışıyor."
  exit 0
fi

echo "==> Frontend (5173; /api istekleri aynı adresten API'ye geçer)"
# Frontend'in kendi vite.config.ts'i + Codespaces adresi ve gerçek istemci IP'si (xfwd).
cat >"$FRONT/vite.codespaces.config.ts" <<EOF
import { defineConfig, mergeConfig } from "vite";
import base from "./vite.config";

export default defineConfig((env) =>
  mergeConfig(typeof base === "function" ? base(env) : base, {
    server: {
      allowedHosts: ["${PUBLIC_HOST}"],
      proxy: { "/api": { xfwd: true } },
    },
  }),
);
EOF
cd "$FRONT"
setsid nohup npx vite --config vite.codespaces.config.ts >"$LOGS/web.log" 2>&1 </dev/null &
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:5173/ >/dev/null 2>&1 && break
  sleep 1
done

cat <<EOF

  Site Yönetim demo hazır:  ${PUBLIC_URL}
  Demo hesapları giriş ekranında listelenir (parola: Demo1234!).

  Bağlantı şu an yalnız SİZE açık. Paylaşmak için:  bash .devcontainer/share.sh public
  Paylaşımı kapatmak için:                          bash .devcontainer/share.sh private
  Kayıtlar: ${LOGS}/{api,web,migrate}.log

EOF
