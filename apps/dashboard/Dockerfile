# Multi-stage build for the APEX dashboard SPA.
# Build context = REPO ROOT (the dashboard depends on the @apex/api-client and
# @apex/pipeline-events workspaces):
#   docker build -f apps/dashboard/Dockerfile -t apex-dashboard:local .
#
# Runtime: one image serves any environment — the backend origins are injected
# into /config.json at container start from APEX_ORIGIN / LANGGRAPH_ORIGIN, and
# an optional same-origin reverse proxy is enabled by setting BACKEND_UPSTREAM.

# ── build ─────────────────────────────────────────────────────────────────────
FROM node:24-alpine@sha256:a0b9bf06e4e6193cf7a0f58816cc935ff8c2a908f81e6f1a95432d679c54fbfd AS build
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
FROM nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0 AS runtime
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
