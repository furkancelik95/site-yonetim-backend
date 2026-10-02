#!/usr/bin/env bash
# Demo bağlantısını herkese aç / kapat:  bash .devcontainer/share.sh public|private
# "public": bağlantıyı bilen herkes giriş ekranını ve demo hesaplarını görür.
set -euo pipefail

# SSH oturumlarında Codespaces değişkenleri ortamda olmayabilir: paylaşılan dosyadan oku.
if [ -z "${CODESPACE_NAME:-}" ] && [ -f /workspaces/.codespaces/shared/.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /workspaces/.codespaces/shared/.env
  set +a
fi

visibility="${1:-}"
case "$visibility" in
  public | private) ;;
  *) echo "Kullanım: bash .devcontainer/share.sh public|private" >&2; exit 2 ;;
esac
if [ -z "${CODESPACE_NAME:-}" ]; then
  echo "Bu komut yalnız GitHub Codespaces içinde çalışır." >&2
  exit 1
fi

url="https://${CODESPACE_NAME}-5173.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
if gh codespace ports visibility "5173:${visibility}" -c "$CODESPACE_NAME" >/dev/null 2>&1; then
  echo "5173 artık ${visibility}: ${url}"
else
  echo "Görünürlük komutla değiştirilemedi (codespace jetonunun yetkisi yetmeyebilir)."
  echo "Elle: alttaki PORTS sekmesi → 'Site Yönetim (demo)' satırı → sağ tık →"
  echo "Port Visibility → $( [ "$visibility" = public ] && echo Public || echo Private )"
  echo "Bağlantı: ${url}"
fi
