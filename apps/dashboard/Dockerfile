# Multi-stage build for the APEX dashboard SPA.
# Build context = REPO ROOT (the dashboard depends on the @apex/api-client and
# @apex/pipeline-events workspaces):
#   docker build -f apps/dashboard/Dockerfile -t apex-dashboard:local .
#
# Runtime: one image serves any environment — the backend origins are injected
# into /config.json at container start from APEX_ORIGIN / LANGGRAPH_ORIGIN, and
# an optional same-origin reverse proxy is enabled by setting BACKEND_UPSTREAM.

# ── build ─────────────────────────────────────────────────────────────────────
FROM node:24-alpine AS build
WORKDIR /app
# Workspace manifests + lockfile first for dependency-layer caching.
COPY package.json package-lock.json ./
COPY packages/api-client/package.json packages/api-client/
COPY packages/pipeline-events/package.json packages/pipeline-events/
COPY apps/dashboard/package.json apps/dashboard/
RUN npm ci
# Sources, then build the SPA into apps/dashboard/dist.
COPY packages packages
COPY apps/dashboard apps/dashboard
RUN npm run -w @apex/dashboard build

# ── runtime ───────────────────────────────────────────────────────────────────
# nginx-unprivileged runs as uid 101 and listens on 8080 — non-root by default.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
# Custom config: serves the SPA from the (read-only) image rootfs, /config.json and
# all temp/pid paths from /tmp, so readOnlyRootFilesystem=true works with one /tmp
# emptyDir. Backend reverse-proxy snippet is generated into /tmp at startup.
COPY apps/dashboard/nginx.conf /etc/nginx/nginx.conf
COPY apps/dashboard/docker-entrypoint.sh /apex-entrypoint.sh
COPY --from=build /app/apps/dashboard/dist /usr/share/nginx/html
USER root
RUN chmod +x /apex-entrypoint.sh
USER 101
ENV APEX_ORIGIN="" \
    LANGGRAPH_ORIGIN="" \
    BACKEND_UPSTREAM=""
EXPOSE 8080
ENTRYPOINT ["/apex-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
