# syntax=docker/dockerfile:1
# Frontend'i derler, iç Caddy ile sunar (Caddyfile çalışırken bağlanır). Bağlam: frontend
# reposu. API aynı alan adında (/api) olduğu için frontend göreli adres kullanır.
# Üretim derlemesine MSW (sahte servisler) girmez.

FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM caddy:2
COPY --from=build /app/dist /srv/web
