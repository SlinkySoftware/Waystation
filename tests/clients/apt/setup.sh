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
    if ! ws_api GET /repositories/apt-hosted >/dev/null 2>&1; then
      step 'create hosted apt repository apt-hosted (distribution stable, component main)'
      ws_api POST /repositories '{"name": "apt-hosted", "format": "apt", "type": "hosted", "online": true,
        "description": "hosted apt (client harness)", "hosted": {"write_policy": "allow_redeploy"},
        "format_options": {"distribution": "stable", "components": ["main"], "architectures": ["amd64"]}}' \
        >/dev/null || fail 'could not create the repository apt-hosted'
    fi
    ;;
  ubuntu)
    create ubuntu https://archive.ubuntu.com/ubuntu/ resolute
    create ubuntu-security https://security.ubuntu.com/ubuntu/ resolute-security
    ;;
  raspios)  # Raspberry Pi OS: Debian (created by the debian client) + the Raspberry Pi archive
    create debian https://deb.debian.org/debian/ trixie
    create raspberrypi https://archive.raspberrypi.com/debian/ trixie
    if ! ws_api GET /repositories/raspberrypi-files >/dev/null 2>&1; then
      step 'create raw proxy repository raspberrypi-files (the archive signing key)'
      ws_api POST /repositories '{"name": "raspberrypi-files", "format": "raw", "type": "proxy", "online": true,
        "proxy": {"upstream_url": "https://archive.raspberrypi.com/debian/"}}' >/dev/null \
        || fail 'could not create the repository raspberrypi-files'
    fi
    step 'fetch the Raspberry Pi archive key through Waystation (raw proxy)'
    code=$(_ws_http GET "$WAYSTATION_URL/repository/raspberrypi-files/raspberrypi.gpg.key")
    [ "$code" = 200 ] || fail "raspberrypi.gpg.key returned $code"
    grep -q 'BEGIN PGP PUBLIC KEY BLOCK' /tmp/ws-api.out || fail 'raspberrypi.gpg.key is not an armored public key'
    mkdir -p /etc/apt/keyrings
    cp /tmp/ws-api.out /etc/apt/keyrings/raspberrypi.asc  # apt reads armored keys from *.asc
    ;;
  *) fail "unknown client $HARNESS_CLIENT" ;;
esac
