#!/bin/sh
# Create the Cargo repositories the test uses: a proxy of index.crates.io (anonymous reads, as every proxy), a hosted
# registry and a group of both with the hosted one as writable member (both need credentials). Idempotent.
set -eu
. /harness/lib.sh

create() {  # create <name> <JSON body>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create Cargo repository $1"
  ws_api POST /repositories "$2" >/dev/null || fail "could not create the repository $1"
}

ws_login
create cargo-proxy '{"name": "cargo-proxy", "format": "cargo", "type": "proxy", "online": true,
  "description": "crates.io sparse proxy (client harness)", "proxy": {"upstream_url": "https://index.crates.io/"}}'
create cargo-hosted '{"name": "cargo-hosted", "format": "cargo", "type": "hosted", "online": true,
  "description": "Cargo hosted registry (client harness)"}'
create cargo-group '{"name": "cargo-group", "format": "cargo", "type": "group", "online": true,
  "description": "Cargo group (client harness)",
  "group": {"members": ["cargo-hosted", "cargo-proxy"], "writable_member": "cargo-hosted"}}'
