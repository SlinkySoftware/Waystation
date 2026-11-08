#!/bin/sh
# Docker format setup (client-docker container): create the proxy repositories, then start dockerd with the stack as
# its Docker Hub mirror and as an insecure (plain HTTP) registry.
set -eu
. /harness/lib.sh

step 'create docker proxy repositories docker-hub (default, alias docker.io) and ghcr (alias ghcr.io)'
ws_login
ws_api POST /repositories '{
  "name": "docker-hub",
  "format": "docker",
  "type": "proxy",
  "online": true,
  "description": "Docker Hub (client harness)",
  "proxy": {"upstream_url": "https://registry-1.docker.io"},
  "format_options": {"docker_hub": true, "default_repo": true, "aliases": ["docker.io"]}
}' >/dev/null || fail 'could not create docker-hub'
ws_api POST /repositories '{
  "name": "ghcr",
  "format": "docker",
  "type": "proxy",
  "online": true,
  "description": "GitHub Container Registry (client harness)",
  "proxy": {"upstream_url": "https://ghcr.io"},
  "format_options": {"aliases": ["ghcr.io"]}
}' >/dev/null || fail 'could not create ghcr'

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
