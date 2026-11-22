#!/bin/sh
# Create the RubyGems repositories the test uses: a proxy of rubygems.org, a hosted repository and a group of both
# (hosted first). Idempotent.
set -eu
. /harness/lib.sh

create() {  # create <name> <JSON body>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create RubyGems repository $1"
  ws_api POST /repositories "$2" >/dev/null || fail "could not create the repository $1"
}

ws_login
create rubygems-proxy '{"name": "rubygems-proxy", "format": "rubygems", "type": "proxy", "online": true,
  "description": "RubyGems proxy (client harness)", "proxy": {"upstream_url": "https://rubygems.org"}}'
create rubygems-hosted '{"name": "rubygems-hosted", "format": "rubygems", "type": "hosted", "online": true,
  "description": "RubyGems hosted (client harness)"}'
create rubygems-group '{"name": "rubygems-group", "format": "rubygems", "type": "group", "online": true,
  "description": "RubyGems group (client harness)", "group": {"members": ["rubygems-hosted", "rubygems-proxy"]}}'
