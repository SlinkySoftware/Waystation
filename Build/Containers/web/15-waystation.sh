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
set -eu

out=/tmp/nginx-conf
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

# ---- listeners
rm -f "$out/waystation-redirect.conf"
if [ -z "$cert" ] && [ -z "$key" ]; then
  printf 'listen %s;\n' "$port" > "$out/waystation-listen.inc"
  exit 0
fi

[ -n "$cert" ] && [ -n "$key" ] || die 'set both WAYSTATION_TLS_CERT and WAYSTATION_TLS_KEY, or neither'
for f in "$cert" "$key"; do
  case $f in *[!A-Za-z0-9._/@+-]*) die "unsupported characters in TLS file path '$f'" ;; esac
  [ -r "$f" ] || die "cannot read $f (uid 101 needs read access: e.g. group WAYSTATION_GID, mode 0640)"
done

cat > "$out/waystation-listen.inc" <<EOF
listen $tls_port ssl;
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
