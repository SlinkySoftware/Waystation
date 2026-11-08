# Shared helpers for client scripts (POSIX sh; sourced inside client containers as /harness/lib.sh).
# The harness CA is installed for curl via CURL_CA_BUNDLE; other clients read HARNESS_CA (see README).

step() { printf '\n==> [%s/%s] %s\n' "${HARNESS_FORMAT:-?}" "${HARNESS_MODE:-?}" "$*"; }
fail() { printf 'FAIL [%s/%s]: %s\n' "${HARNESS_FORMAT:-?}" "${HARNESS_MODE:-?}" "$*" >&2; exit 1; }
pass() { printf '\nPASS [%s/%s]\n' "${HARNESS_FORMAT:-?}" "${HARNESS_MODE:-?}"; }

# Wait until a URL answers (any HTTP status), e.g. after the stack starts.
wait_for() {
  i=0
  until if command -v curl >/dev/null; then curl -s -o /dev/null "$1"; else _ws_http GET "$1" >/dev/null; fi; do
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
# Sets WS_COOKIES (a curl cookie jar) and WS_CSRF for ws_api. Uses curl, or bash alone in images without curl
# (debian, ubuntu: see _ws_http).
ws_login() {
  [ -n "${HARNESS_ADMIN_PASSWORD:-}" ] || fail 'ws_login: HARNESS_ADMIN_PASSWORD is not set'
  wait_for "$WAYSTATION_URL/healthz"
  if ! command -v curl >/dev/null; then
    WS_COOKIES=/tmp/ws-cookie-header WS_CSRF=
    rm -f "$WS_COOKIES"
    _code=$(_ws_http POST "$WAYSTATION_URL/api/v1/auth/login" \
      "$(printf '{"username":"admin","password":"%s"}' "$HARNESS_ADMIN_PASSWORD")") || fail 'ws_login: sign-in failed'
    [ "$_code" = 200 ] || fail "ws_login: sign-in returned $_code"
    WS_CSRF=$(sed -n 's/.*"csrf_token": *"\([^"]*\)".*/\1/p' /tmp/ws-api.out)
    [ -n "$WS_CSRF" ] || fail 'ws_login: no CSRF token in the session'
    return
  fi
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
  if ! command -v curl >/dev/null; then
    _code=$(_ws_http "$1" "$WAYSTATION_URL/api/v1$2" ${3+"$3"}) || return 1
  elif [ $# -ge 3 ]; then
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

# _ws_http <METHOD> <http URL> [json body]: a plain HTTP/1.0 request with bash alone (/dev/tcp), for client images
# without curl (debian-slim, ubuntu). Prints the status code; the body goes to /tmp/ws-api.out, the headers to
# /tmp/ws-api.headers. Sends the cookies kept in $WS_COOKIES (a Cookie header line, set by ws_login) and
# X-CSRF-Token: $WS_CSRF when set, and stores new cookies there.
_ws_http() {
  WS_COOKIES=${WS_COOKIES:-} WS_CSRF=${WS_CSRF:-} LC_ALL=C bash -c '
    set -eu
    rest=${2#http://}; hostport=${rest%%/*}; path=/${rest#*/}; [ "$rest" != "$hostport" ] || path=/
    host=${hostport%:*}; port=${hostport##*:}; [ "$port" != "$hostport" ] || port=80
    exec 3<>"/dev/tcp/$host/$port"
    {
      printf "%s %s HTTP/1.0\r\nHost: %s\r\nAccept: application/json\r\n" "$1" "$path" "$hostport"
      if [ -n "$WS_COOKIES" ] && [ -s "$WS_COOKIES" ]; then printf "Cookie: %s\r\n" "$(cat "$WS_COOKIES")"; fi
      if [ -n "$WS_CSRF" ]; then printf "X-CSRF-Token: %s\r\n" "$WS_CSRF"; fi
      if [ $# -ge 3 ]; then
        printf "Content-Type: application/json\r\nContent-Length: %s\r\n\r\n%s" "${#3}" "$3"
      else
        printf "\r\n"
      fi
    } >&3
    status= cookies=
    : > /tmp/ws-api.headers
    IFS= read -r line <&3; status=${line#* }; status=${status%% *}
    while IFS= read -r line <&3; do
      line=${line%$'\''\r'\''}
      [ -n "$line" ] || break
      printf "%s\n" "$line" >> /tmp/ws-api.headers
      case "${line,,}" in set-cookie:*) c=${line#*:}; c=${c# }; cookies="${cookies:+$cookies; }${c%%;*}" ;; esac
    done
    cat <&3 > /tmp/ws-api.out
    if [ -n "$WS_COOKIES" ] && [ -n "$cookies" ]; then printf "%s" "$cookies" > "$WS_COOKIES"; fi
    printf "%s\n" "$status"
  ' _ "$@"
}
