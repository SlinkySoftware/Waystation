#!/bin/sh
# Create the Helm repositories the test uses: a proxy of the metrics-server chart repository, a hosted repository
# for uploads and a group of both (hosted first) whose uploads land in the hosted member. Idempotent.
set -eu
. /harness/lib.sh

step 'create helm proxy metrics-server, hosted helm-hosted and group helm-all'
ws_login
exists() { ws_api GET "/repositories/$1" >/dev/null 2>&1; }
exists metrics-server || ws_api POST /repositories '{
  "name": "metrics-server",
  "format": "helm",
  "type": "proxy",
  "online": true,
  "description": "metrics-server chart repository (client harness)",
  "proxy": {"upstream_url": "https://kubernetes-sigs.github.io/metrics-server/"}
}' >/dev/null || fail 'could not create the proxy repository'
exists helm-hosted || ws_api POST /repositories '{
  "name": "helm-hosted",
  "format": "helm",
  "type": "hosted",
  "online": true,
  "description": "Chart uploads (client harness)",
  "hosted": {"write_policy": "allow_redeploy"}
}' >/dev/null || fail 'could not create the hosted repository'
exists helm-all || ws_api POST /repositories '{
  "name": "helm-all",
  "format": "helm",
  "type": "group",
  "online": true,
  "description": "Uploads and metrics-server (client harness)",
  "group": {"members": ["helm-hosted", "metrics-server"], "writable_member": "helm-hosted"}
}' >/dev/null || fail 'could not create the group repository'
