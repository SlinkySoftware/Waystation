#!/bin/sh
# podman through Waystation (client-podman, after client-docker against the same stack): Docker Hub in mirror mode
# (registries.conf, written by setup.sh; podman sends no ?ns=, so mirror requests go to the default repository),
# then the repository prefix and the upstream alias for Docker Hub and GHCR. The client is cut off from the upstreams
# (setup.sh), so podman's fallback to docker.io cannot hide a broken mirror. linux/amd64 only, like the docker client.
set -eu
. /harness/lib.sh

registry=${WAYSTATION_URL#http://}
pull() {  # pull <ref>: prints the digest of the manifest podman pulled (linux/amd64 of a multi-platform tag)
  podman pull -q --arch amd64 --os linux "$1" > /tmp/podman.out 2>&1 || { cat /tmp/podman.out; fail "podman pull $1"; }
  podman image inspect --format '{{.Digest}}' "$1"
}

step 'podman mirror mode: docker.io/library/hello-world through the registries.conf mirror'
hub=$(pull docker.io/library/hello-world:latest)
case "$hub" in sha256:*) ;; *) fail "podman: no digest for hello-world: $hub" ;; esac

step 'podman: repository prefix and docker.io alias give the same image'
[ "$(pull "$registry/docker-hub/library/hello-world:latest")" = "$hub" ] || fail 'podman prefix pull: different digest'
[ "$(pull "$registry/docker.io/library/hello-world:latest")" = "$hub" ] || fail 'podman alias pull: different digest'

step 'podman: GHCR by alias (ghcr.io) and by prefix (ghcr)'
ghcr=$(pull "$registry/ghcr.io/containerd/busybox:1.36")
[ "$(pull "$registry/ghcr/containerd/busybox:1.36")" = "$ghcr" ] || fail 'podman GHCR prefix pull: different digest'

step 'podman: what it pulled is the linux/amd64 manifest of the index the registry serves for the tag'
token=$(curl -fsS "$WAYSTATION_URL/auth/token?service=$registry&scope=repository:docker-hub/library/hello-world:pull" \
  | sed -n 's/.*"token": *"\([^"]*\)".*/\1/p')
index=$(curl -fsS -H "Authorization: Bearer $token" "$WAYSTATION_URL/v2/docker-hub/library/hello-world/manifests/latest") \
  || fail 'no index for hello-world:latest'
printf '%s' "$index" | grep -q "\"$hub\"" || fail "podman pulled $hub, not listed in the served index: $index"

pass
