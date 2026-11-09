#!/bin/sh
# Docker format setup (runs in each client container: docker, then conformance): create the proxy and hosted
# repositories (idempotent), then, in client-docker, start dockerd with the stack as its Docker Hub mirror and as an
# insecure (plain HTTP) registry.
set -eu
. /harness/lib.sh

ensure_repo() {  # ensure_repo <name> <json>: create the repository unless an earlier client already did
  ws_api GET "/repositories/$1" > /dev/null 2>&1 || ws_api POST /repositories "$2" > /dev/null || fail "could not create $1"
}

step 'create docker proxy repositories docker-hub (default, alias docker.io) and ghcr (alias ghcr.io)'
ws_login
ensure_repo docker-hub '{
  "name": "docker-hub",
  "format": "docker",
  "type": "proxy",
  "online": true,
  "description": "Docker Hub (client harness)",
  "proxy": {"upstream_url": "https://registry-1.docker.io"},
  "format_options": {"docker_hub": true, "default_repo": true, "aliases": ["docker.io"]}
}'
ensure_repo ghcr '{
  "name": "ghcr",
  "format": "docker",
  "type": "proxy",
  "online": true,
  "description": "GitHub Container Registry (client harness)",
  "proxy": {"upstream_url": "https://ghcr.io"},
  "format_options": {"aliases": ["ghcr.io"]}
}'

step 'create docker hosted repositories docker-hosted (immutable v<semver> tags) and docker-hosted-copy'
ensure_repo docker-hosted '{
  "name": "docker-hosted",
  "format": "docker",
  "type": "hosted",
  "online": true,
  "description": "Hosted images (client harness)",
  "hosted": {"write_policy": "allow_redeploy", "immutable_tag_patterns": ["v[0-9]+(\\.[0-9]+)*"]}
}'
ensure_repo docker-hosted-copy '{"name": "docker-hosted-copy", "format": "docker", "type": "hosted", "online": true}'

# The conformance client only needs the repositories; the rest prepares the docker client container.
[ "${HARNESS_CLIENT:-docker}" = docker ] || exit 0

step 'cut the client off from the upstreams (a pull can only succeed through Waystation)'
# run.sh points the upstream names at the fake upstream in every container. Here they must not resolve to anything
# useful: dockerd falls back to registry-1.docker.io when a mirror fails, which would hide a broken mirror.
grep -v -e 'docker\.io' -e 'docker\.com' -e 'ghcr\.io' -e 'githubusercontent\.com' /etc/hosts > /tmp/hosts
echo '127.0.0.1 registry-1.docker.io auth.docker.io production.cloudflare.docker.com production.cloudfront.docker.com ghcr.io pkg-containers.githubusercontent.com' \
  >> /tmp/hosts
cat /tmp/hosts > /etc/hosts

step 'start dockerd (registry-mirrors + insecure-registries = the stack)'
registry=${WAYSTATION_URL#http://}
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<JSON
{
  "registry-mirrors": ["$WAYSTATION_URL"],
  "insecure-registries": ["$registry"]
}
JSON
dockerd > /tmp/dockerd.log 2>&1 &
i=0
until docker info > /dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    tail -20 /tmp/dockerd.log >&2
    fail 'dockerd did not start'
  fi
  sleep 1
done
