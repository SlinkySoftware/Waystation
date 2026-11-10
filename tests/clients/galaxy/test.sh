#!/bin/sh
# Ansible Galaxy with the real ansible-galaxy client (client-ansible: ansible-core 2.22 + curl), the commands of the
# "Set me up" snippets: proxy (collection install from galaxy.ansible.com through Waystation, artifact cached and
# verified against the version's sha256; a role through the legacy v1 roles API), hosted (collection build, publish
# with an API token, import task polled until completed, versions immutable, delete) and group (install of both the
# published and the proxied collection, publish through the group, ansible.cfg server list with a token).
# Runs identically in record, replay and live mode; assertions use only what survives fixture scrubbing.
set -eu
. /harness/lib.sh

COLL=community.library_inventory_filtering_v1
NS=community
NAME=library_inventory_filtering_v1
VERSION=1.1.5  # a released version (never changes; newer releases may appear in live mode)
ROLE=geerlingguy.ntp
ROLE_VERSION=4.0.1
proxy="$WAYSTATION_URL/repository/galaxy-proxy/"
hosted="$WAYSTATION_URL/repository/galaxy-hosted/"
group="$WAYSTATION_URL/repository/galaxy-all/"
export HOME=/tmp/home ANSIBLE_LOCAL_TEMP=/tmp/home/tmp ANSIBLE_GALAXY_CACHE_DIR=/tmp/home/galaxy-cache
export ANSIBLE_GALAXY_DISABLE_GPG_VERIFY=1
mkdir -p "$HOME" && cd /tmp

header() {  # header <name> <file>: value of a response header in a curl -D dump (lower-case name)
  tr -d '\r' < "$2" | awk -v n="$1" 'index($0, ":") { k = tolower(substr($0, 1, index($0, ":") - 1));
    if (k == n) { v = substr($0, index($0, ":") + 1); sub(/^ +/, "", v) } } END { print v }'
}
status() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
installed() {  # installed <collections dir> <namespace> <name> <version>
  m="$1/ansible_collections/$2/$3/MANIFEST.json"
  [ -s "$m" ] || fail "$2.$3 not installed in $1"
  grep -q "\"version\": *\"$4\"" "$m" || fail "$2.$3 in $1 is not $4: $(grep -o '"version": *"[^"]*"' "$m" | head -1)"
}

step 'proxy: discovery answers the v3 API (and v1 for roles)'
curl -fsS "${proxy}api/" > discovery.json || fail 'discovery'
grep -q '"v3":"v3/"' discovery.json || fail "discovery: $(cat discovery.json)"
grep -q '"v1":"v1/"' discovery.json || fail "discovery without v1: $(cat discovery.json)"

step "proxy: ansible-galaxy collection install $COLL:==$VERSION (anonymous)"
ansible-galaxy collection install "$COLL:==$VERSION" -s "$proxy" -p c-proxy >install.log 2>&1 \
  || fail "install: $(cat install.log)"
installed c-proxy "$NS" "$NAME" "$VERSION"

step 'proxy: version document rewritten into the repository; the artifact is cached and matches its sha256'
api="${proxy}api/v3/collections/$NS/$NAME"
curl -fsS "$api/versions/$VERSION/" > version.json || fail 'version document'
grep -q "\"download_url\":\"${proxy}collections/$NS/$NAME/$NS-$NAME-$VERSION.tar.gz\"" version.json \
  || fail "download_url not rewritten: $(head -c 600 version.json)"
grep -q 'galaxy.ansible.com' version.json && fail 'an upstream URL is left in the version document'
want=$(sed -n 's/.*"sha256":"\([0-9a-f]\{64\}\)".*/\1/p' version.json)
[ -n "$want" ] || fail 'no artifact sha256'
curl -fsS -D h1 -o artifact.tar.gz "${proxy}collections/$NS/$NAME/$NS-$NAME-$VERSION.tar.gz" || fail 'artifact'
[ "$(header x-ws-cache h1)" = HIT ] || fail "second download cache state: $(header x-ws-cache h1)"
[ "$(sha256sum artifact.tar.gz | cut -d' ' -f1)" = "$want" ] || fail 'the cached artifact does not match its sha256'
curl -fsS "$api/versions/?limit=1" > page.json || fail 'version list'
grep -q '"next":"/repository/galaxy-proxy/api/v3/collections/' page.json || fail "pagination links: $(cat page.json)"
curl -fsS "${proxy}api/v3/plugin/ansible/content/published/collections/index/$NS/$NAME/" | grep -q '"highest_version"' \
  || fail 'Galaxy NG path alias'
[ "$(status "${proxy}collections/$NS/$NAME/$NS-$NAME-0.0.1.tar.gz")" = 404 ] || fail 'unknown version not 404'

step "proxy: ansible-galaxy role install $ROLE,$ROLE_VERSION (legacy v1 roles API, archive cached)"
ansible-galaxy role install "$ROLE,$ROLE_VERSION" -s "$proxy" -p roles >role.log 2>&1 || fail "role: $(cat role.log)"
[ -s roles/$ROLE/tasks/main.yml ] || fail "role not installed: $(cat role.log)"
grep -q "downloading role from ${proxy}roles/" role.log || fail "role archive not fetched through Waystation: $(cat role.log)"

step 'hosted: build a collection, publish it with an API token (import polled until completed)'
ws_login
ws_token '["read", "write", "delete"]'
token=$WS_TOKEN
ansible-galaxy collection init harness.demo --init-path src >/dev/null || fail 'collection init'
src=src/harness/demo
sed -i 's/^version: .*/version: 1.0.0/' "$src/galaxy.yml"
ansible-galaxy collection build "$src" --output-path dist >/dev/null || fail 'build 1.0.0'
sed -i 's/^version: .*/version: 1.1.0/' "$src/galaxy.yml"
ansible-galaxy collection build "$src" --output-path dist >/dev/null || fail 'build 1.1.0'
[ "$(status "${hosted}api/v3/collections/harness/demo/")" = 401 ] || fail 'anonymous not challenged (ADR 0005)'
ansible-galaxy collection publish dist/harness-demo-1.0.0.tar.gz -s "$hosted" --token "$token" >publish.log 2>&1 \
  || fail "publish: $(cat publish.log)"
if ansible-galaxy collection publish dist/harness-demo-1.0.0.tar.gz -s "$hosted" --token "$token" >again.log 2>&1; then
  fail 'a published version was replaced'
fi
grep -qi 'immutable\|exists' again.log || fail "republish refusal: $(cat again.log)"
[ "$(status -H "Authorization: Token $token" "${hosted}collections/harness/demo/harness-demo-1.0.0.tar.gz")" = 200 ] \
  || fail 'published artifact not served'

step 'group: publish through the group (lands in the hosted member); install both collections from the group'
ansible-galaxy collection publish dist/harness-demo-1.1.0.tar.gz -s "$group" --token "$token" >publish2.log 2>&1 \
  || fail "publish through the group: $(cat publish2.log)"
ansible-galaxy collection install harness.demo "$COLL:==$VERSION" -s "$group" --token "$token" -p c-group \
  >group.log 2>&1 || fail "install through the group: $(cat group.log)"
installed c-group harness demo 1.1.0
installed c-group "$NS" "$NAME" "$VERSION"
curl -fsS -H "Authorization: Token $token" "${group}api/v3/collections/harness/demo/versions/" > merged.json
grep -q '"version":"1.1.0"' merged.json && grep -q '"version":"1.0.0"' merged.json || fail "group versions: $(cat merged.json)"

step 'ansible.cfg: a server list with the token (the "Set me up" snippet); a pinned version from the group'
cat > ansible.cfg <<CFG
[galaxy]
server_list = waystation

[galaxy_server.waystation]
url = $group
token = $token
CFG
ANSIBLE_CONFIG=/tmp/ansible.cfg ansible-galaxy collection install 'harness.demo:==1.0.0' -p c-cfg >cfg.log 2>&1 \
  || fail "install with ansible.cfg: $(cat cfg.log)"
installed c-cfg harness demo 1.0.0

step 'hosted: delete a version; it is gone from the API and the artifact path'
[ "$(status -H "Authorization: Token $token" -X DELETE "${hosted}api/v3/collections/harness/demo/versions/1.0.0/")" = 204 ] \
  || fail 'delete'
[ "$(status -H "Authorization: Token $token" "${hosted}api/v3/collections/harness/demo/versions/1.0.0/")" = 404 ] \
  || fail 'deleted version still listed'
[ "$(status -H "Authorization: Token $token" "${hosted}collections/harness/demo/harness-demo-1.0.0.tar.gz")" = 404 ] \
  || fail 'deleted artifact still served'

step 'browse API: namespaces, collections and versions'
rest="$WAYSTATION_URL/api/v1/repositories"
curl -fsS "$rest/galaxy-proxy/galaxy/collections" | grep -q "\"name\":\"$NAME\"" || fail 'browse proxy collections'
curl -fsS "$rest/galaxy-proxy/galaxy/versions?namespace=$NS&collection=$NAME&limit=200" \
  | grep -q "\"version\":\"$VERSION\",.*\"cached\":true" || fail 'browse proxy versions'
curl -fsS -H "Authorization: Token $token" "$rest/galaxy-hosted/galaxy/namespaces" | grep -q '"namespace":"harness"' \
  || fail 'browse hosted namespaces'
curl -fsS -H "Authorization: Token $token" "$rest/galaxy-all/galaxy/versions?namespace=harness&collection=demo" \
  | grep -q '"version":"1.1.0"' || fail 'browse group versions'

pass
