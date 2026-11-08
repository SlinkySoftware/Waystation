#!/bin/sh
# Create the apt proxy repositories the test uses (idempotent: the debian and ubuntu clients both run it).
set -eu
. /harness/lib.sh

create() {  # create <name> <upstream URL> <distribution>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create apt proxy repository $1 -> $2"
  ws_api POST /repositories "{
    \"name\": \"$1\",
    \"format\": \"apt\",
    \"type\": \"proxy\",
    \"online\": true,
    \"description\": \"apt proxy (client harness)\",
    \"proxy\": {\"upstream_url\": \"$2\"},
    \"format_options\": {\"distribution\": \"$3\"}
  }" >/dev/null || fail "could not create the repository $1"
}

ws_login
case "$HARNESS_CLIENT" in
  debian)
    create debian https://deb.debian.org/debian/ trixie
    create debian-security https://deb.debian.org/debian-security/ trixie-security
    ;;
  ubuntu)
    create ubuntu https://archive.ubuntu.com/ubuntu/ resolute
    create ubuntu-security https://security.ubuntu.com/ubuntu/ resolute-security
    ;;
  *) fail "unknown client $HARNESS_CLIENT" ;;
esac
