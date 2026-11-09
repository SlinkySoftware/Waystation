#!/bin/sh
# Go modules through Waystation (client-go container). The go command lists versions, downloads and builds a tiny
# module with two small public dependencies (one with upper-case letters in its path: case-encoding) through the
# proxy, verifying every module against sum.golang.org through the repository's checksum database proxy (GOSUMDB is
# left at its default; the go command finds <GOPROXY>/sumdb/sum.golang.org/supported). Then the same through the
# group with an API token in .netrc (through a TLS front: the go command sends credentials over HTTPS only), a token
# limited to other repositories, and the browse API.
# The go command never talks to the upstreams: GOPROXY has no ",direct" and the fake upstream fails on any request
# it does not expect.
set -eu
. /harness/lib.sh

W=$WAYSTATION_URL
work=/tmp/go-harness
rm -rf "$work"
mkdir -p "$work/src"
export GOTOOLCHAIN=local GOFLAGS=-mod=mod GONOSUMDB= GOPRIVATE= GONOPROXY= GOTELEMETRY=off
export GOSUMDB=sum.golang.org  # the default, verified through the proxy
UUID=github.com/google/uuid SEMVER=github.com/Masterminds/semver/v3

fresh() {  # fresh <name>: an empty module cache (and checksum database cache) for one scenario
  export GOPATH="$work/$1" GOMODCACHE="$work/$1/pkg/mod" GOCACHE="$work/$1/cache"
  rm -rf "$GOPATH"
}

cat > "$work/src/go.mod" <<'MOD'
module example.com/harness

go 1.24

require (
	github.com/Masterminds/semver/v3 v3.3.1
	github.com/google/uuid v1.6.0
)
MOD
cat > "$work/src/main.go" <<'SRC'
package main

import (
	"fmt"

	"github.com/Masterminds/semver/v3"
	"github.com/google/uuid"
)

func main() {
	v := semver.MustParse("1.2.3")
	id := uuid.NewSHA1(uuid.NameSpaceURL, []byte("https://example.com/"))
	fmt.Println(v.IncMinor().String(), int(id.Version()))
}
SRC

build() {  # build <GOPROXY> <label>: download (writing go.sum), verify and build the module from a clean cache
  # One module at a time first: the go command proves each lookup's tree against the newest tree it has seen, so
  # the checksum database tiles it asks for depend on the order of the lookups (parallel downloads would make the
  # requests differ between runs and miss fixtures).
  for module in "$UUID@v1.6.0" "$SEMVER@v3.3.1"; do
    (cd /tmp && GOFLAGS= GOPROXY="$1" go mod download "$module" 2>"$work/download.log") \
      || { grep -v -i token "$work/download.log" | tail -n 30; fail "go mod download $module through the $2"; }
  done
  rm -f "$work/src/go.sum"
  (cd "$work/src" && GOPROXY="$1" go mod download 2>"$work/download.log") \
    || { grep -v -i token "$work/download.log" | tail -n 30; fail "go mod download through the $2"; }
  out=$(cd "$work/src" && GOPROXY="$1" go run .) || fail "go run through the $2"
  [ "$out" = '1.3.0 5' ] || fail "unexpected program output through the $2: $out"
  grep -q "^$SEMVER v3.3.1 h1:" "$work/src/go.sum" && grep -q "^$UUID v1.6.0 h1:" "$work/src/go.sum" \
    || fail "go.sum is incomplete after building through the $2"
  [ -f "$GOPATH/pkg/sumdb/sum.golang.org/latest" ] \
    || fail "the go command did not verify against the checksum database through the $2"
}

proxy="$W/repository/go-proxy"
step 'the proxy announces the checksum database proxy'
curl -fsS -o /dev/null "$proxy/sumdb/sum.golang.org/supported" || fail 'sumdb supported'

step 'go mod download + go run through the proxy (sum.golang.org through the proxy)'
fresh proxy
build "$proxy" proxy

step 'go list -m -versions through the proxy (@v/list)'
versions=$(cd "$work/src" && GOPROXY="$proxy" go list -m -versions "$UUID") || fail 'go list -m -versions'
case "$versions" in *v1.6.0*) ;; *) fail "go list: $versions" ;; esac

step 'case-encoded and decoded module paths name the same cached file'
code=$(curl -sS -o /dev/null -w '%{http_code}' "$proxy/github.com/!masterminds/semver/v3/@v/v3.3.1.mod")
[ "$code" = 200 ] || fail "escaped .mod: $code"
code=$(curl -sS -o /dev/null -w '%{http_code}' "$proxy/github.com/Masterminds/semver/v3/@v/v3.3.1.mod")
[ "$code" = 200 ] || fail "upper-case .mod: $code"
code=$(curl -sS -o /dev/null -w '%{http_code}' "$proxy/github.com/google/uuid/@v/not-a-version.zip")
[ "$code" = 404 ] || fail "non-canonical .zip: $code"

step 'start a TLS front (the go command sends credentials over HTTPS only)'
# tlsfront.go: a standard-library reverse proxy with a self-signed certificate for localhost, trusted via
# SSL_CERT_FILE, in front of the plain-HTTP harness stack.
(cd /tmp && GOFLAGS= GOPROXY=off GOPATH="$work/front" GOCACHE="$work/front/cache" \
  go build -o "$work/tlsfront" /harness/go/tlsfront.go) || fail 'build tlsfront'
"$work/tlsfront" 127.0.0.1:443 "$W" "$work/front.crt" &
front_pid=$!
trap 'kill $front_pid 2>/dev/null || true' EXIT
i=0
until [ -s "$work/front.crt" ] && curl -fsS --cacert "$work/front.crt" -o /dev/null https://localhost/healthz; do
  i=$((i + 1)); [ "$i" -lt 30 ] || fail 'tlsfront did not start'; sleep 0.5
done
export SSL_CERT_FILE="$work/front.crt" NETRC="$work/netrc"
group=https://localhost/repository/go-group  # port 443: .netrc machines carry no port

netrc() {  # netrc <token>: the go command's credentials for the TLS front (API token as the password)
  printf 'machine localhost\nlogin harness\npassword %s\n' "$1" > "$NETRC"
  chmod 600 "$NETRC"
}

step 'the group needs credentials'
code=$(curl -sS -o /dev/null -w '%{http_code}' "$group/github.com/google/uuid/@v/list")
[ "$code" = 404 ] || fail "anonymous group read: $code"

step 'go mod download + go run through the group with an API token in .netrc (served from the cache)'
ws_login
ws_token '["read"]' '["go-*"]'
netrc "$WS_TOKEN"
curl -fsS --netrc-file "$NETRC" -o /dev/null "$group/github.com/google/uuid/@v/list" || fail 'group read with the token'
fresh group
build "$group" group

step 'a token limited to other repositories cannot read the group'
ws_token '["read"]' '["docker-*"]'
netrc "$WS_TOKEN"
fresh narrow
rm -f "$work/src/go.sum"
if (cd "$work/src" && GOPROXY="$group" go mod download) >/dev/null 2>&1; then
  fail 'a token limited to docker-* could read the Go group'
fi

step 'browse API: cached modules and versions'
i=0
until curl -fsS "$W/api/v1/repositories/go-proxy/go/modules?q=uuid" | grep -q '"module":"github.com/google/uuid"'; do
  i=$((i + 1)); [ "$i" -lt 40 ] || fail 'browse: uuid not listed'; sleep 0.5
done
page=$(curl -fsS "$W/api/v1/repositories/go-proxy/go/versions?module=github.com/Masterminds/semver/v3") \
  || fail 'browse: semver versions'
printf '%s' "$page" | grep -q '"version":"v3.3.1"' || fail "browse: v3.3.1 missing: $page"
printf '%s' "$page" | grep -q '"kind":"zip"' || fail "browse: no cached zip: $page"

pass
