#!/usr/bin/env bash
# M0 end-to-end check against a RUNNING stack (`make e2e-m0`; Documents/Operations/TRY_IT.md):
#   docker pull nginx, npm install, apt update + install in a Debian 13 container and a raw download, all through
#   Waystation, then checks that the live event stream saw them and that stats, activity and browse show them.
#
# Real clients against real public upstreams (Docker Hub, registry.npmjs.org, deb.debian.org, github.com): this needs
# internet access and is not part of `make check`. It uses the host's Docker daemon; client containers run with
# --network host so they reach the stack at WAYSTATION_URL. Docker treats localhost registries as insecure-allowed,
# so prefix pulls need no daemon configuration.
#
# Env:
#   WAYSTATION_URL                 stack base URL (default http://localhost:8080)
#   WAYSTATION_ADMIN_PASSWORD      admin password, or
#   WAYSTATION_ADMIN_PASSWORD_FILE a file holding it
#   WAYSTATION_ADMIN_USER          default admin
#   KEEP_IMAGES=1                  keep the pulled images in the local Docker image store
# Creates the demo repositories when missing (dockerhub-proxy, ghcr-proxy, debian, debian-security, ubuntu,
# ubuntu-security, npm, raw-releases).
set -euo pipefail

URL=${WAYSTATION_URL:-http://localhost:8080}
URL=${URL%/}
REGISTRY=${URL#http://}
REGISTRY=${REGISTRY#https://}
USER_NAME=${WAYSTATION_ADMIN_USER:-admin}
if [ -z "${WAYSTATION_ADMIN_PASSWORD:-}" ] && [ -n "${WAYSTATION_ADMIN_PASSWORD_FILE:-}" ]; then
  WAYSTATION_ADMIN_PASSWORD=$(cat "$WAYSTATION_ADMIN_PASSWORD_FILE")
fi
: "${WAYSTATION_ADMIN_PASSWORD:?set WAYSTATION_ADMIN_PASSWORD or WAYSTATION_ADMIN_PASSWORD_FILE}"

WORK=$(mktemp -d)
SSE_PID=
PULLED=()
cleanup() {
  [ -n "$SSE_PID" ] && kill "$SSE_PID" 2>/dev/null || true
  if [ "${KEEP_IMAGES:-0}" != 1 ] && [ ${#PULLED[@]} -gt 0 ]; then docker rmi "${PULLED[@]}" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok() { printf '   ok: %s\n' "$*"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

JAR=$WORK/cookies
api() {  # api METHOD PATH [JSON]
  local method=$1 path=$2 csrf
  csrf=$(awk '$6 == "ws_csrf" { print $7 }' "$JAR" 2>/dev/null || true)
  curl -fsS -b "$JAR" -X "$method" -H "X-CSRF-Token: $csrf" -H 'Content-Type: application/json' \
    ${3:+--data "$3"} "$URL/api/v1$path"
}

step "sign in to $URL as $USER_NAME"
curl -fsS -c "$JAR" -o /dev/null -H 'Content-Type: application/json' -X POST "$URL/api/v1/auth/login" \
  --data "$(printf '{"username":"%s","password":"%s"}' "$USER_NAME" "$WAYSTATION_ADMIN_PASSWORD")" \
  || fail 'sign-in failed'
ok 'signed in'

step 'demo repositories (created when missing)'
ensure_repo() {  # name json
  if api GET "/repositories/$1" >/dev/null 2>&1; then ok "$1 exists"; return; fi
  api POST /repositories "$2" >/dev/null || fail "could not create $1"
  ok "$1 created"
}
ensure_repo dockerhub-proxy '{"name":"dockerhub-proxy","format":"docker","type":"proxy","online":true,"description":"Docker Hub","proxy":{"upstream_url":"https://registry-1.docker.io"},"format_options":{"aliases":["docker.io"],"docker_hub":true,"default_repo":true}}'
ensure_repo debian '{"name":"debian","format":"apt","type":"proxy","online":true,"description":"Debian","proxy":{"upstream_url":"https://deb.debian.org/debian"},"format_options":{"distribution":"trixie"}}'
ensure_repo debian-security '{"name":"debian-security","format":"apt","type":"proxy","online":true,"description":"Debian security updates","proxy":{"upstream_url":"https://security.debian.org/debian-security","metadata_max_age_s":600},"format_options":{"distribution":"trixie-security"}}'
ensure_repo npm '{"name":"npm","format":"npm","type":"proxy","online":true,"description":"npmjs.org","proxy":{"upstream_url":"https://registry.npmjs.org"},"format_options":{"audit_passthrough":true}}'
ensure_repo raw-releases '{"name":"raw-releases","format":"raw","type":"proxy","online":true,"description":"GitHub release downloads","proxy":{"upstream_url":"https://github.com"},"format_options":{"content_disposition":"attachment"}}'
# The rest of the demo set (not exercised below; the dashboard and Set me up pages show them).
ensure_repo ghcr-proxy '{"name":"ghcr-proxy","format":"docker","type":"proxy","online":true,"description":"GitHub Container Registry","proxy":{"upstream_url":"https://ghcr.io"},"format_options":{"aliases":["ghcr.io"]}}'
ensure_repo ubuntu '{"name":"ubuntu","format":"apt","type":"proxy","online":true,"description":"Ubuntu","proxy":{"upstream_url":"http://archive.ubuntu.com/ubuntu"},"format_options":{"distribution":"resolute"}}'
ensure_repo ubuntu-security '{"name":"ubuntu-security","format":"apt","type":"proxy","online":true,"description":"Ubuntu security updates","proxy":{"upstream_url":"http://security.ubuntu.com/ubuntu","metadata_max_age_s":600},"format_options":{"distribution":"resolute-security"}}'

step 'watch the live event stream while the clients run'
curl -fsSN -b "$JAR" "$URL/api/v1/events/stream?topics=transfers,activity" > "$WORK/sse.log" 2>/dev/null &
SSE_PID=$!
sleep 1

pull() {
  docker rmi "$1" >/dev/null 2>&1 || true  # a fresh pull (layers the daemon already has are not fetched again)
  docker pull -q "$1" >/dev/null || fail "docker pull $1"
  PULLED+=("$1")
  ok "docker pull $1"
}

step 'docker: pull nginx (and the client images used below) through dockerhub-proxy'
pull "$REGISTRY/dockerhub-proxy/library/nginx:latest"
pull "$REGISTRY/dockerhub-proxy/library/debian:trixie"
pull "$REGISTRY/dockerhub-proxy/library/node:24-alpine"
docker run --rm "$REGISTRY/dockerhub-proxy/library/nginx:latest" nginx -v 2>&1 | grep -q 'nginx version' \
  || fail 'nginx image does not run'
ok 'nginx runs'
# The registry API directly (as skopeo/crane do), so docker traffic shows even when the daemon had every layer.
token=$(curl -fsS "$URL/auth/token?service=$REGISTRY&scope=repository:dockerhub-proxy/library/nginx:pull" | jq -r .token)
digest=$(curl -fsSI -H "Authorization: Bearer $token" \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
  "$URL/v2/dockerhub-proxy/library/nginx/manifests/latest" | tr -d '\r' | awk -F': ' 'tolower($1) == "docker-content-digest" { print $2 }')
[ -n "$digest" ] || fail 'no Docker-Content-Digest for nginx:latest'
curl -fsS -o /dev/null -H "Authorization: Bearer $token" "$URL/v2/dockerhub-proxy/library/nginx/manifests/$digest" \
  || fail 'manifest by digest'
ok "registry API: nginx:latest is $digest"

step 'apt: update + install in a Debian 13 container through debian and debian-security'
docker run --rm --network host -e "WS=$URL" "$REGISTRY/dockerhub-proxy/library/debian:trixie" sh -ec '
  rm -f /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list
  cat > /etc/apt/sources.list.d/waystation.sources <<EOF
Types: deb
URIs: $WS/repository/debian/
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: $WS/repository/debian-security/
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends curl jq
  curl --version | head -1; jq --version' > "$WORK/apt.log" 2>&1 || { tail -30 "$WORK/apt.log"; fail 'apt'; }
grep -q "$URL/repository/debian" "$WORK/apt.log" || fail 'apt did not use Waystation'
ok "apt update + install curl jq ($(grep -c '^Get:' "$WORK/apt.log") files)"

step 'npm: install packages through the npm repository'
docker run --rm --network host -e "npm_config_registry=$URL/repository/npm/" \
  "$REGISTRY/dockerhub-proxy/library/node:24-alpine" sh -ec '
  mkdir /app && cd /app && npm init -y >/dev/null
  npm install --no-audit --no-fund chalk@5 is-number left-pad
  node -e "import(\"chalk\").then(() => console.log(\"chalk loads\"))"
  grep -c "\"resolved\": \"$npm_config_registry" package-lock.json' > "$WORK/npm.log" 2>&1 \
  || { tail -30 "$WORK/npm.log"; fail 'npm'; }
grep -q 'chalk loads' "$WORK/npm.log" || fail 'chalk does not load'
ok 'npm install chalk is-number left-pad (tarballs resolved through Waystation)'

step 'raw: download GitHub release files through raw-releases'
raw=$URL/repository/raw-releases/jqlang/jq/releases/download/jq-1.8.1
curl -fsS -o "$WORK/sha256sum.txt" "$raw/sha256sum.txt" || fail 'raw sha256sum.txt'
curl -fsS -o "$WORK/jq-linux-amd64" "$raw/jq-linux-amd64" || fail 'raw jq-linux-amd64'
want=$(awk '$2 == "jq-linux-amd64" { print $1 }' "$WORK/sha256sum.txt")
got=$(sha256sum "$WORK/jq-linux-amd64" | cut -d' ' -f1)
[ -n "$want" ] && [ "$want" = "$got" ] || fail 'jq-linux-amd64 checksum mismatch'
curl -fsS -o /dev/null "$raw/jq-linux-amd64" || fail 'raw second download'
ok 'jq-linux-amd64 downloaded twice, checksum verified'

sleep 2
kill "$SSE_PID" 2>/dev/null || true
SSE_PID=

step 'the live event stream saw every format'
for f in docker apt npm raw; do
  n=$(grep -c "\"format\":\"$f\"" "$WORK/sse.log" || true)
  [ "$n" -gt 0 ] || fail "no live events for $f"
  ok "$f: $n live events"
done

step 'stats, activity and browse show the result'
summary=$(api GET '/stats/summary?range=1h')
requests=$(echo "$summary" | jq '.requests')
[ "$requests" -gt 0 ] || fail "no requests in the 1 h summary: $summary"
ok "summary: $requests requests in the last hour, hit ratio $(echo "$summary" | jq '.hit_ratio')"
for repo in dockerhub-proxy debian debian-security npm raw-releases; do
  n=$(api GET "/activity?repo=$repo&limit=100" | jq '.items | length')
  [ "$n" -gt 0 ] || fail "no activity for $repo"
  ok "activity: $n entries for $repo"
done
api GET '/stats/upstreams' | jq -e '.[] | select(.repository == "dockerhub-proxy") | .rate_limit.limit' >/dev/null \
  || fail 'no Docker Hub rate limit recorded'
ok 'upstream health has the Docker Hub rate limit'

for i in 1 2 3 4 5 6 7 8 9 10; do  # browse indexing runs in the background after the fills
  api GET '/repositories/dockerhub-proxy/docker/images' | jq -e '.items[] | select(.name == "library/nginx")' >/dev/null \
    && break
  sleep 1
done
api GET '/repositories/dockerhub-proxy/docker/tags?image=library/nginx' | jq -e '.items[] | select(.tag == "latest")' \
  >/dev/null || fail 'browse: library/nginx:latest not listed'
ok 'browse docker: library/nginx:latest'
api GET '/repositories/npm/npm/packages' | jq -e '.items[] | select(.name == "chalk" and .cached_versions > 0)' \
  >/dev/null || fail 'browse: chalk not listed'
ok 'browse npm: chalk'
api GET '/repositories/debian/apt/suites' | jq -e '.[] | select(.suite == "trixie")' >/dev/null \
  || fail 'browse: trixie suite missing'
api GET '/repositories/debian/apt/packages?suite=trixie&q=jq' | jq -e '.items[] | select(.name == "jq")' >/dev/null \
  || fail 'browse: jq not in the trixie package list'
ok 'browse apt: trixie suite, package jq'
api GET '/repositories/raw-releases/browse?path=jqlang/jq/releases/download/jq-1.8.1' \
  | jq -e '.entries[] | select(.name == "jq-linux-amd64")' >/dev/null || fail 'browse: raw file missing'
ok 'browse raw: jq-linux-amd64'

printf '\n\033[32mM0 end-to-end: all checks passed.\033[0m Open %s/ui/ to see it on the dashboard.\n' "$URL"
