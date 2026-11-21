#!/bin/sh
# client-node: npm login (legacy, LDAP password -> an API token) and _authToken (OIDC user's API token) against a
# private hosted npm repository. The package is generated here: no upstream.
set -eu
. /harness/lib.sh
. /harness/auth/common.sh

registry="$WAYSTATION_URL/repository/$NPM/"
export npm_config_audit=false npm_config_fund=false npm_config_update_notifier=false npm_config_loglevel=warn
work=/tmp/npm-auth
rm -rf "$work"
mkdir -p "$work/pkg" "$work/consumer"
pkg='@ws-harness-test/auth-matrix'
printf '{"name":"%s","version":"1.0.0","main":"index.js","license":"MIT"}\n' "$pkg" > "$work/pkg/package.json"
printf 'module.exports = () => "auth matrix";\n' > "$work/pkg/index.js"
auth_line() { printf '%s:_authToken=%s\n' "${registry#http:}" "$1"; }
npm_login() {  # npm_login <username> <password> <userconfig>: npm reads each answer with a fresh readline
  { printf '%s\n' "$1"; sleep 3; printf '%s\n' "$2"; } | \
    npm login --auth-type=legacy --registry "$registry" --userconfig "$3" --loglevel notice > "$work/login.log" 2>&1
}

step 'anonymous: npm view refused (E401: anonymous access is off)'
if npm view "$pkg" --registry "$registry" --userconfig /dev/null --cache "$work/c0" > "$work/anon.log" 2>&1; then
  fail 'anonymous npm view worked'
fi
grep -q 'E401' "$work/anon.log" || fail "anonymous: $(cat "$work/anon.log")"

step "npm login with a wrong password is refused (E401, $LDAP_USER)"
: > "$work/npmrc-wrong"
if npm_login "$LDAP_USER" "$NOT_A_TOKEN" "$work/npmrc-wrong"; then fail 'npm login with a wrong password worked'; fi
grep -q 'E401' "$work/login.log" || fail "wrong password: $(cat "$work/login.log")"

step "npm login --auth-type=legacy with LDAP user $LDAP_USER's directory password: publish, install"
: > "$work/npmrc-ldap"
npm_login "$LDAP_USER" "$LDAP_PASSWORD" "$work/npmrc-ldap" || fail "npm login (LDAP): $(cat "$work/login.log")"
grep -q '_authToken=wst_' "$work/npmrc-ldap" || fail 'npm login stored no API token'
[ "$(npm whoami --registry "$registry" --userconfig "$work/npmrc-ldap")" = "$LDAP_USER" ] || fail 'npm whoami (LDAP)'
(cd "$work/pkg" && npm publish --registry "$registry" --userconfig "$work/npmrc-ldap" --cache "$work/c1") \
  || fail 'npm publish (LDAP login)'
(cd "$work/consumer" && npm init -y > /dev/null && npm install --registry "$registry" --userconfig "$work/npmrc-ldap" \
  --cache "$work/c2" "$pkg@1.0.0") || fail 'npm install (LDAP login)'
(cd "$work/consumer" && node -e 'if (require(process.argv[1])() !== "auth matrix") process.exit(1)' "$pkg") \
  || fail 'installed package does not work'

step "_authToken = OIDC user $OIDC_USER's API token: npm whoami, view and install"
token=$(shared_get OIDC_TOKEN_NPM)
auth_line "$token" > "$work/npmrc-oidc"
[ "$(npm whoami --registry "$registry" --userconfig "$work/npmrc-oidc")" = "$OIDC_USER" ] || fail 'npm whoami (OIDC)'
rm -rf "$work/consumer/node_modules"
(cd "$work/consumer" && npm ci --registry "$registry" --userconfig "$work/npmrc-oidc" --cache "$work/c3") \
  || fail 'npm ci with the OIDC token'

step 'a wrong _authToken is refused (E401)'
auth_line "$WRONG_TOKEN" > "$work/npmrc-bad"
if npm view "$pkg" --registry "$registry" --userconfig "$work/npmrc-bad" --cache "$work/c4" > "$work/bad.log" 2>&1; then
  fail 'a wrong token worked'
fi
grep -q 'E401' "$work/bad.log" || fail "wrong token: $(cat "$work/bad.log")"

step 'the OIDC token is revoked by an administrator: refused at once (E401)'
revoke_as_admin "$(shared_get OIDC_TOKEN_ID_NPM)"
if npm view "$pkg" --registry "$registry" --userconfig "$work/npmrc-oidc" --cache "$work/c5" > "$work/rev.log" 2>&1; then
  fail 'a revoked token still works'
fi
grep -q 'E401' "$work/rev.log" || fail "revoked token: $(cat "$work/rev.log")"

step 'npm logout revokes the token npm login created'
cp "$work/npmrc-ldap" "$work/npmrc-ldap-copy"
npm logout --registry "$registry" --userconfig "$work/npmrc-ldap" || fail 'npm logout'
if npm whoami --registry "$registry" --userconfig "$work/npmrc-ldap-copy" > "$work/logout.log" 2>&1; then
  fail 'the npm login token still works after npm logout'
fi
grep -q 'E401' "$work/logout.log" || fail "after logout: $(cat "$work/logout.log")"

pass
