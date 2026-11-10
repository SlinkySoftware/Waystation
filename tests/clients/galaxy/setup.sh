#!/bin/sh
# Create the Galaxy repositories the test uses: a proxy of galaxy.ansible.com, a hosted repository for published
# collections and a group of both (hosted first) whose uploads land in the hosted member. Idempotent.
set -eu
. /harness/lib.sh

step 'create galaxy proxy galaxy-proxy, hosted galaxy-hosted and group galaxy-all'
ws_login
exists() { ws_api GET "/repositories/$1" >/dev/null 2>&1; }
exists galaxy-proxy || ws_api POST /repositories '{
  "name": "galaxy-proxy",
  "format": "galaxy",
  "type": "proxy",
  "online": true,
  "description": "galaxy.ansible.com (client harness)",
  "proxy": {"upstream_url": "https://galaxy.ansible.com/"}
}' >/dev/null || fail 'could not create the proxy repository'
exists galaxy-hosted || ws_api POST /repositories '{
  "name": "galaxy-hosted",
  "format": "galaxy",
  "type": "hosted",
  "online": true,
  "description": "Published collections (client harness)"
}' >/dev/null || fail 'could not create the hosted repository'
exists galaxy-all || ws_api POST /repositories '{
  "name": "galaxy-all",
  "format": "galaxy",
  "type": "group",
  "online": true,
  "description": "Published collections and galaxy.ansible.com (client harness)",
  "group": {"members": ["galaxy-hosted", "galaxy-proxy"], "writable_member": "galaxy-hosted"}
}' >/dev/null || fail 'could not create the group repository'
