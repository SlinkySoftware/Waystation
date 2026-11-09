#!/bin/sh
# Create the Go repositories the test uses: a proxy of proxy.golang.org (checksum database proxy on, the default) and
# a group of it. Idempotent.
set -eu
. /harness/lib.sh

create() {  # create <name> <JSON body>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create Go repository $1"
  ws_api POST /repositories "$2" >/dev/null || fail "could not create the repository $1"
}

ws_login
create go-proxy '{"name": "go-proxy", "format": "go", "type": "proxy", "online": true,
  "description": "Go module proxy (client harness)", "proxy": {"upstream_url": "https://proxy.golang.org/"}}'
create go-group '{"name": "go-group", "format": "go", "type": "group", "online": true,
  "description": "Go group (client harness)", "group": {"members": ["go-proxy"]}}'
