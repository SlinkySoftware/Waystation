#!/bin/sh
# Create the Swift registries the test uses: an SCM-backed proxy of github.com and a hosted registry. Idempotent.
set -eu
. /harness/lib.sh

step 'create the swift proxy swift-proxy (github.com) and the hosted registry swift-hosted'
ws_login
exists() { ws_api GET "/repositories/$1" >/dev/null 2>&1; }
exists swift-proxy || ws_api POST /repositories '{
  "name": "swift-proxy",
  "format": "swift",
  "type": "proxy",
  "online": true,
  "description": "Swift packages from github.com tags (client harness)",
  "proxy": {"upstream_url": "https://github.com/"},
  "format_options": {"archive_layout": "github"}
}' >/dev/null || fail 'could not create the proxy repository'
exists swift-hosted || ws_api POST /repositories '{
  "name": "swift-hosted",
  "format": "swift",
  "type": "hosted",
  "online": true,
  "description": "Published Swift packages (client harness)"
}' >/dev/null || fail 'could not create the hosted repository'
