#!/usr/bin/env bash
# Checks the reverse-proxy examples (Documents/Operations/REVERSE_PROXIES.md) in containers: `make proxies-check`.
#
# For nginx, Traefik (file provider and Docker labels), Caddy and HAProxy:
#   1. syntax: nginx -t, caddy validate, haproxy -c, traefik starts without configuration errors, and
#      `docker compose config` accepts compose.yaml + traefik.override.example.yaml;
#   2. behaviour, against a stand-in for the web tier (backend.py) over TLS with a throwaway certificate:
#      health, HTTP -> HTTPS redirect, the event stream arrives unbuffered (>= 3 events in 3.5 s), a SIZE_MB download
#      and a SIZE_MB upload pass whole, X-Forwarded-For / X-Forwarded-Proto reach the backend, and (SLOW=1) a
#      response after 75 s of silence still arrives (catches 60 s default timeouts);
#   3. every config block in REVERSE_PROXIES.md is identical to its example file.
# Usage: Build/Containers/examples/reverse-proxies/check/check.sh [nginx|traefik|caddy|haproxy|docs]...
# Environment: SIZE_MB (default 1024), SLOW=1, KEEP=1 (leave the scratch directory).
# shellcheck disable=SC2015 # ok/bad never fail, so "test && ok || bad" is safe
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ROOT=$(cd "$HERE/../../../.." && pwd)
DOC="$ROOT/Documents/Operations/REVERSE_PROXIES.md"
SIZE_MB=${SIZE_MB:-1024}
NGINX_IMAGE=${NGINX_IMAGE:-nginx:1.30-alpine}
TRAEFIK_IMAGE=${TRAEFIK_IMAGE:-traefik:v3}
CADDY_IMAGE=${CADDY_IMAGE:-caddy:2-alpine}
HAPROXY_IMAGE=${HAPROXY_IMAGE:-haproxy:3.2-alpine}
PYTHON_IMAGE=${PYTHON_IMAGE:-python:3.14-slim-trixie}
CURL_IMAGE=${CURL_IMAGE:-curlimages/curl:8.17.0}
OPENSSL_IMAGE=${OPENSSL_IMAGE:-alpine/openssl:latest}
HOST=registry.example.com
ID=wspc-$$
NET=$ID
TMP=$(mktemp -d "${TMPDIR:-/tmp}/waystation-proxies.XXXXXX")
FAILED=0

cleanup() {
  docker ps -aq --filter "label=$ID" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  if [ "${KEEP:-0}" = 1 ]; then echo "scratch: $TMP"; else rm -rf "$TMP"; fi
}
trap cleanup EXIT

ok() { echo "  ok   $*"; }
bad() { echo "  FAIL $*"; FAILED=1; }

# ---------------------------------------------------------------------------- docs == example files
check_docs() {
  echo "== REVERSE_PROXIES.md blocks match the example files"
  local markers
  markers=$(grep -o '<!-- example: [^ ]* -->' "$DOC" | sed -E 's/<!-- example: ([^ ]*) -->/\1/')
  [ -n "$markers" ] || { bad "no example markers in $DOC"; return; }
  for rel in $markers; do
    awk -v m="<!-- example: $rel -->" '
      $0 == m {found = 1; next}
      found && /^```/ {if (inside) exit; inside = 1; next}
      inside {print}
    ' "$DOC" > "$TMP/doc-block"
    if diff -q "$TMP/doc-block" "$ROOT/$rel" >/dev/null; then ok "$rel"; else bad "$rel differs from its block in the doc"; fi
  done
}

# ---------------------------------------------------------------------------- shared fixtures
setup() {
  docker network create --label "$ID" "$NET" >/dev/null
  docker run --rm --user "$(id -u):$(id -g)" -v "$TMP:/out" "$OPENSSL_IMAGE" req -x509 -newkey rsa:2048 -nodes -days 2 \
    -subj "/CN=$HOST" -addext "subjectAltName=DNS:$HOST,DNS:labels.example.com" \
    -keyout /out/privkey.pem -out /out/fullchain.pem >/dev/null 2>&1
  cat "$TMP/fullchain.pem" "$TMP/privkey.pem" > "$TMP/$HOST.pem"
  chmod 0644 "$TMP"/*.pem
  truncate -s "${SIZE_MB}M" "$TMP/upload.bin"
  truncate -s 80M "$TMP/slow.bin"
  chmod 0755 "$TMP"
  docker run -d --label "$ID" --name "$ID-backend" --network "$NET" --network-alias waystation-web \
    -v "$HERE/check/backend.py:/backend.py:ro" "$PYTHON_IMAGE" python /backend.py >/dev/null
}

curl_() { docker run --rm --network "$NET" -v "$TMP:/data:ro" "$CURL_IMAGE" "$@"; }

wait_ready() { # proxy container
  for _ in $(seq 1 30); do
    if curl_ -sk -o /dev/null --max-time 2 --connect-to "$HOST:443:$1:443" "https://$HOST/healthz" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

behaviour() { # proxy container, host name
  local p=$1 h=${2:-$HOST} out
  local to=(--connect-to "$h:443:$p:443")
  wait_ready "$p" || { bad "$p never answered over TLS"; docker logs "$p" 2>&1 | tail -20; return; }
  out=$(curl_ -sk "${to[@]}" "https://$h/healthz") && [ "$out" = '{"status": "ok"}' ] && ok "health" || bad "health: $out"
  out=$(curl_ -s -o /dev/null -w '%{http_code}' --connect-to "$h:80:$p:80" "http://$h/healthz" || true)
  case "$out" in 301 | 302 | 307 | 308) ok "http -> https ($out)" ;; *) bad "http -> https redirect: $out" ;; esac
  out=$(curl_ -skN --max-time 3.5 "${to[@]}" "https://$h/api/v1/events/stream" 2>/dev/null | grep -c '^data:' || true)
  [ "${out:-0}" -ge 3 ] && ok "event stream unbuffered ($out events in 3.5 s)" || bad "event stream buffered ($out events)"
  out=$(curl_ -sk -o /dev/null -w '%{size_download}' "${to[@]}" "https://$h/big?mb=$SIZE_MB")
  [ "$out" = $((SIZE_MB * 1024 * 1024)) ] && ok "download $SIZE_MB MiB" || bad "download: $out bytes"
  out=$(curl_ -sk -T /data/upload.bin "${to[@]}" "https://$h/upload")
  [ "$out" = "{\"bytes\": $((SIZE_MB * 1024 * 1024))}" ] && ok "upload $SIZE_MB MiB" || bad "upload: $out"
  out=$(curl_ -sk "${to[@]}" "https://$h/headers")
  if echo "$out" | grep -qi '"x-forwarded-for": "[0-9a-f.:]*' && echo "$out" | grep -qi '"x-forwarded-proto": "https"'; then
    ok "X-Forwarded-For / X-Forwarded-Proto"
  else
    bad "forwarded headers: $out"
  fi
  if [ "${SLOW:-0}" = 1 ]; then
    out=$(curl_ -sk -o /dev/null -w '%{http_code}' "${to[@]}" "https://$h/slow?s=75" || true)
    [ "$out" = 200 ] && ok "75 s idle response" || bad "75 s idle response: $out"
    out=$(curl_ -sk --limit-rate 1M -T /data/slow.bin "${to[@]}" "https://$h/upload" || true)
    [ "$out" = '{"bytes": 83886080}' ] && ok "80 s upload" || bad "80 s upload: $out"
  fi
}

# ---------------------------------------------------------------------------- proxies
check_nginx() {
  echo "== nginx ($NGINX_IMAGE)"
  local args=(--label "$ID" --network "$NET" -v "$HERE/nginx/waystation.conf:/etc/nginx/conf.d/default.conf:ro"
    -v "$TMP:/etc/nginx/tls:ro")
  args+=(-e NGINX_ENTRYPOINT_QUIET_LOGS=1)
  if docker run --rm "${args[@]}" "$NGINX_IMAGE" nginx -t -q; then ok "nginx -t"; else bad "nginx -t"; return; fi
  docker run -d --name "$ID-nginx" "${args[@]}" "$NGINX_IMAGE" >/dev/null
  behaviour "$ID-nginx"
}

check_caddy() {
  echo "== Caddy ($CADDY_IMAGE)"
  if docker run --rm -v "$HERE/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >"$TMP/caddy.log" 2>&1; then
    ok "caddy validate"
  else
    bad "caddy validate"; tail -5 "$TMP/caddy.log"; return
  fi
  # Behaviour run: the throwaway certificate instead of ACME.
  sed -e 's|^\t# tls /etc/caddy/tls/|\ttls /etc/caddy/tls/|' "$HERE/caddy/Caddyfile" > "$TMP/Caddyfile"
  docker run -d --label "$ID" --name "$ID-caddy" --network "$NET" -v "$TMP/Caddyfile:/etc/caddy/Caddyfile:ro" \
    -v "$TMP:/etc/caddy/tls:ro" "$CADDY_IMAGE" >/dev/null
  behaviour "$ID-caddy"
}

check_haproxy() {
  echo "== HAProxy ($HAPROXY_IMAGE)"
  local args=(--label "$ID" --network "$NET" -v "$HERE/haproxy/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro"
    -v "$TMP:/usr/local/etc/haproxy/tls:ro")
  if docker run --rm "${args[@]}" "$HAPROXY_IMAGE" haproxy -c -q -f /usr/local/etc/haproxy/haproxy.cfg; then
    ok "haproxy -c"
  else
    bad "haproxy -c"; return
  fi
  docker run -d --name "$ID-haproxy" "${args[@]}" "$HAPROXY_IMAGE" >/dev/null
  behaviour "$ID-haproxy"
}

check_traefik() {
  echo "== Traefik ($TRAEFIK_IMAGE): file provider and Docker labels"
  # Only this run's labelled containers are visible to the Docker provider.
  sed -e "s|^    exposedByDefault: false|&\n    constraints: \"Label(\`waystation-check\`,\`$ID\`)\"|" \
    -e "s|^    network: proxy|    network: $NET|" "$HERE/traefik/traefik.yaml" > "$TMP/traefik.yaml"
  grep -q "$ID" "$TMP/traefik.yaml" || { bad "could not add the test constraint"; return; }
  docker run -d --label "$ID" --name "$ID-traefik" --network "$NET" \
    -v "$TMP/traefik.yaml:/etc/traefik/traefik.yaml:ro" -v "$HERE/traefik/dynamic:/etc/traefik/dynamic:ro" \
    -v "$TMP:/etc/traefik/tls:ro" -v /var/run/docker.sock:/var/run/docker.sock:ro "$TRAEFIK_IMAGE" >/dev/null
  # Labels from the override example, applied to a second backend under another host name.
  local labels=() line
  while IFS= read -r line; do
    line=$(echo "$line" | sed -E 's/^ +//; s/: "?/=/; s/"$//; s/registry\.example\.com/labels.example.com/')
    line=${line/traefik.docker.network=proxy/traefik.docker.network=$NET}
    labels+=(--label "${line//waystation/wslabels}")
  done < <(grep -E '^      traefik\.' "$ROOT/Build/Containers/examples/traefik.override.example.yaml")
  docker run -d --label "$ID" --label "waystation-check=$ID" "${labels[@]}" --name "$ID-labelled" --network "$NET" \
    -v "$HERE/check/backend.py:/backend.py:ro" "$PYTHON_IMAGE" python /backend.py >/dev/null
  echo " -- file provider ($HOST)"
  behaviour "$ID-traefik"
  echo " -- Docker labels (labels.example.com)"
  behaviour "$ID-traefik" labels.example.com
  if docker logs "$ID-traefik" 2>&1 | grep -E '"?level"?[=:]"?(error|ERR)|ERR ' | grep -v 'acme' ; then
    bad "Traefik logged errors (above)"
  else
    ok "no configuration errors in the Traefik log"
  fi
  if WAYSTATION_IMAGE_PREFIX=waystation WAYSTATION_IMAGE_TAG=0.0.0 docker compose \
    -f "$ROOT/Build/Containers/compose.yaml" -f "$ROOT/Build/Containers/examples/traefik.override.example.yaml" \
    config --quiet 2>"$TMP/compose.log"; then
    ok "docker compose config with traefik.override.example.yaml"
  else
    bad "docker compose config"; cat "$TMP/compose.log"
  fi
}

targets=("$@")
[ ${#targets[@]} -gt 0 ] || targets=(docs nginx traefik caddy haproxy)
for t in "${targets[@]}"; do
  case "$t" in
    docs) check_docs ;;
    nginx | traefik | caddy | haproxy) [ -d "$TMP/.setup" ] || { setup; mkdir "$TMP/.setup"; }; "check_$t" ;;
    *) echo "unknown target: $t" >&2; exit 2 ;;
  esac
done
[ "$FAILED" = 0 ] && echo "all checks passed" || { echo "some checks failed"; exit 1; }
