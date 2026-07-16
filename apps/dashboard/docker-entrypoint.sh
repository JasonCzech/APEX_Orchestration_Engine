#!/bin/sh
# Generate the runtime /config.json and the optional reverse-proxy snippet into
# /tmp (writable even when the root filesystem is read-only), then exec nginx.
set -eu

# These values are written into both JSON and nginx syntax. Accept only a
# complete HTTP(S) origin with a DNS/IPv4-style host and optional numeric port;
# rejecting paths, userinfo, controls, and nginx/JSON metacharacters keeps both
# generated files fail-closed. Public origins may carry one trailing slash,
# which is removed before they reach openapi-fetch.
normalize_http_origin() {
    label="$1"
    value="$2"
    allow_empty="$3"

    if [ -z "$value" ]; then
        if [ "$allow_empty" = "true" ]; then
            printf '%s' ""
            return 0
        fi
        echo "$label must be configured as an HTTP(S) origin" >&2
        return 2
    fi

    case "$value" in
        */) value="${value%/}" ;;
    esac
    origin_pattern='^https?://[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*(:[0-9]{1,5})?$'
    if [ "${#value}" -gt 2048 ] || ! printf '%s\n' "$value" | grep -Eq "$origin_pattern"; then
        echo "$label must be an HTTP(S) origin without userinfo, a path, query, or fragment" >&2
        return 2
    fi

    host_port="${value#http://}"
    host_port="${host_port#https://}"
    case "$host_port" in
        *:*)
            port="${host_port##*:}"
            if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
                echo "$label contains an invalid TCP port" >&2
                return 2
            fi
            ;;
    esac
    printf '%s' "$value"
}

apex_origin="$(normalize_http_origin APEX_ORIGIN "${APEX_ORIGIN:-}" true)"
langgraph_origin="$(normalize_http_origin LANGGRAPH_ORIGIN "${LANGGRAPH_ORIGIN:-}" true)"
backend_upstream=""
if [ -n "${BACKEND_UPSTREAM:-}" ]; then
    backend_upstream="$(normalize_http_origin BACKEND_UPSTREAM "$BACKEND_UPSTREAM" false)"
fi

# 1) Backend origins consumed by the SPA's runtimeConfig loader. Empty = same origin.
cat > /tmp/config.json <<EOF
{"apexOrigin":"${apex_origin}","langgraphOrigin":"${langgraph_origin}"}
EOF

# 2) Browser security headers. The API key is browser-held, so connect-src is
# an exact allowlist of the validated API origins plus same-origin. Add the
# matching WebSocket origin for SDK transports without opening an entire scheme.
connect_sources="'self'"
for origin in "$apex_origin" "$langgraph_origin"; do
    if [ -n "$origin" ]; then
        case " $connect_sources " in
            *" $origin "*) ;;
            *) connect_sources="$connect_sources $origin" ;;
        esac
        case "$origin" in
            https://*) socket_origin="wss://${origin#https://}" ;;
            http://*) socket_origin="ws://${origin#http://}" ;;
        esac
        case " $connect_sources " in
            *" $socket_origin "*) ;;
            *) connect_sources="$connect_sources $socket_origin" ;;
        esac
    fi
done
cat > /tmp/apex-security-headers.conf <<EOF
add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src ${connect_sources}; frame-ancestors 'none'; base-uri 'self'; form-action 'self'" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header Referrer-Policy "no-referrer" always;
add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
EOF

# 3) Same-origin reverse proxy to the backend, only when BACKEND_UPSTREAM is set.
if [ -n "$backend_upstream" ]; then
    cat > /tmp/apex-proxy.conf <<EOF
location ~ ^/(v1|threads|runs|assistants|crons|store|ok|ready)(/|\$) {
    # /runs collides with an SPA route: browser navigations (Accept: text/html)
    # get index.html; API/SSE requests are forwarded (mirrors the vite dev proxy).
    if (\$http_accept ~* "text/html") {
        rewrite ^ /index.html last;
    }
    proxy_pass ${backend_upstream};
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
