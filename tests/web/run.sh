#!/usr/bin/env bash
# Web tier configuration test (`make web-config-test`): renders the nginx configuration the way the web image does
# (Build/Containers/web: 15-waystation.sh, then the nginx image's envsubst step) in the pinned official nginx image,
# without building the SPA, and checks it:
#   - `nginx -t` accepts plain HTTP and TLS configurations, with and without docker dedicated port connectors
#     (WAYSTATION_DOCKER_CONNECTORS, SPECIFICATION §4.4, ADR 0013);
#   - invalid connector lists are refused at start;
#   - live requests against a header-echo upstream: a connector port sends X-Waystation-Connector: <port> and
#     replaces a client's copy, the main port never passes a client's copy, a connector answers /v2/ only (and over
#     HTTPS when TLS is on).
# Host: bash + docker. KEEP=1 leaves the containers and network in place.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
WEB="$ROOT/Build/Containers/web"
ID="wswt-$(printf '%s' "$ROOT" | md5sum | cut -c1-8)"
NET="$ID-net"
RUN=$(mktemp -d)
NGINX_IMAGE=$(sed -n 's/^ARG NGINX_IMAGE=//p' "$WEB/Dockerfile")
OPENSSL_IMAGE=${OPENSSL_IMAGE:-alpine/openssl:latest}
CURL_IMAGE=${CURL_IMAGE:-curlimages/curl:8.17.0}
PYTHON_IMAGE=${PYTHON_IMAGE:-python:3.14-alpine}

step() { printf '\n==> [web-config-test] %s\n' "$*"; }
fail() { printf 'FAIL [web-config-test]: %s\n' "$*" >&2; exit 1; }

cleanup() {
  local rc=$?
  if [ "${KEEP:-0}" = 1 ]; then
    echo "KEEP=1: left $ID-* containers, network $NET and $RUN in place"
    exit "$rc"
  fi
  docker rm -f "$ID-web" "$ID-web-tls" "$ID-echo" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$RUN"
  exit "$rc"
}
trap cleanup EXIT

# web <docker run options>... -- <command>...: the web container as the image runs it (uid 101, read-only root)
web_args=(--user 101 --read-only --tmpfs /tmp --ulimit nofile=65536:65536
  -e NGINX_ENVSUBST_OUTPUT_DIR=/tmp/nginx-conf -e 'NGINX_ENVSUBST_FILTER=^WAYSTATION_' -e NGINX_ENTRYPOINT_QUIET_LOGS=1
  -e WAYSTATION_DATA_DIR=/var/lib/waystation -e WAYSTATION_WEB_PORT=8080 -e WAYSTATION_WEB_TLS_PORT=8443
  -v "$WEB/15-waystation.sh:/docker-entrypoint.d/15-waystation.sh:ro"
  -v "$WEB/nginx.conf:/etc/nginx/nginx.conf:ro"
  -v "$WEB/nginx.conf.template:/etc/nginx/templates/waystation.conf.template:ro"
  -v "$WEB/waystation-proxy.inc.template:/etc/nginx/templates/waystation-proxy.inc.template:ro"
  -v "$WEB/waystation-blobs.inc.template:/etc/nginx/templates/waystation-blobs.inc.template:ro"
  -v "$RUN/tls:/run/waystation-tls:ro")
tls_env=(-e WAYSTATION_TLS_CERT=/run/waystation-tls/tls.crt -e WAYSTATION_TLS_KEY=/run/waystation-tls/tls.key
  -e WAYSTATION_BASE_URL=https://registry.example.com)

check_config() {  # check_config <label> <docker run options>...: render and nginx -t; prints the connectors file
  local label=$1
  shift
  docker run --rm "${web_args[@]}" -e WAYSTATION_SERVER_ADDR=127.0.0.1:8000 "$@" --entrypoint sh "$NGINX_IMAGE" -c \
    '/docker-entrypoint.sh nginx -t -q && cat /tmp/nginx-conf/waystation-connectors.conf' > "$RUN/out" 2> "$RUN/err" \
    || fail "$label: $(cat "$RUN/out" "$RUN/err")"
}

refused() {  # refused <expected message> <connector list>
  local out
  out=$(docker run --rm "${web_args[@]}" -e WAYSTATION_SERVER_ADDR=127.0.0.1:8000 \
    -e WAYSTATION_DOCKER_CONNECTORS="$2" "$NGINX_IMAGE" nginx -t 2>&1) && fail "accepted connectors '$2'"
  echo "$out" | grep -q "$1" || fail "connectors '$2': unexpected output: $out"
}

step "throwaway certificate ($NGINX_IMAGE)"
mkdir -p "$RUN/tls"
docker run --rm --user "$(id -u):$(id -g)" -v "$RUN/tls:/out" "$OPENSSL_IMAGE" req -x509 -newkey rsa:2048 -nodes \
  -days 2 -subj '/CN=registry.example.com' -addext 'subjectAltName=DNS:registry.example.com' \
  -keyout /out/tls.key -out /out/tls.crt >/dev/null 2>&1
chmod 0755 "$RUN" "$RUN/tls"
chmod 0644 "$RUN/tls/tls.crt" "$RUN/tls/tls.key"

step 'nginx -t: plain HTTP without connectors (no connector server)'
check_config 'plain'
[ ! -s "$RUN/out" ] || fail "connector servers without connectors: $(cat "$RUN/out")"

step 'nginx -t: plain HTTP with connectors 5001,5002'
check_config 'plain + connectors' -e WAYSTATION_DOCKER_CONNECTORS='5001,5002'
grep -q '^    listen 5001;$' "$RUN/out" || fail "no plain listener on 5001: $(cat "$RUN/out")"
grep -q '^    proxy_set_header X-Waystation-Connector 5002;$' "$RUN/out" || fail 'no connector header for 5002'

step 'nginx -t: TLS with connectors 5001 5002 (HTTPS + HTTP/2 on the same certificate)'
check_config 'tls + connectors' "${tls_env[@]}" -e WAYSTATION_DOCKER_CONNECTORS='5001 5002'
[ "$(grep -c '^    listen 500[12] ssl;$' "$RUN/out")" = 2 ] || fail "no TLS connector listeners: $(cat "$RUN/out")"
grep -q 'include /tmp/nginx-conf/waystation-tls.inc;' "$RUN/out" || fail 'connectors do not use the TLS settings'

step 'invalid connector lists are refused'
refused 'is a web port' '8080'
refused 'is a web port' '8443'
refused 'outside 1024-65535' '80'
refused 'outside 1024-65535' '70000'
refused 'expected a port number' '5001;5002'
refused 'expected a port number' '05001'
refused 'lists port 5001 twice' '5001 5001'

step 'live: header-echo upstream, web with connectors 5001 5002 (plain) and 5001 (TLS)'
docker network create "$NET" >/dev/null
# The "server": answers every request with the X-Waystation-Connector header it received (or "none").
docker run -d --name "$ID-echo" --network "$NET" --network-alias echo.example.com "$PYTHON_IMAGE" python -c '
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("connector=" + (self.headers.get("X-Waystation-Connector") or "none") + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("", 8000), H).serve_forever()
' >/dev/null
docker run -d --name "$ID-web" --network "$NET" "${web_args[@]}" -e WAYSTATION_SERVER_ADDR=echo.example.com:8000 \
  -e WAYSTATION_DOCKER_CONNECTORS='5001 5002' "$NGINX_IMAGE" >/dev/null
docker run -d --name "$ID-web-tls" --network "$NET" --network-alias registry.example.com "${web_args[@]}" \
  -e WAYSTATION_SERVER_ADDR=echo.example.com:8000 "${tls_env[@]}" -e WAYSTATION_DOCKER_CONNECTORS=5001 \
  "$NGINX_IMAGE" >/dev/null

curl_() {  # curl_ <curl arguments>...: status and body
  docker run --rm --network "$NET" -v "$RUN/tls:/tls:ro" "$CURL_IMAGE" -sS --retry 10 --retry-connrefused \
    --retry-delay 1 --cacert /tls/tls.crt -o - -w '%{http_code}\n' "$@"
}
expect() {  # expect <label> <expected output> <curl arguments>...
  local label=$1 want=$2 got
  shift 2
  got=$(curl_ "$@" | tr '\n' ' ')
  [ "$got" = "$want" ] || fail "$label: expected '$want', got '$got'"
  echo "ok: $label"
}
web=$ID-web
expect 'connector 5001 names its port' 'connector=5001 200 ' "http://$web:5001/v2/library/nginx/manifests/latest"
expect 'connector 5002 names its port' 'connector=5002 200 ' "http://$web:5002/v2/"
expect 'connector /v2 (no slash)' 'connector=5001 200 ' "http://$web:5001/v2"
expect 'a client copy is replaced on a connector' 'connector=5002 200 ' -H 'X-Waystation-Connector: 5001' \
  "http://$web:5002/v2/"
expect 'a client copy never passes the main port' 'connector=none 200 ' -H 'X-Waystation-Connector: 5001' \
  "http://$web:8080/v2/"
expect 'main port /api' 'connector=none 200 ' "http://$web:8080/api/v1/config"
[ "$(curl_ "http://$web:5001/api/v1/config" | tail -1)" = 404 ] || fail 'a connector answered /api/'
[ "$(curl_ "http://$web:5001/ui/" | tail -1)" = 404 ] || fail 'a connector answered /ui/'
[ "$(curl_ "http://$web:5001/repository/x/y" | tail -1)" = 404 ] || fail 'a connector answered /repository/'
echo 'ok: a connector answers /v2/ only'
expect 'TLS connector (HTTP/2)' 'connector=5001 200 ' --http2 'https://registry.example.com:5001/v2/'
expect 'TLS main port' 'connector=none 200 ' -H 'X-Waystation-Connector: 5001' 'https://registry.example.com:8443/v2/'

# Security headers (15-waystation.sh): artifacts and API answers are sandboxed, the UI gets its own CSP, HSTS only
# over TLS, and every location that adds its own headers still carries them.
headers() {  # headers <url>: the response headers, lower-cased
  docker run --rm --network "$NET" -v "$RUN/tls:/tls:ro" "$CURL_IMAGE" -sS --retry 10 --retry-connrefused \
    --retry-delay 1 --cacert /tls/tls.crt -o /dev/null -D - "$1" | tr -d '\r' | tr '[:upper:]' '[:lower:]'
}
has() {  # has <label> <headers> <line>...
  local label=$1 got=$2
  shift 2
  for line in "$@"; do
    printf '%s\n' "$got" | grep -qxF "$line" || fail "$label: no '$line' in: $got"
  done
  echo "ok: $label"
}
common=('x-content-type-options: nosniff' 'x-frame-options: deny' 'referrer-policy: same-origin')
sandbox="content-security-policy: sandbox; frame-ancestors 'none'"
h=$(headers "http://$web:8080/repository/x/y")
has 'artifact answers are sandboxed' "$h" "${common[@]}" "$sandbox"
printf '%s\n' "$h" | grep -q '^strict-transport-security' && fail 'HSTS over plain HTTP'
has 'API answers' "$(headers "http://$web:8080/api/v1/config")" "${common[@]}" "$sandbox"
has 'event stream' "$(headers "http://$web:8080/api/v1/events/stream")" "${common[@]}" "$sandbox" \
  'x-accel-buffering: no'
has 'connector answers' "$(headers "http://$web:5001/v2/")" "${common[@]}" "$sandbox"
h=$(headers "http://$web:8080/ui/")
has 'UI CSP' "$h" "${common[@]}" 'cache-control: no-cache'
printf '%s\n' "$h" | grep -q "^content-security-policy: default-src 'self'; script-src 'self';.*frame-ancestors 'none'$" \
  || fail "UI CSP: $h"
has 'UI assets' "$(headers "http://$web:8080/ui/assets/x.js")" "${common[@]}" \
  'cache-control: public, max-age=31536000, immutable'
has 'HSTS over TLS' "$(headers 'https://registry.example.com:8443/api/v1/config')" "${common[@]}" "$sandbox" \
  'strict-transport-security: max-age=31536000'

printf '\nPASS [web-config-test]\n'
