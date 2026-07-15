#!/bin/sh
# Generate the runtime /config.json and the optional reverse-proxy snippet into
# /tmp (writable even when the root filesystem is read-only), then exec nginx.
set -eu

# 1) Backend origins consumed by the SPA's runtimeConfig loader. Empty = same origin.
cat > /tmp/config.json <<EOF
{"apexOrigin":"${APEX_ORIGIN:-}","langgraphOrigin":"${LANGGRAPH_ORIGIN:-}"}
EOF

# 2) Same-origin reverse proxy to the backend, only when BACKEND_UPSTREAM is set.
if [ -n "${BACKEND_UPSTREAM:-}" ]; then
    cat > /tmp/apex-proxy.conf <<EOF
location ~ ^/(v1|threads|runs|assistants|crons|store|ok|ready)(/|\$) {
    # /runs collides with an SPA route: browser navigations (Accept: text/html)
    # get index.html; API/SSE requests are forwarded (mirrors the vite dev proxy).
    if (\$http_accept ~* "text/html") {
        rewrite ^ /index.html last;
    }
    proxy_pass ${BACKEND_UPSTREAM};
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    # SSE: never buffer or cache; long read timeouts (mirrors compose-ha/nginx.conf).
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 24h;
    proxy_send_timeout 24h;
}
EOF
else
    : > /tmp/apex-proxy.conf
fi

exec "$@"
