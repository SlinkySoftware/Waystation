#!/bin/sh
# Echo self-test (client-curl container). Runs identically in record, replay and live mode: in replay every
# answer comes from tests/fixtures/upstreams/echo/, so the assertions below prove the fixtures are complete and
# that scrubbing kept what clients need while dropping secrets.
set -eu
. /harness/lib.sh

base=https://echo.example.com

step 'plain GET over TLS (harness CA)'
body=$(curl -fsS "$base/hello")
[ "$body" = 'hello from the echo origin' ] || fail "unexpected /hello body: $body"

step 'plain HTTP listener'
[ "$(curl -fsS http://echo.example.com/hello)" = 'hello from the echo origin' ] || fail 'http /hello'

step 'volatile and environment-specific headers are not replayed'
headers=$(curl -fsS -D - -o /dev/null "$base/hello" | tr -d '\r' | tr '[:upper:]' '[:lower:]')
echo "$headers" | grep -q '^cache-control: max-age=60' || fail 'cache-control header lost'
if [ "$HARNESS_MODE" = replay ]; then
  for h in set-cookie x-request-id x-client-ip; do
    if echo "$headers" | grep -q "^$h:"; then fail "$h replayed"; fi
  done
fi

step 'token endpoint: JSON shape kept, credentials redacted in fixtures'
json=$(curl -fsS "$base/token")
echo "$json" | grep -q '"expires_in": *300' || fail "token response: $json"
if [ "$HARNESS_MODE" = replay ]; then
  echo "$json" | grep -q '"token": *"harness-redacted"' || fail "token not redacted: $json"
fi

step 'auth scheme is part of the key: 401 anonymous, 200 with a bearer token'
code=$(curl -sS -o /dev/null -w '%{http_code}' "$base/private")
[ "$code" = 401 ] || fail "anonymous /private returned $code"
token=$(echo "$json" | sed -n 's/.*"token": *"\([^"]*\)".*/\1/p')
[ "$(curl -fsS -H "Authorization: Bearer $token" "$base/private")" = 'private content' ] || fail 'bearer /private'

step 'redirect to a signed, expiring URL (query values scrubbed consistently)'
sum=$(curl -fsSL "$base/redirect" | sha256sum | cut -d' ' -f1)
expected=$(curl -fsS "$base/blob/4096" | sha256sum | cut -d' ' -f1)
[ "$sum" = "$expected" ] || fail 'redirect target differs from /blob/4096'

step 'binary body, byte exact, and ETag revalidation (304)'
etag=$(curl -fsS -D - -o /tmp/blob "$base/blob/4096" | tr -d '\r' | sed -n 's/^[Ee][Tt]ag: //p')
[ "$(wc -c < /tmp/blob)" -eq 4096 ] || fail 'blob size'
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "If-None-Match: $etag" "$base/blob/4096")
[ "$code" = 304 ] || fail "conditional GET returned $code"

step 'HEAD'
curl -fsSI "$base/hello" | tr -d '\r' | grep -qi '^content-length: 27$' || fail 'HEAD content-length'

if [ "${HARNESS_STACK:-0}" = 1 ]; then
  step 'Waystation stack under test answers through the web tier'
  curl -fsS "$WAYSTATION_URL/healthz" >/dev/null || fail 'stack /healthz'
fi

pass
