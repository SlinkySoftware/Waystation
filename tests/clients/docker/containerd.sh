#!/bin/sh
# containerd (ctr, the containerd under the client's dockerd) through Waystation: mirror mode from hosts.toml for
# Docker Hub and GHCR (containerd adds ?ns=<registry> to mirror requests: Waystation routes them by upstream alias),
# then the repository prefix and the alias as plain registry names. Sourced by test.sh in client-docker; the client
# is cut off from the upstreams (setup.sh), so the hosts.toml fallback to the real registry cannot hide a broken
# mirror.
set -eu
. /harness/lib.sh

registry=${WAYSTATION_URL#http://}
ctr_() { ctr --address /var/run/docker/containerd/containerd.sock --namespace harness "$@"; }
certs=/etc/containerd/certs.d
for upstream in docker.io:https://registry-1.docker.io ghcr.io:https://ghcr.io; do
  mkdir -p "$certs/${upstream%%:*}"
  cat > "$certs/${upstream%%:*}/hosts.toml" <<TOML
server = "${upstream#*:}"

[host."$WAYSTATION_URL"]
  capabilities = ["pull", "resolve"]
TOML
done
pulled() {  # pulled <image ref>: its digest in containerd's image store
  ctr_ images ls -q | grep -qx "$1" || fail "containerd has no $1"
  ctr_ images ls | awk -v ref="$1" '$1 == ref {print $3}'
}
# --local: the client-side pull (dockerd's containerd has no transfer streaming service); it honours --hosts-dir.
pull() {  # pull <ref> [ctr pull options]
  _ref=$1; shift
  ctr_ images pull --local --platform linux/amd64 "$@" "$_ref" > /tmp/ctr.out 2>&1 || { cat /tmp/ctr.out; fail "ctr pull $_ref"; }
}

step 'containerd mirror mode (hosts.toml): docker.io/library/hello-world and ghcr.io/containerd/busybox'
pull docker.io/library/hello-world:latest --hosts-dir "$certs"
hub=$(pulled docker.io/library/hello-world:latest)
[ "$hub" = "$mirror_digest" ] || fail "containerd mirror pull: $hub, the docker daemon got $mirror_digest"
pull ghcr.io/containerd/busybox:1.36 --hosts-dir "$certs"
[ "$(pulled ghcr.io/containerd/busybox:1.36)" = "$ghcr_digest" ] || fail 'containerd GHCR mirror pull: different digest'

step 'containerd: repository prefix and upstream alias as registry names (plain HTTP)'
pull "$registry/docker-hub/library/hello-world:latest" --plain-http
[ "$(pulled "$registry/docker-hub/library/hello-world:latest")" = "$hub" ] || fail 'containerd prefix pull: digest'
pull "$registry/ghcr.io/containerd/busybox:1.36" --plain-http
[ "$(pulled "$registry/ghcr.io/containerd/busybox:1.36")" = "$ghcr_digest" ] || fail 'containerd alias pull: digest'
