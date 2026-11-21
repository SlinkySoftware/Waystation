#!/bin/sh
# client-curl: the OIDC user signs in through Keycloak's login form (curl is the browser) and creates one API token
# per client for the later clients; then the raw (curl) row of the matrix.
set -eu
. /harness/lib.sh
. /harness/auth/common.sh

status() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
oidc_id=$(shared_get OIDC_PROVIDER)

# oidc_sign_in <password> <cookie jar>: start at Waystation, post the Keycloak login form, deliver the redirect to
# Waystation's callback. Prints where the callback sends the browser (nothing when Keycloak did not send it back).
oidc_sign_in() {
  rm -f "$2" /tmp/kc.jar
  start=$(curl -sS -o /dev/null -w '%{redirect_url}' -c "$2" "$WAYSTATION_URL/auth/oidc/$oidc_id/login?next=/")
  case "$start" in http://keycloak.example.com:8080/realms/waystation/*) ;; *) fail "OIDC start: $start" ;; esac
  page=$(curl -sS -c /tmp/kc.jar -b /tmp/kc.jar "$start") || fail 'Keycloak login page'
  action=$(printf '%s' "$page" | tr '\n' ' ' \
    | sed -n 's/.*<form[^>]*id="kc-form-login"[^>]*action="\([^"]*\)".*/\1/p' | sed 's/&amp;/\&/g')
  [ -n "$action" ] || fail 'no login form on the Keycloak page'
  back=$(curl -sS -o /dev/null -w '%{redirect_url}' -c /tmp/kc.jar -b /tmp/kc.jar --data-urlencode "username=$OIDC_USER" \
    --data-urlencode "password=$1" --data-urlencode 'credentialId=' "$action")
  [ -n "$back" ] || return 0
  case "$back" in "$WAYSTATION_URL/auth/oidc/$oidc_id/callback?"*) ;; *) fail "Keycloak sent the browser to $back" ;; esac
  curl -sS -o /dev/null -w '%{redirect_url}' -b "$2" -c "$2" "$back"
}

step "OIDC: a wrong Keycloak password does not come back to Waystation"
[ -z "$(oidc_sign_in "$NOT_A_TOKEN" /tmp/oidc-wrong.jar)" ] || fail 'Keycloak accepted a wrong password'

step "OIDC: $OIDC_USER signs in through Keycloak (authorization code + PKCE), group /developers -> $ROLE"
landing=$(oidc_sign_in "$OIDC_PASSWORD" /tmp/oidc.jar)
case "$landing" in "$WAYSTATION_URL/ui"* | /ui*) ;; *) fail "OIDC callback sent the browser to: $landing" ;; esac
case "$landing" in *error=*) fail "OIDC sign-in failed: $landing" ;; esac
WS_COOKIES=/tmp/oidc.jar
WS_CSRF=$(awk '$6 == "ws_csrf" {print $7}' /tmp/oidc.jar)
[ -n "$WS_CSRF" ] || fail 'no CSRF cookie after the OIDC sign-in'
me=$(ws_api GET /auth/me) || fail 'GET /auth/me with the OIDC session'
printf '%s' "$me" | grep -q "\"username\": *\"$OIDC_USER\"" || fail "OIDC session is not $OIDC_USER: $me"

step "OIDC: $OIDC_USER creates an API token per client (read+write on private-*)"
for client in DOCKER NPM APT YUM RAW; do
  ws_token '["read","write"]' '["private-*"]'
  shared_put "OIDC_TOKEN_$client" "$WS_TOKEN"
  shared_put "OIDC_TOKEN_ID_$client" "$WS_TOKEN_ID"
done
oidc_token=$WS_TOKEN oidc_token_id=$WS_TOKEN_ID  # the RAW one

# ---------------------------------------------------------------------------- raw (curl, basic auth)
url="$WAYSTATION_URL/repository/$RAW"
printf 'auth matrix\n' > /tmp/a.txt

step "raw: anonymous upload and download refused (401 Basic challenge: anonymous access is off)"
[ "$(status --upload-file /tmp/a.txt "$url/ldap/a.txt")" = 401 ] || fail 'anonymous upload not refused'
curl -sS -o /dev/null -D /tmp/anon.h "$url/ldap/a.txt"
head -1 /tmp/anon.h | grep -q ' 401' || fail "anonymous download: $(head -1 /tmp/anon.h)"
grep -qi '^www-authenticate: Basic realm="Waystation"' /tmp/anon.h || fail 'no Basic challenge for anonymous'

step "raw: LDAP user $LDAP_USER (API token from a directory sign-in) uploads and downloads with basic auth"
ldap_token '["read","write"]'
ldap_tok=$WS_TOKEN ldap_tok_id=$WS_TOKEN_ID
[ "$(status -u "$LDAP_USER:$ldap_tok" --upload-file /tmp/a.txt "$url/ldap/a.txt")" = 201 ] || fail 'LDAP token upload'
[ "$(curl -fsS -u "$LDAP_USER:$ldap_tok" "$url/ldap/a.txt")" = 'auth matrix' ] || fail 'LDAP token download'

step "raw: the LDAP password itself is refused on format routes (401: clients use API tokens)"
[ "$(status -u "$LDAP_USER:$LDAP_PASSWORD" "$url/ldap/a.txt")" = 401 ] || fail 'LDAP password accepted by basic auth'

step "raw: OIDC user $OIDC_USER's API token (any username) uploads and downloads"
[ "$(status -u "ci-bot:$oidc_token" --upload-file /tmp/a.txt "$url/oidc/a.txt")" = 201 ] || fail 'OIDC token upload'
[ "$(curl -fsS -u "ci-bot:$oidc_token" "$url/ldap/a.txt")" = 'auth matrix' ] || fail 'OIDC token download'
[ "$(curl -fsS -H "Authorization: Bearer $oidc_token" "$url/oidc/a.txt")" = 'auth matrix' ] || fail 'bearer download'

step 'raw: a wrong password is refused (401), not downgraded to anonymous'
[ "$(status -u "$LDAP_USER:$NOT_A_TOKEN" "$url/ldap/a.txt")" = 401 ] || fail 'wrong password not refused'
[ "$(status -u "ci-bot:$WRONG_TOKEN" "$url/ldap/a.txt")" = 401 ] \
  || fail 'unknown token not refused'

step 'raw: revoked tokens are refused at once (OIDC token by an administrator, LDAP token by its owner)'
revoke_as_admin "$oidc_token_id"
[ "$(status -u "ci-bot:$oidc_token" "$url/ldap/a.txt")" = 401 ] || fail 'revoked OIDC token not refused'
ws_login "$LDAP_USER" "$LDAP_PASSWORD" "$(shared_get LDAP_PROVIDER)"
ws_revoke "$ldap_tok_id"
[ "$(status -u "$LDAP_USER:$ldap_tok" "$url/ldap/a.txt")" = 401 ] || fail 'revoked LDAP token not refused'

step "LDAP user $LDAP_NOROLE_USER (no mapped group) signs in, but the private repository is hidden (404)"
ws_login "$LDAP_NOROLE_USER" "$LDAP_NOROLE_PASSWORD" "$(shared_get LDAP_PROVIDER)"
if ws_api GET "/repositories/$RAW" > /dev/null 2> /tmp/norole.err; then fail "$LDAP_NOROLE_USER can see $RAW"; fi
grep -q -- '-> 404' /tmp/norole.err || fail "$LDAP_NOROLE_USER: $(cat /tmp/norole.err)"

pass
