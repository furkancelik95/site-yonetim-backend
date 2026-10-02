#!/usr/bin/env bash
# Codespace ilk kurulduğunda bir kez: araçlar, Python bağımlılıkları, frontend klonu.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONT="/workspaces/site-yonetim-frontend"
FRONT_REPO="https://github.com/furkancelik95/site-yonetim-frontend.git"
UV_VERSION="0.12.21" # CI ile aynı (.github/workflows/ci.yml)

echo "==> uv $UV_VERSION"
curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | sh
export PATH="$HOME/.local/bin:$PATH"

echo "==> Python bağımlılıkları (uv.lock)"
cd "$ROOT"
uv sync --locked

echo "==> Frontend"
if [ -d "$FRONT/.git" ]; then
  git -C "$FRONT" pull --ff-only || echo "Uyarı: frontend güncellenemedi; mevcut kopya kullanılacak."
elif ! git clone "$FRONT_REPO" "$FRONT"; then
  echo "Uyarı: frontend klonlanamadı. Codespace oluşturulurken frontend reposuna okuma izni"
  echo "verilmeli (bkz. .devcontainer/README.md). API yine de çalışır."
  exit 0
fi
cd "$FRONT"
npm ci --no-audit --no-fund
# Codespaces'e özel Vite ayarı (start-demo.sh yazar) frontend reposunda görünmesin.
grep -qx "vite.codespaces.config.ts" .git/info/exclude 2>/dev/null \
  || echo "vite.codespaces.config.ts" >>.git/info/exclude
