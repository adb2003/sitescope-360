# SiteScope 360 frontend — built to static assets, served by nginx (§38).
#
# The build context is the repository root, not `frontend/`, because the app imports the
# generated cross-language contracts from `shared/`. Keeping that import means a status string
# cannot drift between the backend and the UI.

FROM node:22-alpine AS builder

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json ./frontend/
WORKDIR /build/frontend
RUN npm ci --no-audit --no-fund

WORKDIR /build
COPY shared ./shared
COPY frontend ./frontend

WORKDIR /build/frontend
# Vite inlines env vars at build time, so this is an ARG rather than a runtime variable.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# ---- runtime ----------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

COPY --from=builder /build/frontend/dist /usr/share/nginx/html
COPY infrastructure/nginx/frontend.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
