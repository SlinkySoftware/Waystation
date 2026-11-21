#!/bin/sh
# Swift Package Registry with the real Swift Package Manager (client-swift: swift 6.4 + curl), the commands of the
# "Set me up" snippets: an SCM-backed proxy of github.com (JohnSundell/Files 4.2.0 resolved by registry identity and,
# with --replace-scm-with-registry, from its Git URL through /identifiers; the source archive is built once from the
# tag and its checksum pinned), and a hosted registry (swift package-registry login with an API token, publish,
# resolve and build against it, immutable versions, removal answered 410; anonymous, wrong-token and missing-scope
# requests refused). SwiftPM talks to registries over HTTPS only: it reaches the plain-HTTP harness stack through a TLS
# front (go/tlsfront.go, built into the image). Runs identically in record, replay and live mode; the client never
# reaches github.com itself (the fake upstream fails the run on any request it does not expect).
set -eu
. /harness/lib.sh

PKG=JohnSundell.Files
VERSION=4.2.0  # a released tag (never changes)
work=/tmp/swift-harness
rm -rf "$work"
mkdir -p "$work/home"
cd "$work"
export HOME="$work/home"
proxy=https://localhost/repository/swift-proxy/
hosted=https://localhost/repository/swift-hosted/
ACCEPT='Accept: application/vnd.swift.registry.v1+json'

step 'start a TLS front and trust it (SwiftPM talks to registries over HTTPS only)'
tlsfront 127.0.0.1:443 "$WAYSTATION_URL" "$work/front.crt" &
front_pid=$!
trap 'kill $front_pid 2>/dev/null || true' EXIT
i=0
until [ -s "$work/front.crt" ] && curl -fsS --cacert "$work/front.crt" -o /dev/null https://localhost/healthz; do
  i=$((i + 1)); [ "$i" -lt 30 ] || fail 'tlsfront did not start'; sleep 0.5
done
cp "$work/front.crt" /usr/local/share/ca-certificates/waystation-front.crt
update-ca-certificates >/dev/null 2>&1 || fail 'update-ca-certificates'

header() {  # header <name> <file>: value of a response header in a curl -D dump (lower-case name)
  tr -d '\r' < "$2" | awk -v n="$1" 'index($0, ":") { k = tolower(substr($0, 1, index($0, ":") - 1));
    if (k == n) { v = substr($0, index($0, ":") + 1); sub(/^ +/, "", v) } } END { print v }'
}
status() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
manifest() {  # manifest <dir> <dependency line> <product> <package>: a consumer package with one dependency
  mkdir -p "$1/Sources/Consumer"
  cat > "$1/Package.swift" <<EOF
// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Consumer",
    dependencies: [
        $2,
    ],
    targets: [
        .executableTarget(name: "Consumer", dependencies: [.product(name: "$3", package: "$4")]),
    ]
)
EOF
  printf 'import %s\nprint("ok")\n' "$3" > "$1/Sources/Consumer/main.swift"
}
resolved_from_registry() {  # resolved_from_registry <dir> <identity> <version>
  r="$1/Package.resolved"
  [ -s "$r" ] || fail "no Package.resolved in $1"
  tr -d ' \n' < "$r" | grep -q "\"identity\":\"$2\",\"kind\":\"registry\"" || fail "$2 not resolved from the registry: $(cat "$r")"
  tr -d ' \n' < "$r" | grep -q "\"version\":\"$3\"" || fail "$2 is not at $3: $(cat "$r")"
}

step "proxy: the release list of $PKG (tags on github.com), API version 1"
curl -fsS -D h1 -H "$ACCEPT" -o releases.json "${proxy}JohnSundell/Files" || fail 'release list'
[ "$(header content-version h1)" = 1 ] || fail "Content-Version: $(header content-version h1)"
grep -q "\"$VERSION\":{\"url\":" releases.json || fail "release list without $VERSION: $(head -c 400 releases.json)"
header link h1 | grep -q 'rel="canonical"' || fail "no canonical link: $(header link h1)"
[ "$(status -H 'Accept: application/vnd.swift.registry.v2+json' "${proxy}JohnSundell/Files")" = 415 ] \
  || fail 'an unsupported API version is not 415'

step "proxy: /identifiers maps the Git URL to $PKG"
curl -fsS -H "$ACCEPT" "${proxy}identifiers?url=https://github.com/JohnSundell/Files.git" > ids.json || fail 'identifiers'
grep -q "\"$PKG\"" ids.json || fail "identifiers: $(cat ids.json)"
[ "$(status -H "$ACCEPT" "${proxy}identifiers?url=https://example.com/a/b")" = 404 ] || fail 'a foreign URL has an id'

step "proxy: swift package resolve by registry identity ($PKG $VERSION)"
manifest "$work/by-id" ".package(id: \"$PKG\", exact: \"$VERSION\")" Files "$PKG"
(cd "$work/by-id" && swift package-registry set "$proxy" >/dev/null && swift package resolve >"$work/resolve-id.log" 2>&1) \
  || { tail -n 40 "$work/resolve-id.log"; fail 'resolve by identity'; }
resolved_from_registry "$work/by-id" johnsundell.files "$VERSION"

step 'proxy: the source archive is pinned: same checksum as the release information, built once'
curl -fsS -H "$ACCEPT" "${proxy}JohnSundell/Files/$VERSION" > release.json || fail 'release information'
want=$(sed -n 's/.*"checksum":"\([0-9a-f]\{64\}\)".*/\1/p' release.json)
[ -n "$want" ] || fail "no checksum: $(head -c 400 release.json)"
curl -fsS -D h2 -o files.zip "${proxy}JohnSundell/Files/$VERSION.zip" || fail 'archive'
[ "$(sha256sum files.zip | cut -d' ' -f1)" = "$want" ] || fail 'the archive does not match its checksum'
[ "$(header x-ws-cache h2)" = HIT ] || fail "archive cache state: $(header x-ws-cache h2)"
header digest h2 | grep -q '^sha-256=' || fail "no Digest header: $(header digest h2)"
swift package compute-checksum files.zip > computed.txt 2>"$work/checksum.log" || fail 'compute-checksum'
[ "$(cat computed.txt)" = "$want" ] || fail "swift package compute-checksum disagrees: $(cat computed.txt)"
unzip -l files.zip | grep -q ' Files/Package.swift$' || fail 'the archive has no <name>/Package.swift'
curl -fsS -D h3 -o Package.swift "${proxy}JohnSundell/Files/$VERSION/Package.swift" || fail 'manifest'
head -1 Package.swift | grep -q 'swift-tools-version' || fail 'manifest content'
[ "$(header content-type h3)" = 'text/x-swift' ] || fail "manifest type: $(header content-type h3)"

step 'proxy: swift package resolve --replace-scm-with-registry (a url: dependency served by the registry)'
manifest "$work/by-url" ".package(url: \"https://github.com/JohnSundell/Files\", exact: \"$VERSION\")" Files Files
(cd "$work/by-url" && swift package-registry set "$proxy" >/dev/null \
  && swift package resolve --replace-scm-with-registry >"$work/resolve-url.log" 2>&1) \
  || { tail -n 40 "$work/resolve-url.log"; fail 'resolve --replace-scm-with-registry'; }
resolved_from_registry "$work/by-url" johnsundell.files "$VERSION"

step 'hosted: swift package-registry login with an API token'
ws_login
ws_token '["read", "write", "delete"]'
token=$WS_TOKEN
lib="$work/LinkedList"
mkdir -p "$lib/Sources/LinkedList"
cat > "$lib/Package.swift" <<'EOF'
// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "LinkedList",
    products: [.library(name: "LinkedList", targets: ["LinkedList"])],
    targets: [.target(name: "LinkedList")]
)
EOF
printf 'public struct LinkedList { public init() {}\n  public var count: Int { 0 }\n}\n' > "$lib/Sources/LinkedList/LinkedList.swift"
cat > "$lib/package-metadata.json" <<'EOF'
{"description": "One thing links to another.", "repositoryURLs": ["https://example.com/mona/LinkedList"],
 "author": {"name": "Harness"}}
EOF
cd "$lib"
if swift package-registry login "${hosted}login" --token not-a-token --no-confirm >"$work/login-bad.log" 2>&1; then
  fail 'login with a wrong token succeeded'
fi
swift package-registry set "$hosted" >/dev/null
if swift package-registry publish mona.LinkedList 1.0.0 >"$work/publish-anon.log" 2>&1; then
  fail 'anonymous publish succeeded'
fi
swift package-registry login "${hosted}login" --token "$token" --no-confirm >"$work/login.log" 2>&1 \
  || { cat "$work/login.log"; fail 'login'; }

step 'hosted: swift package-registry publish mona.LinkedList 1.0.0'
swift package-registry publish mona.LinkedList 1.0.0 >"$work/publish.log" 2>&1 || { tail -n 30 "$work/publish.log"; fail 'publish'; }
if swift package-registry publish mona.LinkedList 1.0.0 >"$work/again.log" 2>&1; then
  fail 'publishing the same version twice succeeded'
fi
grep -q 'already exists' "$work/again.log" || fail "second publish: $(tail -n 5 "$work/again.log")"
cd "$work"
auth="Authorization: Bearer $token"
curl -fsS -H "$ACCEPT" -H "$auth" "${hosted}mona/LinkedList/1.0.0" > hosted.json || fail 'hosted release information'
grep -q '"description":"One thing links to another."' hosted.json || fail "metadata: $(cat hosted.json)"
curl -fsS -H "$ACCEPT" -H "$auth" "${hosted}identifiers?url=https://example.com/mona/LinkedList.git" | grep -q '"mona.LinkedList"' \
  || fail 'identifiers from the release metadata'
[ "$(status -H "$ACCEPT" "${hosted}mona/LinkedList/1.0.0")" = 401 ] || fail 'anonymous read of the hosted registry not 401'

step 'hosted: resolve and build a package that depends on mona.LinkedList (credentials from the login)'
consumer="$work/consumer"
manifest "$consumer" '.package(id: "mona.LinkedList", from: "1.0.0")' LinkedList mona.LinkedList
printf 'import LinkedList\nprint(LinkedList().count)\n' > "$consumer/Sources/Consumer/main.swift"
(cd "$consumer" && swift package-registry set "$hosted" >/dev/null && swift build >"$work/build.log" 2>&1) \
  || { tail -n 40 "$work/build.log"; fail 'resolve and build against the hosted registry'; }
resolved_from_registry "$consumer" mona.linkedlist 1.0.0
[ "$("$consumer/.build/debug/Consumer")" = 0 ] || fail 'the consumer did not run'

step 'hosted: without credentials the registry refuses the resolve'
(cd "$consumer" && swift package-registry logout "${hosted}login" >/dev/null 2>&1 || true)
rm -rf "$consumer/.build" "$consumer/Package.resolved" "$HOME/.cache" "$HOME/.swiftpm/cache" "$HOME/.netrc"
if (cd "$consumer" && swift package resolve >"$work/resolve-anon.log" 2>&1); then
  fail 'resolve without credentials succeeded'
fi
grep -qi -e '401' -e 'unauthorized' -e 'authentication' "$work/resolve-anon.log" \
  || fail "resolve without credentials failed for another reason: $(tail -n 10 "$work/resolve-anon.log")"

step 'hosted: a token limited to other repositories cannot publish; removal answers 410 and keeps the version'
ws_token '["read", "write"]' '["swift-proxy"]'
other=$WS_TOKEN
case "$(status -X POST -H "Authorization: Bearer $other" "${hosted}login")" in
  403|404) ;;
  *) fail 'a token for other repositories signs in to the hosted registry' ;;
esac
[ "$(status -X DELETE -H "$auth" "${hosted}mona/LinkedList/1.0.0")" = 204 ] || fail 'remove'
[ "$(status -H "$ACCEPT" -H "$auth" "${hosted}mona/LinkedList/1.0.0")" = 410 ] || fail 'a removed release is not 410'
curl -fsS -H "$ACCEPT" -H "$auth" "${hosted}mona/LinkedList" | grep -q '"status":410' || fail 'the list does not mark the removal'
pass
