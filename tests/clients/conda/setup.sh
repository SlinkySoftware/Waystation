#!/bin/sh
# Create the conda repositories the test uses: a proxy of the conda-forge channel (anonymous reads, as every proxy)
# and a hosted channel (needs credentials). Idempotent.
set -eu
. /harness/lib.sh

create() {  # create <name> <JSON body>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create conda repository $1"
  ws_api POST /repositories "$2" >/dev/null || fail "could not create the repository $1"
}

ws_login
create conda-forge '{"name": "conda-forge", "format": "conda", "type": "proxy", "online": true,
  "description": "conda-forge channel proxy (client harness)",
  "proxy": {"upstream_url": "https://conda.anaconda.org/conda-forge"}}'
create conda-hosted '{"name": "conda-hosted", "format": "conda", "type": "hosted", "online": true,
  "description": "conda hosted channel (client harness)"}'
