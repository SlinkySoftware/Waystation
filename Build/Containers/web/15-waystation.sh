#!/bin/sh
# Waystation web tier: renders the listeners, TLS and the server list into /tmp/nginx-conf before nginx starts
# (SPECIFICATION §8.4). Run by the nginx image's entrypoint from /docker-entrypoint.d/ (as uid 101; the root
# filesystem is read-only). Environment:
#   WAYSTATION_SERVER_ADDR    host:port of the server, or several separated by commas or spaces (one per server
#                             replica or host; default waystation-server:8000)
#   WAYSTATION_WEB_PORT       plain HTTP listen port (default 8080)
#   WAYSTATION_TLS_CERT       certificate chain (PEM) path; with WAYSTATION_TLS_KEY turns on TLS: HTTPS with HTTP/2 on
#   WAYSTATION_TLS_KEY        WAYSTATION_WEB_TLS_PORT (default 8443), and the plain port redirects to HTTPS
#   WAYSTATION_BASE_URL       the redirect target when it is an https:// URL (else https://<requested host>)
#   WAYSTATION_DOCKER_CONNECTORS  docker dedicated port connectors (SPECIFICATION §4.4, ADR 0013): ports separated by
#                             commas or spaces (1024-65535, not the web ports). Each gets its own server: /v2/ only,
#                             HTTPS + HTTP/2 on the same certificate when TLS is on (else plain HTTP), proxied with
#                             X-Waystation-Connector: <port>; the server routes it to the docker repository whose
#                             connector_port it is. Publish the same port numbers on the host.
#   WAYSTATION_NGINX_CONF_DIR output directory (default /tmp/nginx-conf; tests render elsewhere)
set -eu

out=${WAYSTATION_NGINX_CONF_DIR:-/tmp/nginx-conf}
port=${WAYSTATION_WEB_PORT:-8080}
tls_port=${WAYSTATION_WEB_TLS_PORT:-8443}
cert=${WAYSTATION_TLS_CERT:-}
key=${WAYSTATION_TLS_KEY:-}

die() {
  echo "waystation-web: $*" >&2
  exit 1
}

mkdir -p "$out"

for p in "$port" "$tls_port"; do
  case $p in '' | *[!0-9]*) die "invalid port '$p'" ;; esac
done

# ---- upstream servers
addrs=$(printf '%s' "${WAYSTATION_SERVER_ADDR:-waystation-server:8000}" | tr ',' ' ')
: > "$out/waystation-upstream.inc"
for a in $addrs; do
  case $a in
    *[!][A-Za-z0-9.:_-]*) die "invalid WAYSTATION_SERVER_ADDR entry '$a' (expected host:port)" ;;
    *:[0-9]*) printf 'server %s;\n' "$a" >> "$out/waystation-upstream.inc" ;;
    *) die "WAYSTATION_SERVER_ADDR entry '$a' has no port (expected host:port)" ;;
  esac
done
[ -s "$out/waystation-upstream.inc" ] || die 'WAYSTATION_SERVER_ADDR is empty'

# ---- docker dedicated port connectors (checked before the listeners are written)
connectors=''
for c in $(printf '%s' "${WAYSTATION_DOCKER_CONNECTORS:-}" | tr ',' ' '); do
  case $c in *[!0-9]* | 0*) die "invalid WAYSTATION_DOCKER_CONNECTORS entry '$c' (expected a port number)" ;; esac
  [ "${#c}" -le 5 ] && [ "$c" -ge 1024 ] && [ "$c" -le 65535 ] \
    || die "WAYSTATION_DOCKER_CONNECTORS port $c is outside 1024-65535"
  [ "$c" != "$port" ] && [ "$c" != "$tls_port" ] || die "WAYSTATION_DOCKER_CONNECTORS port $c is a web port"
  case " $connectors " in *" $c "*) die "WAYSTATION_DOCKER_CONNECTORS lists port $c twice" ;; esac
  connectors="$connectors $c"
done

tls=0
if [ -n "$cert" ] || [ -n "$key" ]; then
  [ -n "$cert" ] && [ -n "$key" ] || die 'set both WAYSTATION_TLS_CERT and WAYSTATION_TLS_KEY, or neither'
  for f in "$cert" "$key"; do
    case $f in *[!A-Za-z0-9._/@+-]*) die "unsupported characters in TLS file path '$f'" ;; esac
    [ -r "$f" ] || die "cannot read $f (uid 101 needs read access: e.g. group WAYSTATION_GID, mode 0640)"
  done
  tls=1
fi

# One server per connector: only the registry API (and the blobs the server redirects to). nginx sets the header
# itself, so a client's copy never reaches the server; the main server clears it (nginx.conf.template).
: > "$out/waystation-connectors.conf"
for c in $connectors; do
  if [ "$tls" = 1 ]; then
    listen="listen $c ssl;
    include /tmp/nginx-conf/waystation-tls.inc;"
  else
    listen="listen $c;"
  fi
  cat >> "$out/waystation-connectors.conf" <<EOF
# docker dedicated port connector $c (WAYSTATION_DOCKER_CONNECTORS)
server {
    $listen
    server_name _;
    include /tmp/nginx-conf/waystation-proxy.inc;
    proxy_set_header X-Waystation-Connector $c;
    location /v2/ {
        proxy_pass http://waystation_server;
    }
    location = /v2 {
        proxy_pass http://waystation_server;
    }
    include /tmp/nginx-conf/waystation-blobs.inc;
    location / {
        return 404;
    }
}
EOF
done

# ---- listeners
rm -f "$out/waystation-redirect.conf" "$out/waystation-tls.inc"
if [ "$tls" = 0 ]; then
  printf 'listen %s;\n' "$port" > "$out/waystation-listen.inc"
  exit 0
fi

printf 'listen %s ssl;\ninclude /tmp/nginx-conf/waystation-tls.inc;\n' "$tls_port" > "$out/waystation-listen.inc"
cat > "$out/waystation-tls.inc" <<EOF
http2 on;
ssl_certificate $cert;
ssl_certificate_key $key;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_session_cache shared:waystation_tls:10m;
ssl_session_timeout 1d;
ssl_session_tickets off;
EOF

base=${WAYSTATION_BASE_URL:-}
base=${base%/}
case $base in
  https://*)
    case $base in *[!][A-Za-z0-9.:/_-]*) die "unsupported characters in WAYSTATION_BASE_URL '$base'" ;; esac
    target=$base
    ;;
  *)
    [ -z "$base" ] || echo "waystation-web: WAYSTATION_BASE_URL is not https://; redirecting to https://<host>" >&2
    target='https://$host'
    ;;
esac

# Plain HTTP only redirects (308 keeps the method and body) and answers the container healthcheck.
cat > "$out/waystation-redirect.conf" <<EOF
server {
    listen $port;
    server_name _;
    location = /_web/healthz {
        access_log off;
        default_type text/plain;
        return 200 "ok\n";
    }
    location / {
        return 308 $target\$request_uri;
    }
}
EOF
