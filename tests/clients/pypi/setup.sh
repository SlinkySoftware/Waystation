#!/bin/sh
# Create the PyPI repositories the test uses: a proxy of pypi.org, a hosted repository and a group of both (hosted
# first; uploads to the group go to the hosted member). Idempotent.
set -eu
. /harness/lib.sh

create() {  # create <name> <JSON body>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create PyPI repository $1"
  ws_api POST /repositories "$2" >/dev/null || fail "could not create the repository $1"
}

ws_login
create pypi-proxy '{"name": "pypi-proxy", "format": "pypi", "type": "proxy", "online": true,
  "description": "PyPI proxy (client harness)", "proxy": {"upstream_url": "https://pypi.org/"}}'
create pypi-hosted '{"name": "pypi-hosted", "format": "pypi", "type": "hosted", "online": true,
  "description": "PyPI hosted (client harness)"}'
create pypi-group '{"name": "pypi-group", "format": "pypi", "type": "group", "online": true,
  "description": "PyPI group (client harness)",
  "group": {"members": ["pypi-hosted", "pypi-proxy"], "writable_member": "pypi-hosted"}}'
