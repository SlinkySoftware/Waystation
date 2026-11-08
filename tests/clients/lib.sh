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
