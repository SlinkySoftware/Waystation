#!/bin/sh
# Client side of the backup/restore test (tests/backup/run.sh), run in a curl container on the stack's network.
#   client.sh seed   create a raw hosted and a raw proxy repository, upload two files, fetch one through the proxy
#   client.sh check  after the restore: hosted files intact, the proxied file re-fetched from the origin
set -eu
. /harness/lib.sh
HARNESS_FORMAT=backup HARNESS_MODE=${1:-?}

header() {  # header <name> <file>: value of a response header in a curl -D dump (lower-case name)
  tr -d '\r' < "$2" | awk -v n="$1" 'index($0, ":") { k = tolower(substr($0, 1, index($0, ":") - 1));
    if (k == n) { v = substr($0, index($0, ":") + 1); sub(/^ +/, "", v) } } END { print v }'
}

hosted="$WAYSTATION_URL/repository/files-hosted"
proxy="$WAYSTATION_URL/repository/files-proxy"
cd /tmp

case "${1:-}" in
  seed)
    step 'create repositories files-hosted (raw hosted) and files-proxy (raw proxy of the origin)'
    ws_login
    ws_api POST /repositories '{"name": "files-hosted", "format": "raw", "type": "hosted", "online": true,
      "hosted": {"write_policy": "allow_redeploy"}}' >/dev/null || fail 'create files-hosted'
    ws_api POST /repositories '{"name": "files-proxy", "format": "raw", "type": "proxy", "online": true,
      "proxy": {"upstream_url": "http://origin.example.com:8080/", "allow_private_upstream": true}}' >/dev/null \
      || fail 'create files-proxy'
    ws_token '["read", "write"]'
    step 'upload two files to files-hosted'
    printf 'hosted release notes 1.0\n' > hosted.txt
    printf 'hosted build output\n%.0s' 1 2 3 4 5 > build.log
    for f in hosted.txt build.log; do
      curl -fsS -u "ci-bot:$WS_TOKEN" --upload-file "$f" "$hosted/releases/$f" -o /dev/null || fail "upload $f"
    done
    step 'fetch proxied.txt through files-proxy (cached)'
    curl -fsS -D h1 -o proxied.txt "$proxy/proxied.txt" || fail 'proxy fetch'
    [ "$(header x-ws-cache h1)" = FETCHING ] || fail "proxy fetch cache state: $(header x-ws-cache h1)"
    curl -fsS -D h2 -o /dev/null "$proxy/proxied.txt"
    [ "$(header x-ws-cache h2)" = HIT ] || fail "second proxy fetch cache state: $(header x-ws-cache h2)"
    pass
    ;;
  check)
    ws_login
    ws_token '["read"]'
    step 'hosted files survived the restore'
    curl -fsS -u "ci-bot:$WS_TOKEN" -o hosted.txt "$hosted/releases/hosted.txt" || fail 'hosted.txt'
    [ "$(cat hosted.txt)" = 'hosted release notes 1.0' ] || fail "hosted.txt: $(cat hosted.txt)"
    curl -fsS -u "ci-bot:$WS_TOKEN" -o build.log "$hosted/releases/build.log" || fail 'build.log'
    [ "$(wc -l < build.log | tr -d ' ')" = 5 ] || fail 'build.log content'
    step 'the proxied file (not in the hosted-only backup) is fetched from the origin again'
    curl -fsS -D h3 -o proxied.txt "$proxy/proxied.txt" || fail 'proxy fetch after restore'
    [ "$(header x-ws-cache h3)" = FETCHING ] || fail "after restore: $(header x-ws-cache h3)"
    grep -q 'proxied content' proxied.txt || fail "proxied.txt: $(cat proxied.txt)"
    curl -fsS -D h4 -o /dev/null "$proxy/proxied.txt"
    [ "$(header x-ws-cache h4)" = HIT ] || fail "cached again: $(header x-ws-cache h4)"
    pass
    ;;
  *) fail "usage: client.sh seed|check" ;;
esac
