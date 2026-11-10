#!/bin/sh
# Raw proxy, hosted and group with real clients (client-curl container: curl + BusyBox wget), the commands of the
# "Set me up" snippets.
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

step 'directory listings of the cache (HTML and JSON); writes to a proxy are refused'
curl -fsS -H 'Accept: application/json' "$repo/" > list.json || fail 'listing of the repository root'
grep -q '"name":"README.mirrors.txt","path":"README.mirrors.txt","kind":"file"' list.json \
  || fail "root listing: $(cat list.json)"
curl -fsS "$repo/project/trace/" | grep -q 'href="./master"' || fail 'HTML listing of project/trace/'
code=$(curl -sS -o /dev/null -w '%{http_code}' "$repo/no-such-folder/")
[ "$code" = 404 ] || fail "missing folder returned $code"
code=$(curl -sS -o /dev/null -w '%{http_code}' -T README "$repo/upload.txt")
[ "$code" = 401 ] || fail "anonymous PUT returned $code"

step 'browse API (anonymous): cached files in the folder listing'
api="$WAYSTATION_URL/api/v1/repositories/debian-files/browse"
curl -fsS "$api" > root.json || fail 'browse root'
grep -q '"name":"project","path":"project","kind":"folder"' root.json || fail "browse root: $(cat root.json)"
grep -q '"name":"README.mirrors.txt","path":"README.mirrors.txt","kind":"file"' root.json \
  || fail "browse root: $(cat root.json)"
curl -fsS "$api?path=project/trace" | grep -q '"path":"project/trace/master","kind":"file"' \
  || fail 'browse project/trace'

status() {  # status <curl args...>: the HTTP status of a request
  curl -sS -o /dev/null -w '%{http_code}' "$@"
}

step 'API token: basic auth (username + token, token as password) and bearer'
ws_login
ws_token '["read"]'
read_token=$WS_TOKEN read_id=$WS_TOKEN_ID
[ "$(status -u "ci-bot:$read_token" "$repo/README.mirrors.txt")" = 200 ] || fail 'basic auth with username + token'
[ "$(status -u ":$read_token" "$repo/README.mirrors.txt")" = 200 ] || fail 'basic auth with the token as password'
[ "$(status -H "Authorization: Bearer $read_token" "$repo/README.mirrors.txt")" = 200 ] || fail 'bearer token'

step 'API token: a read-only token cannot write (403, where anonymous gets 401)'
code=$(status -u "ci-bot:$read_token" -T README "$repo/upload.txt")
[ "$code" = 403 ] || fail "PUT with a read-only token returned $code"

step 'API token: a token limited to other repositories does not see this one (404)'
ws_token '["read"]' '["npm-*"]'
code=$(status -u "ci-bot:$WS_TOKEN" "$repo/README.mirrors.txt")
[ "$code" = 404 ] || fail "token limited to npm-* returned $code"

step 'API token: a revoked token is refused at once (401 with a Basic challenge)'
ws_revoke "$read_id"
curl -sS -o /dev/null -D h4 -u "ci-bot:$read_token" "$repo/README.mirrors.txt"
head -1 h4 | grep -q ' 401' || fail "revoked token: $(head -1 h4)"
header www-authenticate h4 | grep -q '^Basic realm="Waystation"' || fail 'no Basic challenge'

step 'hosted: curl uploads with a verified checksum (write token), anonymous does not see the repository'
ws_login
ws_token '["read", "write", "delete"]'
writer=$WS_TOKEN
hosted="$WAYSTATION_URL/repository/raw-uploads"
printf 'release notes for the harness\n' > notes.txt
sum=$(sha256sum notes.txt | cut -d' ' -f1)
curl -fsS -u "ci-bot:$writer" -H "X-Checksum-Sha256: $sum" -D u1 --upload-file notes.txt \
  "$hosted/releases/1.0/notes.txt" || fail 'upload'
[ "$(header etag u1)" = "\"sha256:$sum\"" ] || fail "upload ETag: $(header etag u1)"
code=$(status -u "ci-bot:$writer" -H "X-Checksum-Sha256: $(printf '%064d' 0)" --upload-file notes.txt \
  "$hosted/releases/1.0/bad.txt")
[ "$code" = 400 ] || fail "checksum mismatch returned $code"
[ "$(status "$hosted/releases/1.0/notes.txt")" = 401 ] || fail 'anonymous not challenged by the hosted repository'
# ADR 0005: curl --anyauth sends the credentials only after the 401 Basic challenge; a missing repository answers the
# same challenge, so anonymous callers cannot tell it from a hidden one.
[ "$(status --anyauth -u "ci-bot:$writer" "$hosted/releases/1.0/notes.txt")" = 200 ] || fail 'credentials after challenge'
[ "$(status "$WAYSTATION_URL/repository/no-such-raw-repo/releases/1.0/notes.txt")" = 401 ] || fail 'missing repository'

step 'hosted: curl and wget download what was uploaded; the folder is listed'
curl -fsS -u "ci-bot:$writer" -D d1 -o notes.down "$hosted/releases/1.0/notes.txt"
cmp -s notes.txt notes.down || fail 'downloaded file differs'
case "$(header content-type d1)" in text/plain*) ;; *) fail "content type: $(header content-type d1)" ;; esac
wget -q --header "Authorization: Bearer $writer" -O notes.wget "$hosted/releases/1.0/notes.txt" \
  || fail 'wget with credentials'
cmp -s notes.txt notes.wget || fail 'wget copy differs'
[ "$(status -u "ci-bot:$writer" "$hosted/releases/1.0/notes.txt" -H "If-None-Match: \"sha256:$sum\"")" = 304 ] \
  || fail 'conditional request on the uploaded file'
curl -fsS -u "ci-bot:$writer" -H 'Accept: application/json' "$hosted/releases/1.0/" \
  | grep -q "\"path\":\"releases/1.0/notes.txt\",\"kind\":\"file\",\"size\":$(wc -c < notes.txt | tr -d ' ')" \
  || fail 'JSON listing of releases/1.0/'

step 'group: reads the hosted member first and the proxy member next; uploads land in the writable member'
group="$WAYSTATION_URL/repository/raw-all"
curl -fsS -u "ci-bot:$writer" -D g1 -o group.notes "$group/releases/1.0/notes.txt"
cmp -s notes.txt group.notes || fail 'group read of the hosted file'
[ "$(header x-ws-group-member g1)" = raw-uploads ] || fail "served by $(header x-ws-group-member g1)"
curl -fsS -u "ci-bot:$writer" -D g2 -o group.readme "$group/README.mirrors.txt"
cmp -s README.mirrors.txt group.readme || fail 'group read of the cached proxy file'
[ "$(header x-ws-group-member g2)" = debian-files ] || fail "served by $(header x-ws-group-member g2)"
curl -fsS -u "ci-bot:$writer" --upload-file notes.txt "$group/via-group/notes.txt" || fail 'upload through the group'
[ "$(status -u "ci-bot:$writer" "$hosted/via-group/notes.txt")" = 200 ] || fail 'group upload not in raw-uploads'

step 'hosted: delete with curl, then the file is gone'
[ "$(status -u "ci-bot:$writer" -X DELETE "$hosted/releases/1.0/notes.txt")" = 204 ] || fail 'delete'
[ "$(status -u "ci-bot:$writer" "$hosted/releases/1.0/notes.txt")" = 404 ] || fail 'deleted file still served'
[ "$(status -u "ci-bot:$writer" -X DELETE "$hosted/releases/1.0/notes.txt")" = 404 ] || fail 'second delete'

pass
