#!/bin/sh
# Raw proxy with real clients (client-curl container: curl + BusyBox wget), the commands of the "Set me up" snippets.
# Runs identically in record, replay and live mode; assertions use only what survives fixture scrubbing.
set -eu
. /harness/lib.sh

repo="$WAYSTATION_URL/repository/debian-files"
cd /tmp

header() {  # header <name> <file>: value of a response header in a curl -D dump (lower-case name)
  tr -d '\r' < "$2" | awk -v n="$1" 'index($0, ":") { k = tolower(substr($0, 1, index($0, ":") - 1));
    if (k == n) { v = substr($0, index($0, ":") + 1); sub(/^ +/, "", v) } } END { print v }'
}

step 'curl -fSLO: first download is fetched from the upstream'
curl -fsSLO -D h1 "$repo/README.mirrors.txt"
[ -s README.mirrors.txt ] || fail 'empty download'
grep -qi 'mirror' README.mirrors.txt || fail "unexpected content: $(cat README.mirrors.txt)"
[ "$(header x-ws-cache h1)" = FETCHING ] || fail "first request cache state: $(header x-ws-cache h1)"
case "$(header content-type h1)" in text/plain*) ;; *) fail "content type: $(header content-type h1)" ;; esac
header content-disposition h1 | grep -q '^attachment; filename="README.mirrors.txt"' || fail 'content-disposition'

step 'second download is a cache hit served by the web tier, byte-identical'
curl -fsS -D h2 -o again.txt "$repo/README.mirrors.txt"
[ "$(header x-ws-cache h2)" = HIT ] || fail "second request cache state: $(header x-ws-cache h2)"
cmp -s README.mirrors.txt again.txt || fail 'cached copy differs from the first download'
size=$(wc -c < README.mirrors.txt | tr -d ' ')

step 'HEAD and conditional requests on the cached file'
curl -fsSI "$repo/README.mirrors.txt" > h3
[ "$(header content-length h3)" = "$size" ] || fail "HEAD content-length $(header content-length h3) != $size"
etag=$(header etag h2)
[ -n "$etag" ] || fail 'no ETag on the cached file'
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "If-None-Match: $etag" "$repo/README.mirrors.txt")
[ "$code" = 304 ] || fail "If-None-Match returned $code"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "If-Modified-Since: $(header last-modified h2)" \
  "$repo/README.mirrors.txt")
[ "$code" = 304 ] || fail "If-Modified-Since returned $code"
code=$(curl -sS -o /dev/null -w '%{http_code}' -r 0-9 "$repo/README.mirrors.txt")
[ "$code" = 206 ] || fail "Range request returned $code"

step 'wget downloads through the repository'
wget -q -O README "$repo/README"
grep -q 'Debian' README || fail 'wget: unexpected README content'
wget -q -O README.2 "$repo/README"
cmp -s README README.2 || fail 'wget: second copy differs'

step 'paths matching the metadata regex are revalidated upstream'
curl -fsS -D m1 -o trace1 "$repo/project/trace/master"
[ "$(header x-ws-cache m1)" = FETCHING ] || fail "trace first: $(header x-ws-cache m1)"
curl -fsS -D m2 -o trace2 "$repo/project/trace/master"
state=$(header x-ws-cache m2)
if [ "$HARNESS_MODE" = replay ]; then
  [ "$state" = REVALIDATED ] || fail "trace second: $state"
else  # the live trace file can change between two requests
  case "$state" in REVALIDATED|FETCHING) ;; *) fail "trace second: $state" ;; esac
fi

step 'a missing file is a native 404, also when asked again (negative cache)'
for _ in 1 2; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' "$repo/no-such-file-for-the-harness.txt")
  [ "$code" = 404 ] || fail "missing file returned $code"
done

step 'directories are not listed (later task) and writes are refused'
code=$(curl -sS -o /dev/null -w '%{http_code}' "$repo/")
[ "$code" = 404 ] || fail "repository root returned $code"
code=$(curl -sS -o /dev/null -w '%{http_code}' -T README "$repo/upload.txt")
[ "$code" = 401 ] || fail "anonymous PUT returned $code"

pass
