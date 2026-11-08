# Shared helpers for client scripts (POSIX sh; sourced inside client containers as /harness/lib.sh).
# The harness CA is installed for curl via CURL_CA_BUNDLE; other clients read HARNESS_CA (see README).

step() { printf '\n==> [%s/%s] %s\n' "${HARNESS_FORMAT:-?}" "${HARNESS_MODE:-?}" "$*"; }
fail() { printf 'FAIL [%s/%s]: %s\n' "${HARNESS_FORMAT:-?}" "${HARNESS_MODE:-?}" "$*" >&2; exit 1; }
pass() { printf '\nPASS [%s/%s]\n' "${HARNESS_FORMAT:-?}" "${HARNESS_MODE:-?}"; }

# Wait until a URL answers (any HTTP status), e.g. after the stack starts.
wait_for() {
  i=0
  until curl -s -o /dev/null "$1"; do
    i=$((i + 1)); [ "$i" -lt "${2:-60}" ] || fail "timed out waiting for $1"; sleep 1
  done
}

# Install the harness CA into the system trust store (Debian/Ubuntu/Alpine client images).
trust_harness_ca() {
  if [ -d /usr/local/share/ca-certificates ] && command -v update-ca-certificates >/dev/null; then
    cp "$HARNESS_CA" /usr/local/share/ca-certificates/waystation-harness.crt
    update-ca-certificates >/dev/null 2>&1
  else
    fail 'trust_harness_ca: no update-ca-certificates in this image'
  fi
}

# Sign in to the stack under test as the bootstrap admin (per-run random HARNESS_ADMIN_PASSWORD, set by run.sh).
# Sets WS_COOKIES (a curl cookie jar) and WS_CSRF for ws_api. Needs curl in the client image.
ws_login() {
  [ -n "${HARNESS_ADMIN_PASSWORD:-}" ] || fail 'ws_login: HARNESS_ADMIN_PASSWORD is not set'
  wait_for "$WAYSTATION_URL/healthz"
  WS_COOKIES=/tmp/ws-cookies.txt
  _body=$(printf '{"username":"admin","password":"%s"}' "$HARNESS_ADMIN_PASSWORD")
  _session=$(curl -fsS -c "$WS_COOKIES" -H 'Content-Type: application/json' -d "$_body" \
    "$WAYSTATION_URL/api/v1/auth/login") || fail 'ws_login: sign-in failed'
  WS_CSRF=$(printf '%s' "$_session" | sed -n 's/.*"csrf_token": *"\([^"]*\)".*/\1/p')
  [ -n "$WS_CSRF" ] || fail 'ws_login: no CSRF token in the session'
}

# ws_api <METHOD> <path under /api/v1> [json body]: an authenticated API call (after ws_login). Prints the response
# body; on HTTP >= 400 prints it to stderr and returns 1.
ws_api() {
  _out=/tmp/ws-api.out
  if [ $# -ge 3 ]; then
    _code=$(curl -sS -o "$_out" -w '%{http_code}' -b "$WS_COOKIES" -H "X-CSRF-Token: $WS_CSRF" \
      -H 'Content-Type: application/json' -X "$1" -d "$3" "$WAYSTATION_URL/api/v1$2")
  else
    _code=$(curl -sS -o "$_out" -w '%{http_code}' -b "$WS_COOKIES" -H "X-CSRF-Token: $WS_CSRF" -X "$1" \
      "$WAYSTATION_URL/api/v1$2")
  fi
  if [ "$_code" -ge 400 ]; then
    printf 'ws_api %s %s -> %s: %s\n' "$1" "$2" "$_code" "$(cat "$_out")" >&2
    return 1
  fi
  cat "$_out"
}
