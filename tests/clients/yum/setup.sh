#!/bin/sh
# Create the yum proxy repositories the test uses (idempotent: the rocky and alma clients both run it).
set -eu
. /harness/lib.sh

create() {  # create <name> <upstream URL>
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
    \"format_options\": {}
  }" >/dev/null || fail "could not create the repository $1"
}

ws_login
case "$HARNESS_CLIENT" in
  rocky) create rocky https://dl.rockylinux.org/pub/rocky/ ;;
  alma) create almalinux https://repo.almalinux.org/almalinux/ ;;
  *) fail "unknown client $HARNESS_CLIENT" ;;
esac
