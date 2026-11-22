#!/bin/sh
# Client side of the split-host + TLS test (tests/deploy/run.sh), run in a curl container on the simulated LAN with
# the test certificate in CURL_CA_BUNDLE.
#   client.sh tls     plain HTTP redirects to HTTPS, HTTPS speaks HTTP/2, the server answers behind it
#   client.sh seed    create a raw hosted and a raw proxy repository, upload a file, download it, proxy one file
#   client.sh fetch   the uploaded and the proxied file are served (run again after a server host stops)
set -eu
# shellcheck source=tests/clients/lib.sh
. /harness/lib.sh
export HARNESS_FORMAT=deploy HARNESS_MODE="${1:-?}"  # step/fail/pass labels (lib.sh)

header() {  # header <name> <file>: value of a response header in a curl -D dump (lower-case name)
  tr -d '\r' < "$2" | awk -v n="$1" 'index($0, ":") { k = tolower(substr($0, 1, index($0, ":") - 1));
    if (k == n) { v = substr($0, index($0, ":") + 1); sub(/^ +/, "", v) } } END { print v }'
}

hosted="$WAYSTATION_URL/repository/files-hosted"
proxy="$WAYSTATION_URL/repository/files-proxy"
plain="http://registry.example.com:8080"
cd /tmp

case "${1:-}" in
  tls)
    step 'plain HTTP redirects to WAYSTATION_BASE_URL with 308'
    code=$(curl -sS -o /dev/null -D h -w '%{http_code}' "$plain/ui/?x=1") || fail 'plain HTTP request'
    [ "$code" = 308 ] || fail "plain HTTP answered $code"
    [ "$(header location h)" = "$WAYSTATION_URL/ui/?x=1" ] || fail "redirect to $(header location h)"
    [ "$(curl -sS "$plain/_web/healthz")" = ok ] || fail 'healthcheck on the plain port'
    step 'HTTPS with HTTP/2: the SPA, and the server behind the web tier'
    [ "$(curl -fsS --http2 -o /dev/null -w '%{http_version}' "$WAYSTATION_URL/ui/")" = 2 ] || fail 'no HTTP/2'
    curl -fsS "$WAYSTATION_URL/healthz" | grep -q ok || fail '/healthz through the web tier'
    curl -fsS -o /dev/null --tlsv1.2 --tls-max 1.2 "$WAYSTATION_URL/ui/" || fail 'TLS 1.2'
    if curl -sS -o /dev/null --tlsv1.1 --tls-max 1.1 "$WAYSTATION_URL/ui/" 2>/dev/null; then fail 'TLS 1.1 accepted'; fi
    pass
    ;;
  seed)
    step 'create repositories files-hosted (raw hosted) and files-proxy (raw proxy of the origin)'
    ws_login
    ws_api POST /repositories '{"name": "files-hosted", "format": "raw", "type": "hosted", "online": true,
      "hosted": {"write_policy": "allow_redeploy"}}' >/dev/null || fail 'create files-hosted'
    ws_api POST /repositories '{"name": "files-proxy", "format": "raw", "type": "proxy", "online": true,
      "proxy": {"upstream_url": "http://origin.example.com:8080/", "allow_private_upstream": true}}' >/dev/null \
      || fail 'create files-proxy'
    ws_token '["read", "write"]'
    printf '%s' "$WS_TOKEN" > /run-dir/token
    step 'upload 64 MiB over HTTP/2 and download it back (X-Accel from the shared blob store)'
    head -c 67108864 /dev/urandom > big.bin
    curl -fsS --http2 -u "ci-bot:$WS_TOKEN" --upload-file big.bin "$hosted/releases/big.bin" -o /dev/null \
      || fail 'upload big.bin'
    sha256sum big.bin | cut -d' ' -f1 > /run-dir/big.sha256
    curl -fsS --http2 -u "ci-bot:$WS_TOKEN" -o back.bin "$hosted/releases/big.bin" || fail 'download big.bin'
    [ "$(sha256sum back.bin | cut -d' ' -f1)" = "$(cat /run-dir/big.sha256)" ] || fail 'big.bin differs'
    step 'fetch proxied.txt through files-proxy (fetched, then cached)'
    curl -fsS -D h1 -o /dev/null "$proxy/proxied.txt" || fail 'proxy fetch'
    [ "$(header x-ws-cache h1)" = FETCHING ] || fail "proxy fetch cache state: $(header x-ws-cache h1)"
    pass
    ;;
  fetch)
    WS_TOKEN=$(cat /run-dir/token)
    step 'the uploaded and the proxied file are served (ten times each, across the server hosts)'
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      curl -fsS -u "ci-bot:$WS_TOKEN" -o back.bin "$hosted/releases/big.bin" || fail 'download big.bin'
      [ "$(sha256sum back.bin | cut -d' ' -f1)" = "$(cat /run-dir/big.sha256)" ] || fail 'big.bin differs'
      curl -fsS -D h2 -o proxied.txt "$proxy/proxied.txt" || fail 'proxy fetch'
      [ "$(header x-ws-cache h2)" = HIT ] || fail "cached proxy fetch: $(header x-ws-cache h2)"
      grep -q 'proxied content' proxied.txt || fail "proxied.txt: $(cat proxied.txt)"
    done
    pass
    ;;
  *) fail "usage: client.sh tls|seed|fetch" ;;
esac
