#!/bin/sh
# client-debian: apt with credentials in /etc/apt/auth.conf.d (basic auth, an API token as the password) against a
# private apt proxy of Debian's trixie-updates suite. apt keeps verifying the distribution's signature.
set -eu
. /harness/lib.sh
. /harness/auth/common.sh

repo="$WAYSTATION_URL/repository/$APT/"
export DEBIAN_FRONTEND=noninteractive
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*
echo "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.pgp] $repo trixie-updates main" \
  > /etc/apt/sources.list.d/waystation.list
cat > /etc/apt/apt.conf.d/99waystation-harness <<'CONF'
Acquire::Languages "none";
#clear Acquire::CompressionTypes::Order;
Acquire::CompressionTypes::Order { "xz"; };
Acquire::IndexTargets::deb::Contents-deb::DefaultEnabled "false";
Acquire::Retries "0";
CONF
# Replayed fixtures are a snapshot: their Release file passes Valid-Until within days (signatures still verified).
if [ "${HARNESS_MODE:-}" = replay ]; then
  echo 'Acquire::Check-Valid-Until "false";' >> /etc/apt/apt.conf.d/99waystation-harness
fi
mkdir -p /etc/apt/auth.conf.d

credentials() {  # credentials <login> <password>: the auth.conf entry for the stack (apt needs the scheme for http)
  printf 'machine %s/repository/\nlogin %s\npassword %s\n' "$WAYSTATION_URL" "$1" "$2" > /etc/apt/auth.conf.d/waystation.conf
  chmod 600 /etc/apt/auth.conf.d/waystation.conf
}
update_ok() {  # update_ok <what>
  rm -rf /var/lib/apt/lists/*
  apt-get update --error-on=any > /tmp/update.log 2>&1 || { cat /tmp/update.log; fail "apt-get update ($1)"; }
  grep -q "/repository/$APT.* trixie-updates InRelease" /tmp/update.log || { cat /tmp/update.log; fail "no InRelease ($1)"; }
}
update_refused() {  # update_refused <what>: apt-get update fails with a 401
  rm -rf /var/lib/apt/lists/*
  if apt-get update --error-on=any > /tmp/update.log 2>&1; then cat /tmp/update.log; fail "$1: not refused"; fi
  grep -q '401' /tmp/update.log || { cat /tmp/update.log; fail "$1: no 401"; }
}

step 'anonymous apt-get update is refused (401: anonymous access is off)'
rm -f /etc/apt/auth.conf.d/waystation.conf
update_refused 'anonymous'

step "auth.conf with OIDC user $OIDC_USER's API token: apt-get update"
credentials ci-bot "$(shared_get OIDC_TOKEN_APT)"
update_ok 'OIDC token'

step "auth.conf with LDAP user $LDAP_USER's API token: apt-get update"
ldap_token '["read"]'
credentials "$LDAP_USER" "$WS_TOKEN"
update_ok 'LDAP token'

step "auth.conf with the LDAP password itself is refused (401: apt uses API tokens)"
credentials "$LDAP_USER" "$LDAP_PASSWORD"
update_refused 'LDAP password'

step 'auth.conf with a wrong password is refused (401)'
credentials "$LDAP_USER" "$NOT_A_TOKEN"
update_refused 'wrong password'

step 'the OIDC token is revoked by an administrator: refused (401)'
revoke_as_admin "$(shared_get OIDC_TOKEN_ID_APT)"
credentials ci-bot "$(shared_get OIDC_TOKEN_APT)"
update_refused 'revoked token'
rm -f /etc/apt/auth.conf.d/waystation.conf

pass
