#!/bin/sh
# Create the NuGet repositories the test uses: a proxy of api.nuget.org, a hosted repository and a group of both
# (hosted first; pushes to the group go to the hosted member). Idempotent.
set -eu
. /harness/lib.sh

create() {  # create <name> <JSON body>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create NuGet repository $1"
  ws_api POST /repositories "$2" >/dev/null || fail "could not create the repository $1"
}

ws_login
create nuget-proxy '{"name": "nuget-proxy", "format": "nuget", "type": "proxy", "online": true,
  "description": "NuGet proxy (client harness)", "proxy": {"upstream_url": "https://api.nuget.org/v3/index.json"}}'
create nuget-hosted '{"name": "nuget-hosted", "format": "nuget", "type": "hosted", "online": true,
  "description": "NuGet hosted (client harness)"}'
create nuget-group '{"name": "nuget-group", "format": "nuget", "type": "group", "online": true,
  "description": "NuGet group (client harness)",
  "group": {"members": ["nuget-hosted", "nuget-proxy"], "writable_member": "nuget-hosted"}}'
