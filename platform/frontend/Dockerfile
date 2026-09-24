# Multi-stage build for Vue 3 SPA
# ---------------------------------------------------------------------------
# Stage 1: Build
# ---------------------------------------------------------------------------
# Emits static files only, so it runs on the builder's own platform even when
# the image is built for another one.
FROM --platform=$BUILDPLATFORM node:22.22-alpine AS builder

WORKDIR /app

# Behind a package mirror, pass it in rather than editing this file:
#   docker build --build-arg NPM_REGISTRY=https://registry.npmmirror.com ...
ARG NPM_REGISTRY=https://registry.npmjs.org
ENV NPM_CONFIG_REGISTRY=${NPM_REGISTRY}
COPY package*.json ./
RUN npm ci

COPY . .

# Build with relative API URL so nginx can proxy /api
ENV VITE_API_URL=/api
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: Serve with nginx
# ---------------------------------------------------------------------------
FROM nginx:alpine-perl

COPY --from=builder /app/dist /usr/share/nginx/html

# Default nginx config (override in k8s via ConfigMap mount)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Build info — served at /build-info.json so the admin Version page can
# verify the bundle in the user's browser matches the image deployed.
ARG GIT_SHA=unknown
ARG GIT_REF=unknown
ARG BUILD_TIME=unknown
ARG IMAGE_TAG=unknown
RUN printf '{"component":"frontend","git_sha":"%s","git_ref":"%s","build_time":"%s","image_tag":"%s"}\n' \
    "$GIT_SHA" "$GIT_REF" "$BUILD_TIME" "$IMAGE_TAG" > /usr/share/nginx/html/build-info.json

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
