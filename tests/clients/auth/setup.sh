#!/bin/sh
# Auth format setup. client-curl (first): configure Waystation as the bootstrap admin: the LDAP and OIDC providers
# with group -> role mappings, the role, the private repositories and no anonymous access. client-docker: start
# dockerd with the stack as an insecure (plain HTTP) registry. The other clients need nothing here.
set -eu
. /harness/lib.sh
. /harness/auth/common.sh

case "$HARNESS_CLIENT" in
  curl) ;;
  docker)
    step 'start dockerd (insecure-registries = the stack)'
    mkdir -p /etc/docker
    printf '{"insecure-registries": ["%s"]}\n' "${WAYSTATION_URL#http://}" > /etc/docker/daemon.json
    dockerd > /tmp/dockerd.log 2>&1 &
    i=0
    until docker info > /dev/null 2>&1; do
      i=$((i + 1))
      [ "$i" -lt 60 ] || { tail -20 /tmp/dockerd.log >&2; fail 'dockerd did not start'; }
      sleep 1
    done
    exit 0
    ;;
  *) exit 0 ;;
esac

ws_login
[ -s /harness-idp/ca.crt ] || fail 'no LDAP CA in /harness-idp (ldap-certs did not run)'
ca=$(awk '{printf "%s\\n", $0}' /harness-idp/ca.crt)

step "create role $ROLE (write, so also read and browse, on private-*)"
ws_api POST /roles "{\"id\": \"$ROLE\", \"name\": \"Auth matrix\", \"description\": \"client harness\",
  \"permissions\": [{\"action\": \"write\", \"scope\": \"private-*\"}]}" >/dev/null || fail 'could not create the role'

step 'create the LDAP provider (OpenLDAP, StartTLS with the test CA; group ops -> role)'
out=$(ws_api POST /auth-providers "{
  \"kind\": \"ldap\", \"name\": \"Directory\", \"enabled\": true, \"order\": 10,
  \"ldap\": {
    \"connection\": {\"servers\": [\"ldap1.example.com\"], \"security\": \"starttls\", \"ca_pem\": \"$ca\",
      \"bind_dn\": \"uid=waystation-svc,cn=sysaccounts,cn=etc,dc=example,dc=com\", \"bind_password\": \"Svc-Passw0rd1\",
      \"connect_timeout_s\": 3, \"timeout_s\": 5},
    \"users\": {\"base_dn\": \"cn=users,cn=accounts,dc=example,dc=com\", \"filter\": \"(objectClass=inetOrgPerson)\",
      \"username_attribute\": \"uid\", \"display_name_attribute\": \"displayName\", \"email_attribute\": \"mail\"},
    \"groups\": {\"mode\": \"member_of\", \"member_of_attribute\": \"memberOf\", \"nested\": false},
    \"cache_ttl_s\": 60
  },
  \"group_mapping\": [{\"external_group\": \"ops\", \"roles\": [\"$ROLE\"]}]
}") || fail 'could not create the LDAP provider'
ldap_id=$(printf '%s' "$out" | sed -n 's/^{"id": *"\([^"]*\)".*/\1/p')
[ -n "$ldap_id" ] || fail "no LDAP provider id in: $out"

step 'create the OIDC provider (Keycloak realm waystation; group /developers -> role)'
out=$(ws_api POST /auth-providers "{
  \"kind\": \"oidc\", \"name\": \"Keycloak\", \"enabled\": true, \"order\": 20,
  \"oidc\": {\"issuer\": \"http://keycloak.example.com:8080/realms/waystation\", \"client_id\": \"waystation\",
    \"client_secret\": \"waystation-test-client-secret\", \"scopes\": [\"openid\", \"profile\", \"email\"],
    \"groups_claim\": \"groups\", \"auto_provision\": true, \"allow_private\": true, \"timeout_s\": 10},
  \"group_mapping\": [{\"external_group\": \"/developers\", \"roles\": [\"$ROLE\"]}]
}") || fail 'could not create the OIDC provider'
oidc_id=$(printf '%s' "$out" | sed -n 's/^{"id": *"\([^"]*\)".*/\1/p')
[ -n "$oidc_id" ] || fail "no OIDC provider id in: $out"
shared_put LDAP_PROVIDER "$ldap_id"
shared_put OIDC_PROVIDER "$oidc_id"

step "create the private repositories ($RAW, $DOCKER, $NPM, $APT, $YUM)"
create() {  # create <name> <format> <type> [extra JSON members]
  ws_api POST /repositories "{\"name\": \"$1\", \"format\": \"$2\", \"type\": \"$3\", \"online\": true,
    \"description\": \"client-auth matrix (client harness)\"${4:+, $4}}" >/dev/null || fail "could not create $1"
}
create "$RAW" raw hosted '"hosted": {"write_policy": "allow_redeploy"}'
create "$DOCKER" docker hosted '"hosted": {"write_policy": "allow_redeploy"}'
create "$NPM" npm hosted '"hosted": {"write_policy": "allow_redeploy"}'
create "$APT" apt proxy '"proxy": {"upstream_url": "https://deb.debian.org/debian/"}, "format_options": {"distribution": "trixie"}'
create "$YUM" yum hosted '"hosted": {"write_policy": "allow_redeploy"}'

step 'anonymous access off: every repository is private'
ws_api PUT /anonymous-access '{"roles": []}' >/dev/null || fail 'could not turn anonymous access off'
