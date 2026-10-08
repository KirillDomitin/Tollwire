# Образ веб-сервера: сборка фронтенда (Node) → готовая статика внутри nginx. Node в прод не попадает.
# Базовые образы — по дайджесту (см. корневой Dockerfile).

FROM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS build
WORKDIR /app
# Сначала только манифесты: слой с зависимостями пересобирается лишь при их изменении.
COPY package.json package-lock.json ./
# Сеть до реестра npm бывает нестабильной (обрывы ECONNRESET на крупных пакетах). Поэтому:
# - кэш npm в cache mount переживает упавшую сборку: повторный запуск докачивает только недостающее;
# - --prefer-offline берёт из этого кэша всё, что уже скачано, без лишних запросов;
# - --fetch-retries: упавший пакет npm перезапросит сам, с растущей паузой.
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund --prefer-offline \
      --fetch-retries=5 --fetch-retry-mintimeout=10000 --fetch-retry-maxtimeout=60000
COPY . .
# tsc && vite build: проверка типов — часть сборки, сломанный по типам фронт в образ не попадёт.
RUN npm run build

FROM nginx:stable-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94
COPY --from=build /app/dist /usr/share/nginx/html
