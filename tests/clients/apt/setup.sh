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
    # The archive's raspberrypi.gpg.key carries a binding signature that Debian 13's sqv policy rejects; Raspberry Pi
    # OS trusts the same key from the raspberrypi-archive-keyring package, pinned here by its SHA256.
    step 'fetch the Raspberry Pi archive keyring package through Waystation (raw proxy)'
    deb=pool/main/r/raspberrypi-archive-keyring/raspberrypi-archive-keyring_2025.1+rpt1_all.deb
    code=$(_ws_http GET "$WAYSTATION_URL/repository/raspberrypi-files/$deb")
    [ "$code" = 200 ] || fail "$deb returned $code"
    [ "$(sha256sum /tmp/ws-api.out | cut -d' ' -f1)" = \
      2e727149d7acb8cc7f604e66d0049161039c8aa1eaf1175e54f9e69d963d60e4 ] || fail "$deb: unexpected SHA256"
    rm -rf /tmp/rpi-keyring
    dpkg-deb -x /tmp/ws-api.out /tmp/rpi-keyring || fail "$deb: not a package"
    mkdir -p /etc/apt/keyrings
    cp /tmp/rpi-keyring/usr/share/keyrings/raspberrypi-archive-keyring.pgp /etc/apt/keyrings/raspberrypi.pgp
    ;;
  *) fail "unknown client $HARNESS_CLIENT" ;;
esac
