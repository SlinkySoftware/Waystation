#!/bin/sh
# Helm classic repositories with the real helm 3 and helm 4 clients (client-helm, client-helm4: alpine/helm + curl), the commands
# of the "Set me up" snippets: proxy (repo add, search, pull, template; chart cached and verified against the index
# digest), hosted (curl upload, ChartMuseum multipart API, index rebuilt by the worker, template, delete)
# and group (merged index, pulls from both members, upload through the group).
# Runs identically in record, replay and live mode; assertions use only what survives fixture scrubbing.
set -eu
. /harness/lib.sh

VERSION=3.12.2  # a released metrics-server chart (never changes; newer releases may appear in live mode)
proxy="$WAYSTATION_URL/repository/metrics-server"
hosted="$WAYSTATION_URL/repository/helm-hosted"
group="$WAYSTATION_URL/repository/helm-all"
export HELM_CACHE_HOME=/tmp/helm/cache HELM_CONFIG_HOME=/tmp/helm/config HELM_DATA_HOME=/tmp/helm/data
cd /tmp

header() {  # header <name> <file>: value of a response header in a curl -D dump (lower-case name)
  tr -d '\r' < "$2" | awk -v n="$1" 'index($0, ":") { k = tolower(substr($0, 1, index($0, ":") - 1));
    if (k == n) { v = substr($0, index($0, ":") + 1); sub(/^ +/, "", v) } } END { print v }'
}
status() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

wait_for_chart() {  # wait_for_chart <repo alias> <chart> <version> [absent]: until helm search sees (or not) it
  i=0
  while :; do
    helm repo update "$1" >/dev/null 2>&1 || true
    if helm search repo "$1/$2" --versions | awk '{print $2}' | grep -qx "$3"; then
      [ "${4:-}" = absent ] || return 0
    else
      [ "${4:-}" != absent ] || return 0
    fi
    i=$((i + 1)); [ "$i" -lt 60 ] || fail "index of $1: $2 $3 ${4:-present} after 60 s"; sleep 1
  done
}

step 'proxy: helm repo add + search (index rewritten to the repository)'
helm repo add ms "$proxy/" >/dev/null || fail 'helm repo add'
helm search repo ms/metrics-server --versions | grep -q "ms/metrics-server[[:space:]]*$VERSION" \
  || fail "search: $(helm search repo ms/metrics-server --versions | head -5)"
curl -fsS "$proxy/index.yaml" > index.json
grep -q "\"_ext/https/github.com/kubernetes-sigs/metrics-server/releases/download/metrics-server-helm-chart-$VERSION/metrics-server-$VERSION.tgz\"" \
  index.json || fail 'chart URL not rewritten to the repository'
grep -q 'https://github.com/kubernetes-sigs/metrics-server/releases/download' index.json \
  && fail 'an upstream chart URL is left in the index'

step 'proxy: helm pull fetches the chart through Waystation (GitHub release + redirect), verified by digest'
helm pull ms/metrics-server --version "$VERSION" || fail 'helm pull'
[ -s "metrics-server-$VERSION.tgz" ] || fail 'no chart package'
tar -tzf "metrics-server-$VERSION.tgz" | grep -q '^metrics-server/Chart.yaml$' || fail 'not a chart package'
chart_url="$proxy/_ext/https/github.com/kubernetes-sigs/metrics-server/releases/download/metrics-server-helm-chart-$VERSION/metrics-server-$VERSION.tgz"
curl -fsS -D h1 -o again.tgz "$chart_url"
[ "$(header x-ws-cache h1)" = HIT ] || fail "second download cache state: $(header x-ws-cache h1)"
cmp -s "metrics-server-$VERSION.tgz" again.tgz || fail 'cached chart differs'
digest=$(sha256sum again.tgz | cut -d' ' -f1)
grep -q "\"digest\":\"$digest\"" index.json || fail 'the cached chart does not match the index digest'

step 'proxy: helm template renders the chart; unlisted paths are 404'
helm template ms-test ms/metrics-server --version "$VERSION" > rendered.yaml || fail 'helm template'
grep -q 'kind: Deployment' rendered.yaml || fail 'rendered output has no Deployment'
[ "$(status "$proxy/charts/not-listed-1.0.0.tgz")" = 404 ] || fail 'unlisted chart not 404'
[ "$(status "$proxy/_ext/https/github.com/x.tgz")" = 404 ] || fail 'unlisted external chart not 404'

step 'hosted: package charts, upload with curl (PUT) and the ChartMuseum API (POST multipart)'
ws_login
ws_token '["read", "write", "delete"]'
token=$WS_TOKEN
helm create mychart >/dev/null
mkdir -p pulled via-group
helm package mychart --version 0.1.0 >/dev/null
helm package mychart --version 0.2.0 >/dev/null
helm package mychart --version 0.3.0 >/dev/null
[ "$(status "$hosted/index.yaml")" = 401 ] || fail 'anonymous not challenged by the hosted repository (ADR 0005)'
curl -fsS -u "ci-bot:$token" --upload-file mychart-0.1.0.tgz "$hosted/" >/dev/null || fail 'curl upload'
curl -fsS -u "ci-bot:$token" -F chart=@mychart-0.2.0.tgz "$hosted/api/charts" | grep -q '"saved":true' \
  || fail 'ChartMuseum upload'
printf 'not a chart' > broken.tgz
[ "$(status -u "ci-bot:$token" --upload-file broken.tgz "$hosted/")" = 400 ] || fail 'a broken chart was accepted'

step 'hosted: helm repo add with an API token; the worker publishes the index'
helm repo add mine "$hosted/" --username ci-bot --password "$token" >/dev/null || fail 'helm repo add (hosted)'
wait_for_chart mine mychart 0.2.0
wait_for_chart mine mychart 0.1.0
helm pull mine/mychart --version 0.1.0 -d pulled >/dev/null || fail 'helm pull (hosted)'
cmp -s mychart-0.1.0.tgz pulled/mychart-0.1.0.tgz || fail 'pulled chart differs from the upload'
helm template demo mine/mychart --version 0.2.0 > dry.txt || fail 'helm template (hosted)'
grep -q 'kind: Deployment' dry.txt || fail 'the hosted chart rendered no Deployment'

step 'group: one index for both members; pulls from each; uploads land in the hosted member'
helm repo add all "$group/" --username ci-bot --password "$token" >/dev/null || fail 'helm repo add (group)'
helm search repo all/ --versions > all.txt
grep -q "all/mychart[[:space:]]*0.2.0" all.txt || fail "group search: $(cat all.txt)"
grep -q "all/metrics-server[[:space:]]*$VERSION" all.txt || fail "group search (proxy member): $(cat all.txt)"
helm pull all/mychart --version 0.2.0 -d via-group >/dev/null || fail 'helm pull mychart through the group'
helm pull all/metrics-server --version "$VERSION" -d via-group >/dev/null || fail 'helm pull metrics-server via group'
cmp -s "metrics-server-$VERSION.tgz" "via-group/metrics-server-$VERSION.tgz" || fail 'group copy differs'
curl -fsS -u "ci-bot:$token" --upload-file mychart-0.3.0.tgz "$group/" >/dev/null || fail 'upload through the group'
wait_for_chart mine mychart 0.3.0

step 'hosted: delete a version; it leaves the index'
[ "$(status -u "ci-bot:$token" -X DELETE "$hosted/mychart-0.1.0.tgz")" = 204 ] || fail 'delete'
[ "$(status -u "ci-bot:$token" "$hosted/mychart-0.1.0.tgz")" = 404 ] || fail 'deleted chart still served'
wait_for_chart mine mychart 0.1.0 absent

step 'browse API: charts and versions'
api="$WAYSTATION_URL/api/v1/repositories"
curl -fsS "$api/metrics-server/helm/charts" | grep -q '"name":"metrics-server"' || fail 'browse proxy charts'
curl -fsS "$api/metrics-server/helm/versions?chart=metrics-server&limit=500" \
  | grep -q "\"version\":\"$VERSION\",.*\"cached\":true" || fail 'browse proxy versions'
curl -fsS -u "ci-bot:$token" "$api/helm-hosted/helm/versions?chart=mychart" | grep -q '"version":"0.3.0"' \
  || fail 'browse hosted versions'

pass
