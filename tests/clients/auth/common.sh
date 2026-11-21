# Shared names for the auth format's scripts (sourced after /harness/lib.sh). Test-only accounts of the directory
# and realm in Build/Containers/test/ (throwaway values, also used by `make test-integration`).

# LDAP (OpenLDAP, FreeIPA-like tree): bob is in group "ops" (mapped to the role below); erin is in no mapped group.
LDAP_USER=bob
LDAP_PASSWORD=Bob-Passw0rd1
LDAP_NOROLE_USER=erin
LDAP_NOROLE_PASSWORD=Erin-Passw0rd1
# OIDC (Keycloak realm "waystation"): alice is in group /developers (mapped to the role below). The directory has an
# alice with the same password too, so a password sign-in as alice would link the LDAP identity: only tokens here.
OIDC_USER=alice
OIDC_PASSWORD=Alice-Passw0rd1

ROLE=auth-matrix            # write (so read and browse too) on private-*
RAW=private-raw             # raw hosted
DOCKER=private-docker       # docker hosted
NPM=private-npm             # npm hosted
APT=private-apt             # apt proxy of deb.debian.org (trixie-updates)
YUM=private-yum             # yum hosted

SHARED=/harness-shared      # written by the curl client (provider ids, the OIDC user's API tokens)
NOT_A_TOKEN=Not-An-Api-Token-0  # a wrong password: neither an API token nor anyone's password
WRONG_TOKEN="wst_$(printf '%043d' 0)"  # shaped like an API token, but no such token

# shared_get <KEY>: a value the curl client left in $SHARED/auth.env (KEY=value lines).
shared_get() {
  _v=$(sed -n "s/^$1=//p" "$SHARED/auth.env" 2>/dev/null | tail -1)
  [ -n "$_v" ] || fail "shared_get: no $1 in $SHARED/auth.env (the curl client runs first)"
  printf '%s' "$_v"
}

# shared_put <KEY> <value>
shared_put() {
  (umask 077; printf '%s=%s\n' "$1" "$2" >> "$SHARED/auth.env")
}

# revoke_as_admin <token id>: revoke any account's token (incident-response route).
revoke_as_admin() {
  ws_login
  ws_revoke_any "$1"
}

# ldap_token <scopes JSON>: sign in as the LDAP user (UI sign-in with the directory password) and create an API token
# of theirs limited to private-*. Sets WS_TOKEN and WS_TOKEN_ID.
ldap_token() {
  ws_login "$LDAP_USER" "$LDAP_PASSWORD" "$(shared_get LDAP_PROVIDER)"
  ws_token "$1" '["private-*"]'
}
