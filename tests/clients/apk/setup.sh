#!/bin/sh
# Create the apk repositories the test uses: a proxy of the Alpine CDN and a hosted repository. Idempotent.
set -eu
. /harness/lib.sh

create() {  # create <name> <JSON body>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create apk repository $1"
  ws_api POST /repositories "$2" >/dev/null || fail "could not create the repository $1"
}

ws_login
create alpine-proxy '{"name": "alpine-proxy", "format": "apk", "type": "proxy", "online": true,
  "description": "Alpine proxy (client harness)", "proxy": {"upstream_url": "https://dl-cdn.alpinelinux.org/alpine/"},
  "format_options": {"branch": "v3.22", "repositories": ["main"]}}'
create apk-hosted '{"name": "apk-hosted", "format": "apk", "type": "hosted", "online": true,
  "description": "apk hosted (client harness)", "hosted": {"write_policy": "allow_redeploy"},
  "format_options": {"branch": "v1", "repositories": ["main"]}}'
