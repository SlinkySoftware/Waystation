#!/bin/sh
# Docker / OCI hosted repositories through real clients (sourced by test.sh in client-docker after the proxy steps;
# dockerd is running, hello-world is cached in docker-hub). Nothing here reaches an upstream: images are built FROM
# scratch, so record, replay and live behave the same. Repositories come from setup.sh: docker-hosted (tags matching
# v<semver> are immutable) and docker-hosted-copy.

hosted=$registry/docker-hosted
work=/tmp/hosted
rm -rf "$work"
mkdir -p "$work/ctx"

step 'docker login with an API token that may write and delete (the token as the password)'
ws_login
ws_token '["write","delete"]'
push_token=$WS_TOKEN
printf '%s' "$push_token" | docker login "$registry" --username ci-bot --password-stdin || fail 'docker login (write token)'

step 'buildx: build a multi-platform image FROM scratch and push it (index + amd64/arm64 + attestations)'
printf 'hello from waystation\n' > "$work/ctx/hello.txt"
printf 'FROM scratch\nARG TARGETARCH\nCOPY hello.txt /hello-$TARGETARCH.txt\n' > "$work/ctx/Dockerfile"
docker buildx build -q --platform linux/amd64,linux/arm64 -t "$hosted/app:v1.0" -t "$hosted/app:latest" \
  --push "$work/ctx" > /dev/null || fail 'docker buildx build --push (multi-platform)'
index=$(skopeo inspect --tls-verify=false --raw "docker://$hosted/app:v1.0") || fail 'skopeo inspect --raw app:v1.0'
echo "$index" | grep -q '"architecture": *"arm64"' || fail "no arm64 entry in the pushed index: $index"
echo "$index" | grep -q '"architecture": *"amd64"' || fail "no amd64 entry in the pushed index: $index"
app_digest=$(crane digest --insecure "$hosted/app:v1.0") || fail 'crane digest app:v1.0'
case "$app_digest" in sha256:*) ;; *) fail "no digest for app:v1.0: $app_digest" ;; esac

step 'pull the pushed image back per platform (removed locally first)'
docker rmi -f "$hosted/app:v1.0" "$hosted/app:latest" > /dev/null 2>&1 || true
docker pull -q --platform linux/arm64 "$hosted/app:v1.0" > /dev/null || fail 'docker pull --platform linux/arm64'
docker pull -q --platform linux/amd64 "$hosted/app:latest" > /dev/null || fail 'docker pull --platform linux/amd64'

step 'tag immutability: v1.0 cannot move, latest can'
printf 'second build\n' > "$work/ctx/hello.txt"
if docker buildx build -q --platform linux/amd64 -t "$hosted/app:v1.0" --push "$work/ctx" > "$work/immutable.out" 2>&1
then
  fail 'an immutable tag was overwritten'
fi
grep -Eqi 'immutable|denied' "$work/immutable.out" || fail "immutable tag push: $(cat "$work/immutable.out")"
docker buildx build -q --platform linux/amd64 -t "$hosted/app:latest" --push "$work/ctx" > /dev/null \
  || fail 'pushing a new build to latest'
[ "$(crane digest --insecure "$hosted/app:v1.0")" = "$app_digest" ] || fail 'v1.0 moved'
[ "$(crane digest --insecure "$hosted/app:latest")" != "$app_digest" ] || fail 'latest did not move'

step 'tags/list and the catalog'
crane ls --insecure "$hosted/app" > "$work/tags.out" || fail 'crane ls'
grep -qx 'v1.0' "$work/tags.out" && grep -qx 'latest' "$work/tags.out" || fail "tags: $(cat "$work/tags.out")"
crane catalog --insecure "$registry" > "$work/catalog.out" || fail 'crane catalog'
grep -qx 'docker-hosted/app' "$work/catalog.out" || fail "catalog: $(cat "$work/catalog.out")"

step 'crane copy from the docker-hub proxy into the hosted repository (cross-repository mount of cached layers)'
crane copy --insecure --platform linux/amd64 "$registry/docker-hub/library/hello-world:latest" \
  "$hosted/hello:latest" || fail 'crane copy docker-hub -> docker-hosted'
crane manifest --insecure "$hosted/hello:latest" | grep -q '"layers"' || fail 'crane manifest of the copy'

step 'skopeo copy --all between hosted repositories (every platform, attestations included)'
skopeo copy -q --all --src-tls-verify=false --dest-tls-verify=false "docker://$hosted/app:v1.0" \
  "docker://$registry/docker-hosted-copy/app:v1.0" || fail 'skopeo copy --all'
[ "$(crane digest --insecure "$registry/docker-hosted-copy/app:v1.0")" = "$app_digest" ] || fail 'copy: digest differs'

step 'oras: push and pull an artifact, attach an SBOM to the image and discover it (referrers API)'
printf 'release notes\n' > "$work/notes.txt"
(cd "$work" && oras push --plain-http "$hosted/notes:1.0" --artifact-type application/vnd.example.notes \
  notes.txt:text/plain) || fail 'oras push'
mkdir -p "$work/pulled"
oras pull --plain-http -o "$work/pulled" "$hosted/notes:1.0" || fail 'oras pull'
cmp -s "$work/notes.txt" "$work/pulled/notes.txt" || fail 'oras pull: different content'
printf '{"sbom": "example"}\n' > "$work/sbom.json"
(cd "$work" && oras attach --plain-http --artifact-type application/vnd.example.sbom "$hosted/app@$app_digest" \
  sbom.json:application/json) || fail 'oras attach'
oras discover --plain-http --format json "$hosted/app@$app_digest" > "$work/discover.out" || fail 'oras discover'
grep -q 'application/vnd.example.sbom' "$work/discover.out" || fail "oras discover: $(cat "$work/discover.out")"

step 'cosign: sign with a local key (offline: no signing config, no transparency log) and verify the signature'
# cosign 3 stores the signature as a Sigstore bundle attached through the referrers API.
(
  cd "$work" || exit 1
  COSIGN_PASSWORD='' cosign generate-key-pair > /dev/null 2>&1 || fail 'cosign generate-key-pair'
  COSIGN_PASSWORD='' cosign sign -y --key cosign.key --use-signing-config=false --tlog-upload=false \
    --allow-http-registry --allow-insecure-registry "$hosted/app@$app_digest" > sign.out 2>&1 \
    || fail "cosign sign: $(cat sign.out)"
  cosign verify --key cosign.pub --insecure-ignore-tlog --allow-http-registry --allow-insecure-registry \
    "$hosted/app:v1.0" > verify.out 2>&1 || fail "cosign verify: $(cat verify.out)"
  grep -q 'signatures were verified against the specified public key' verify.out || fail "cosign: $(cat verify.out)"
) || exit 1
oras discover --plain-http --format json "$hosted/app@$app_digest" | grep -q 'application/vnd.dev.sigstore.bundle' \
  || fail 'the cosign signature is not listed by the referrers API'

step 'delete: the copied hello image by digest, then it is gone'
hello_digest=$(crane digest --insecure "$hosted/hello:latest") || fail 'crane digest hello'
crane delete --insecure "$hosted/hello@$hello_digest" || fail 'crane delete'
if crane manifest --insecure "$hosted/hello@$hello_digest" > /dev/null 2>&1; then fail 'deleted manifest served'; fi

step 'browse API and audit: the pushed image, its tags, the upload and delete records'
ws_api GET '/repositories/docker-hosted/docker/images' | grep -q '"name":"app"' || fail 'hosted docker images'
ws_api GET '/repositories/docker-hosted/docker/tags?image=app' | grep -q '"tag":"v1.0"' || fail 'hosted docker tags'
ws_api GET '/audit?action=content.upload&limit=200' | grep -q 'docker-hosted/app' \
  || fail 'no content.upload audit record'
ws_api GET '/audit?action=content.delete&limit=200' | grep -q 'docker-hosted/hello' || fail 'no content.delete audit record'

step 'group: a push through docker-group lands in docker-hosted (the writable member)'
group=$registry/docker-group
printf 'pushed through the group\n' > "$work/ctx/hello.txt"
docker buildx build -q --platform linux/amd64 -t "$group/grouped:1.0" --load "$work/ctx" > /dev/null \
  || fail 'docker buildx build --load'
docker push -q "$group/grouped:1.0" > /dev/null || fail 'docker push through the group'
grouped_digest=$(crane digest --insecure "$hosted/grouped:1.0") || fail 'the pushed image is not in docker-hosted'
[ "$(crane digest --insecure "$group/grouped:1.0")" = "$grouped_digest" ] || fail 'group and member digests differ'
docker rmi -f "$group/grouped:1.0" > /dev/null
docker pull -q "$group/grouped:1.0" > /dev/null || fail 'docker pull through the group'

step 'group: members answer in order (docker-hosted-copy first) and tag lists are merged'
crane copy --insecure "$hosted/app:latest" "$registry/docker-hosted-copy/app:order" || fail 'crane copy (copy member)'
crane copy --insecure "$hosted/app:v1.0" "$hosted/app:order" || fail 'crane copy (hosted member)'
[ "$(crane digest --insecure "$group/app:order")" = "$(crane digest --insecure "$hosted/app:latest")" ] \
  || fail 'the group did not answer from its first member'
crane ls --insecure "$group/app" > "$work/group-tags.out" || fail 'crane ls through the group'
for tag in latest order v1.0; do
  grep -qx "$tag" "$work/group-tags.out" || fail "merged tags: $(cat "$work/group-tags.out")"
done

docker logout "$registry" > /dev/null 2>&1 || true
