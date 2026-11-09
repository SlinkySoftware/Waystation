#!/bin/sh
# Docker / OCI proxy and hosted repositories through real clients (client-docker container, dockerd started by
# setup.sh; hosted steps in hosted.sh). The conformance client runs conformance.sh instead. Runs the same in
# record, replay and live mode. Everything is pinned to linux/amd64 so the upstream requests (and so the fixtures)
# do not depend on the host's architecture.
set -eu
. /harness/lib.sh

if [ "${HARNESS_CLIENT:-docker}" = conformance ]; then
  exec sh /harness/docker/conformance.sh
fi

registry=${WAYSTATION_URL#http://}
platform=linux/amd64
hub_image=hello-world:latest
ghcr_image=containerd/busybox:1.36
skopeo_opts='--override-os linux --override-arch amd64'

pulled_digest() {  # repo digest of a pulled image (the manifest list / index digest)
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$1" | head -1 | sed 's/.*@//'
}

step 'mirror mode: the daemon asks the default repository for library/hello-world'
docker pull -q --platform "$platform" "$hub_image" || fail 'docker pull hello-world via registry-mirrors'
mirror_digest=$(pulled_digest "$hub_image")
case "$mirror_digest" in sha256:*) ;; *) fail "no repo digest after the mirror pull: $mirror_digest" ;; esac
if [ "$(uname -m)" = x86_64 ]; then
  docker run --rm "$hub_image" | grep -q 'Hello from Docker!' || fail 'hello-world did not run'
fi

step 'prefix: <registry>/docker-hub/hello-world (single-segment name gets library/)'
docker pull -q --platform "$platform" "$registry/docker-hub/$hub_image" || fail 'docker pull by repository prefix'
[ "$(pulled_digest "$registry/docker-hub/$hub_image")" = "$mirror_digest" ] || fail 'prefix pull: different digest'

step 'alias: <registry>/docker.io/library/hello-world'
docker pull -q --platform "$platform" "$registry/docker.io/library/$hub_image" || fail 'docker pull by docker.io alias'
[ "$(pulled_digest "$registry/docker.io/library/$hub_image")" = "$mirror_digest" ] || fail 'alias pull: different digest'

step 'generic OCI upstream (GHCR, WWW-Authenticate challenge): alias ghcr.io and prefix ghcr'
docker pull -q --platform "$platform" "$registry/ghcr.io/$ghcr_image" || fail 'docker pull by ghcr.io alias'
ghcr_digest=$(pulled_digest "$registry/ghcr.io/$ghcr_image")
docker pull -q --platform "$platform" "$registry/ghcr/$ghcr_image" || fail 'docker pull by ghcr prefix'
[ "$(pulled_digest "$registry/ghcr/$ghcr_image")" = "$ghcr_digest" ] || fail 'ghcr prefix pull: different digest'
if [ "$(uname -m)" = x86_64 ]; then
  [ "$(docker run --rm "$registry/ghcr/$ghcr_image" echo pulled-through)" = pulled-through ] || fail 'busybox run'
fi

step 'registry headers: Docker-Content-Digest, Content-Type and API version on a cached tag'
token=$(curl -fsS "$WAYSTATION_URL/auth/token?service=$registry&scope=repository:docker-hub/library/hello-world:pull" \
  | sed -n 's/.*"token": *"\([^"]*\)".*/\1/p')
[ -n "$token" ] || fail 'no anonymous pull token from the token service'
headers=$(curl -fsS -I -H "Authorization: Bearer $token" \
  "$WAYSTATION_URL/v2/docker-hub/library/hello-world/manifests/latest" | tr -d '\r' | tr '[:upper:]' '[:lower:]')
echo "$headers" | grep -q "^docker-content-digest: $mirror_digest\$" || fail "digest header: $headers"
echo "$headers" | grep -q '^docker-distribution-api-version: registry/2.0$' || fail "API version header: $headers"
echo "$headers" | grep -Eq '^content-type: application/vnd\.(oci\.image\.index\.v1|docker\.distribution\.manifest\.list\.v2)\+json$' \
  || fail "content type: $headers"

step 'skopeo inspect (raw index and the amd64 image) through the prefix'
raw=$(skopeo inspect --tls-verify=false --raw "docker://$registry/docker-hub/library/$hub_image") || fail 'skopeo inspect --raw'
echo "$raw" | grep -q '"manifests"' || fail "expected an image index: $raw"
# shellcheck disable=SC2086
inspect=$(skopeo inspect --tls-verify=false $skopeo_opts "docker://$registry/docker-hub/library/$hub_image") \
  || fail 'skopeo inspect'
echo "$inspect" | grep -q '"Architecture": "amd64"' || fail "skopeo inspect: $inspect"
echo "$inspect" | grep -q "\"Digest\": \"$mirror_digest\"" || fail "skopeo inspect digest: $inspect"

step 'skopeo list-tags (tags/list pass-through)'
skopeo list-tags --tls-verify=false "docker://$registry/docker-hub/library/hello-world" | grep -q '"latest"' \
  || fail 'skopeo list-tags'

step 'skopeo copy from the GHCR alias to an OCI layout (every blob digest verified by skopeo)'
rm -rf /tmp/busybox-oci
# shellcheck disable=SC2086
skopeo copy -q --src-tls-verify=false $skopeo_opts "docker://$registry/ghcr.io/$ghcr_image" oci:/tmp/busybox-oci:copy \
  || fail 'skopeo copy'
[ -s /tmp/busybox-oci/index.json ] || fail 'skopeo copy wrote no OCI layout'

step 'unknown image: MANIFEST_UNKNOWN or NAME_UNKNOWN, no crash'
if skopeo inspect --tls-verify=false --raw "docker://$registry/docker-hub/library/hello-world:no-such-tag-harness" \
  > /tmp/missing.out 2>&1; then
  fail 'a missing tag was served'
fi
grep -Eqi 'manifest unknown|name unknown|not found' /tmp/missing.out || fail "missing tag: $(cat /tmp/missing.out)"

step 'Docker Hub ratelimit-* headers are recorded for upstream health'
ws_login
ws_api GET /stats/upstreams | grep -Eq '"rate_limit": *\{ *"limit"' || fail 'no Docker Hub rate limit in upstream health'

step 'browse API: images, tags and the manifest of what was pulled'
ws_api GET '/repositories/docker-hub/docker/images' | grep -q '"name":"library/hello-world"' || fail 'docker images'
ws_api GET '/repositories/docker-hub/docker/tags?image=library/hello-world' | grep -q '"tag":"latest"' \
  || fail 'docker tags'
ws_api GET '/repositories/docker-hub/docker/manifest?image=library/hello-world&reference=latest' \
  | grep -q '"is_index":true' || fail 'docker manifest'

step 'docker login with an API token (token as the password), pull, narrowed token denied, revoked token refused'
ws_token '["read"]' '["docker-*"]'
token=$WS_TOKEN token_id=$WS_TOKEN_ID
printf '%s' "$token" | docker login "$registry" --username ci-bot --password-stdin || fail 'docker login with a token'
docker rmi -f "$registry/docker-hub/$hub_image" >/dev/null
docker pull -q --platform "$platform" "$registry/docker-hub/$hub_image" || fail 'docker pull after login with a token'
ws_token '["read"]' '["npm-*"]'
printf '%s' "$WS_TOKEN" | docker login "$registry" --username ci-bot --password-stdin || fail 'docker login (narrowed)'
docker rmi -f "$registry/docker-hub/$hub_image" >/dev/null
if docker pull -q --platform "$platform" "$registry/docker-hub/$hub_image" > /tmp/narrow.out 2>&1; then
  fail 'a token limited to npm-* pulled from docker-hub'
fi
grep -Eqi 'denied|unauthorized|not found|unknown' /tmp/narrow.out || fail "narrowed token: $(cat /tmp/narrow.out)"
ws_revoke "$token_id"
if printf '%s' "$token" | docker login "$registry" --username ci-bot --password-stdin > /tmp/revoked.out 2>&1; then
  fail 'docker login worked with a revoked token'
fi
docker logout "$registry" >/dev/null 2>&1 || true

# Hosted repositories: push, multi-platform, immutability, mounts, crane/skopeo/oras/cosign, referrers, deletes.
. /harness/docker/hosted.sh

pass
