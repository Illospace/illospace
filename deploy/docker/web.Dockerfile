FROM node:22-bookworm-slim AS build

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM caddy:2-alpine

COPY deploy/docker/web.Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/frontend/build /srv

EXPOSE 8080
