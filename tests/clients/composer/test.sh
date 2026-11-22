#!/bin/sh
# Composer through Waystation (client-composer container: the official composer image). composer installs a tiny
# public package through the repo.packagist.org proxy (its archive fetched, checked and pinned by Waystation), uploads
# a generated package to the hosted repository with an API token (curl, the documented upload endpoint), installs it
# through the hosted repository and through the group (with the proxied package), and checks that anonymous reads of
# the hosted repository and a wrong token are refused. Packagist itself is disabled in every project: the client only
# ever talks to Waystation (the metadata Waystation serves names only Waystation archive URLs).
set -eu
. /harness/lib.sh

W=$WAYSTATION_URL
HOST=${W#*://}
work=/tmp/composer-harness
rm -rf "$work"
mkdir -p "$work/home"
export COMPOSER_HOME="$work/home" COMPOSER_CACHE_DIR="$work/cache" COMPOSER_ALLOW_SUPERUSER=1
export COMPOSER_NO_INTERACTION=1 COMPOSER_NO_AUDIT=1
PUBLIC=psr/container
PUBLIC_VERSION=2.0.2
DEMO=ws-harness/demo

project() {  # project <dir> <repository> <require JSON members>: a project using only that repository
  mkdir -p "$1"
  cat > "$1/composer.json" <<EOF
{
  "name": "ws-harness/consumer",
  "type": "project",
  "repositories": [
    {"type": "composer", "url": "$W/repository/$2/"},
    {"packagist.org": false}
  ],
  "require": {$3},
  "config": {"secure-http": false, "preferred-install": "dist", "allow-plugins": false}
}
EOF
}

auth() {  # auth <dir> <token>: auth.json with an API token as the http-basic password
  printf '{"http-basic": {"%s": {"username": "admin", "password": "%s"}}}\n' "$HOST" "$2" > "$1/auth.json"
}

install() {  # install <dir>: a fresh install (empty cache), no lock file
  rm -rf "$COMPOSER_CACHE_DIR" "$1/vendor" "$1/composer.lock"
  (cd "$1" && composer update --no-progress --no-plugins --no-scripts > "$1/composer.log" 2>&1) \
    || { tail -n 40 "$1/composer.log"; return 1; }
}

step 'API token for the repositories'
ws_login
ws_token '["read", "write", "delete"]' '["composer-*"]'
token=$WS_TOKEN

step "composer install through the proxy ($PUBLIC $PUBLIC_VERSION, archive fetched, checked and pinned)"
project "$work/proxy" composer-proxy "\"$PUBLIC\": \"$PUBLIC_VERSION\""
auth "$work/proxy" "$token"
install "$work/proxy" || fail 'install through the proxy'
[ -f "$work/proxy/vendor/psr/container/src/ContainerInterface.php" ] || fail 'psr/container is not installed'
grep -q "/repository/composer-proxy/dists/psr/container/" "$work/proxy/composer.lock" \
  || fail 'the lock file does not point at the repository'

step 'a wrong token is refused'
auth "$work/proxy" wrong-token
if install "$work/proxy" > /dev/null 2>&1; then
  fail 'an install with a wrong token succeeded'
fi

step "upload a generated package to the hosted repository ($DEMO 1.0.0 and 1.1.0)"
for version in 1.0.0 1.1.0; do
  pkg="$work/pkg-$version"
  mkdir -p "$pkg/src"
  cat > "$pkg/composer.json" <<EOF
{
  "name": "$DEMO",
  "description": "A generated package for the Waystation client harness",
  "type": "library",
  "license": "MIT",
  "require": {"php": ">=8.1"},
  "autoload": {"psr-4": {"WsHarness\\\\Demo\\\\": "src/"}}
}
EOF
  printf '<?php\nnamespace WsHarness\\Demo;\nfinal class Demo { public const VERSION = "%s"; }\n' "$version" > "$pkg/src/Demo.php"
  (cd "$pkg" && zip -q -r "$work/demo-$version.zip" .) || fail "zip $version"
  code=$(curl -s -o "$work/upload.out" -w '%{http_code}' -u "admin:$token" --upload-file "$work/demo-$version.zip" \
    "$W/repository/composer-hosted/packages/upload/$DEMO/$version")
  [ "$code" = 201 ] || { cat "$work/upload.out"; fail "upload $version answered $code"; }
done
code=$(curl -s -o /dev/null -w '%{http_code}' --upload-file "$work/demo-1.0.0.zip" \
  "$W/repository/composer-hosted/packages/upload/$DEMO/9.0.0")
[ "$code" = 401 ] || fail "an anonymous upload answered $code"

step 'composer install from the hosted repository (credentials from auth.json)'
project "$work/hosted" composer-hosted "\"$DEMO\": \"1.0.0\""
auth "$work/hosted" "$token"
install "$work/hosted" || fail 'install from the hosted repository'
grep -q 'VERSION = "1.0.0"' "$work/hosted/vendor/ws-harness/demo/src/Demo.php" || fail '1.0.0 is not installed'

step 'composer install through the group: the uploaded package and the proxied one'
project "$work/group" composer-group "\"$DEMO\": \"^1.0\", \"$PUBLIC\": \"$PUBLIC_VERSION\""
auth "$work/group" "$token"
install "$work/group" || fail 'install through the group'
grep -q 'VERSION = "1.1.0"' "$work/group/vendor/ws-harness/demo/src/Demo.php" || fail '^1.0 did not resolve to 1.1.0'
[ -f "$work/group/vendor/psr/container/src/ContainerInterface.php" ] || fail 'psr/container not installed via group'

step 'an anonymous install from the hosted repository is refused'
project "$work/anonymous" composer-hosted "\"$DEMO\": \"1.0.0\""
if install "$work/anonymous" > /dev/null 2>&1; then
  fail 'an anonymous install from the hosted repository succeeded'
fi

step 'delete a hosted version; the group then resolves ^1.0 to 1.0.0'
code=$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$token" -X DELETE \
  "$W/repository/composer-hosted/packages/upload/$DEMO/1.1.0")
[ "$code" = 204 ] || fail "delete answered $code"
install "$work/group" || fail 'install through the group after the delete'
grep -q 'VERSION = "1.0.0"' "$work/group/vendor/ws-harness/demo/src/Demo.php" || fail '^1.0 did not resolve to 1.0.0'

pass
