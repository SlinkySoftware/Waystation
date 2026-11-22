#!/bin/sh
# client-docker: docker login with an LDAP password and with an OIDC user's API token against a private hosted
# repository (dockerd started by setup.sh). Images are built FROM scratch locally: no upstream.
set -eu
. /harness/lib.sh
. /harness/auth/common.sh

registry=${WAYSTATION_URL#http://}
image="$registry/$DOCKER/auth-matrix"
login() {  # login <username> <password>: docker login, output in /tmp/login.out
  printf '%s' "$2" | docker login "$registry" --username "$1" --password-stdin > /tmp/login.out 2>&1
}
refused() {  # refused <what> <command...>: the command must fail with an auth error
  _what=$1; shift
  if "$@" > /tmp/refused.out 2>&1; then fail "$_what: not refused"; fi
  grep -Eqi 'denied|unauthorized|authentication required|invalid username or password|revoked|401|403|not found' /tmp/refused.out \
    || fail "$_what: $(cat /tmp/refused.out)"
}

step 'build a tiny image FROM scratch'
mkdir -p /tmp/ctx
printf 'auth matrix\n' > /tmp/ctx/hello.txt
printf 'FROM scratch\nCOPY hello.txt /\n' > /tmp/ctx/Dockerfile
docker build -q -t "$image:ldap" /tmp/ctx > /dev/null || fail 'docker build'
docker tag "$image:ldap" "$image:oidc"

step 'anonymous: push and pull refused (anonymous access is off)'
docker logout "$registry" > /dev/null 2>&1 || true
refused 'anonymous push' docker push -q "$image:ldap"
refused 'anonymous pull' docker pull -q "$image:ldap"

step "docker login with a wrong password is refused ($LDAP_USER)"
if login "$LDAP_USER" "$NOT_A_TOKEN"; then fail 'docker login with a wrong password worked'; fi
grep -Eqi 'unauthorized|401|incorrect' /tmp/login.out || fail "wrong password: $(cat /tmp/login.out)"

step "docker login with LDAP user $LDAP_USER's directory password: push, then pull"
login "$LDAP_USER" "$LDAP_PASSWORD" || fail "docker login (LDAP): $(cat /tmp/login.out)"
docker push -q "$image:ldap" > /dev/null || fail 'docker push after an LDAP login'
docker rmi -f "$image:ldap" > /dev/null
docker pull -q "$image:ldap" > /dev/null || fail 'docker pull after an LDAP login'

step "LDAP user $LDAP_NOROLE_USER (no mapped group): docker login works, the private repository does not"
login "$LDAP_NOROLE_USER" "$LDAP_NOROLE_PASSWORD" || fail "docker login ($LDAP_NOROLE_USER): $(cat /tmp/login.out)"
docker rmi -f "$image:ldap" > /dev/null
refused "$LDAP_NOROLE_USER pull" docker pull -q "$image:ldap"
refused "$LDAP_NOROLE_USER push" docker push -q "$image:oidc"

step "docker login with OIDC user $OIDC_USER's API token (any username): push and pull"
token=$(shared_get OIDC_TOKEN_DOCKER)
login ci-bot "$token" || fail "docker login (OIDC token): $(cat /tmp/login.out)"
docker push -q "$image:oidc" > /dev/null || fail 'docker push with the OIDC token'
docker pull -q "$image:ldap" > /dev/null || fail 'docker pull with the OIDC token'

step 'the OIDC token is revoked: the next pull is refused at once, so is a new login'
revoke_as_admin "$(shared_get OIDC_TOKEN_ID_DOCKER)"
docker rmi -f "$image:ldap" > /dev/null
refused 'pull with a revoked token' docker pull -q "$image:ldap"
if login ci-bot "$token"; then fail 'docker login with a revoked token worked'; fi
docker logout "$registry" > /dev/null 2>&1 || true

pass
