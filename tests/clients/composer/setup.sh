#!/bin/sh
# Create the Composer repositories the test uses: a proxy of repo.packagist.org, a hosted repository and a group of
# both (hosted first). Idempotent.
set -eu
. /harness/lib.sh

create() {  # create <name> <JSON body>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create Composer repository $1"
  ws_api POST /repositories "$2" >/dev/null || fail "could not create the repository $1"
}

ws_login
create composer-proxy '{"name": "composer-proxy", "format": "composer", "type": "proxy", "online": true,
  "description": "Composer proxy (client harness)", "proxy": {"upstream_url": "https://repo.packagist.org"}}'
create composer-hosted '{"name": "composer-hosted", "format": "composer", "type": "hosted", "online": true,
  "description": "Composer hosted (client harness)"}'
create composer-group '{"name": "composer-group", "format": "composer", "type": "group", "online": true,
  "description": "Composer group (client harness)", "group": {"members": ["composer-hosted", "composer-proxy"]}}'
