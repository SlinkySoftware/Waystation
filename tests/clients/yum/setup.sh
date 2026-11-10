#!/bin/sh
# Create the yum repositories the test uses (idempotent: every client runs it).
set -eu
. /harness/lib.sh

create() {  # create <name> <upstream URL> [format_options JSON]
  options=${3:-}
  [ -n "$options" ] || options='{}'
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create yum proxy repository $1 -> $2"
  ws_api POST /repositories "{
    \"name\": \"$1\",
    \"format\": \"yum\",
    \"type\": \"proxy\",
    \"online\": true,
    \"description\": \"yum proxy (client harness)\",
    \"proxy\": {\"upstream_url\": \"$2\"},
    \"format_options\": $options
  }" >/dev/null || fail "could not create the repository $1"
}

# The distribution's own key as JSON (repomd_keys): Waystation then caches a repomd.xml only when its .asc verifies.
key_option() {
  python3 -c 'import json, sys; print(json.dumps({"repomd_keys": open(sys.argv[1]).read()}))' "$1"
}

ws_login
case "$HARNESS_CLIENT" in
  rocky)
    create rocky https://dl.rockylinux.org/pub/rocky/ "$(key_option /etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-10)"
    create epel https://dl.fedoraproject.org/pub/epel/
    if ! ws_api GET /repositories/yum-hosted >/dev/null 2>&1; then
      step 'create hosted yum repository yum-hosted'
      ws_api POST /repositories '{"name": "yum-hosted", "format": "yum", "type": "hosted", "online": true,
        "description": "hosted yum (client harness)", "hosted": {"write_policy": "allow_redeploy"}}' >/dev/null \
        || fail 'could not create the repository yum-hosted'
    fi
    ;;
  alma) create almalinux https://repo.almalinux.org/almalinux/ ;;
  oracle) create oracle https://yum.oracle.com/repo/OracleLinux/ ;;
  fedora | opensuse) create rocky https://dl.rockylinux.org/pub/rocky/ ;;  # normally created by the rocky run
  *) fail "unknown client $HARNESS_CLIENT" ;;
esac
