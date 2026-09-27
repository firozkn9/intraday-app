# Caddy image with the built SPA. Build context: repository root.
#   docker build -f infrastructure/docker/frontend.Dockerfile -t intraday-web .

FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM caddy:2-alpine
COPY infrastructure/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
