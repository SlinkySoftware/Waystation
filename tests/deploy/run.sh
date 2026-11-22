#!/usr/bin/env bash
# Split hosts + TLS end to end (T-F05, SPECIFICATION §8.3/§8.4, Documents/Operations/DEPLOYMENT.md), `make deploy-test`.
# Host: bash + docker. Every "host" is its own compose project on one simulated LAN network:
#   - an external MongoDB (replica set) and Valkey, and a tiny origin for a raw proxy repository (plain containers);
#   - two server hosts from compose.standalone.server.yaml (their `init` runs concurrently);
#   - a worker host from compose.standalone.worker.yaml;
#   - a web host from compose.standalone.web.yaml + compose.tls.yaml with a throwaway certificate, balancing over both
#     server hosts; all hosts share one blob directory (as an NFS export would be shared).
# Checks: the web container refuses half a TLS configuration; HTTP -> HTTPS redirect, HTTP/2, TLS 1.2+; sign-in,
# repositories, a 64 MiB upload and download, a proxied file; a worker job on the shared blob store; with one server
# host stopped everything is still served. KEEP=1 leaves everything in place. IMAGE_TAG=<tag> reuses built images.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DIR="$ROOT/tests/deploy"
CONTAINERS="$ROOT/Build/Containers"
ID="wsdt-$(printf '%s' "$ROOT" | md5sum | cut -c1-8)"
RUN="$DIR/.run"
export DT_ID="$ID" DT_LAN="$ID-lan"
export WAYSTATION_IMAGE_PREFIX=waystation WAYSTATION_IMAGE_TAG="${IMAGE_TAG:-deploytest}"
HOST=registry.example.com
URL="https://$HOST:8443"
OPENSSL_IMAGE=${OPENSSL_IMAGE:-alpine/openssl:latest}
CURL_IMAGE=${CURL_IMAGE:-curlimages/curl:8.17.0}

step() { printf '\n==> [deploy-test] %s\n' "$*"; }
fail() { printf 'FAIL [deploy-test]: %s\n' "$*" >&2; exit 1; }

project() {  # project <name> <compose files, colon-separated> <compose arguments>...: one simulated host
  local name=$1 f
  local -a files
  IFS=: read -ra files <<< "$2"
  shift 2
  local args=(docker compose -p "$ID-$name" --env-file "$RUN/stack.env" --project-directory "$CONTAINERS")
  for f in "${files[@]}"; do args+=(-f "$f"); done
  "${args[@]}" "$@"
}
server_host() { DT_HOST=$1 project "$1" "$CONTAINERS/compose.standalone.server.yaml:$DIR/server.override.yaml" "${@:2}"; }
worker_host() { project worker "$CONTAINERS/compose.standalone.worker.yaml:$DIR/worker.override.yaml" "$@"; }
web_host() {
  project web "$CONTAINERS/compose.standalone.web.yaml:$CONTAINERS/compose.tls.yaml:$DIR/web.override.yaml" "$@"
}

cleanup() {
  local rc=$?
  if [ "${KEEP:-0}" = 1 ]; then
    echo "KEEP=1: left the $ID-* projects and containers, network $DT_LAN and $RUN in place"
    exit "$rc"
  fi
  if [ "$rc" != 0 ]; then
    for c in $(docker ps -aq --filter "name=^$ID-" | grep -vx "$(docker ps -aq --filter "name=^$ID-mongodb")"); do
      echo "---- logs: $(docker inspect -f '{{.Name}}' "$c")"
      docker logs --tail 30 "$c" 2>&1 || true
    done
  fi
  web_host down -v --remove-orphans >/dev/null 2>&1 || true
  worker_host down -v --remove-orphans >/dev/null 2>&1 || true
  server_host server-a down -v --remove-orphans >/dev/null 2>&1 || true
  server_host server-b down -v --remove-orphans >/dev/null 2>&1 || true
  docker rm -f "$ID-mongodb" "$ID-valkey" "$ID-origin" >/dev/null 2>&1 || true
  docker network rm "$DT_LAN" >/dev/null 2>&1 || true
  # The shared blob directory belongs to the Waystation uid after init: remove it from a container.
  [ -d "$RUN" ] && docker run --rm -v "$RUN:/run-dir" busybox:1.37 rm -rf /run-dir/data >/dev/null 2>&1 || true
  rm -rf "$RUN"
  exit "$rc"
}
trap cleanup EXIT

client() {
  docker run --rm --network "$DT_LAN" --user "$(id -u):$(id -g)" -v "$ROOT/tests/clients:/harness:ro" \
    -v "$DIR/client.sh:/deploy-client.sh:ro" -v "$RUN:/run-dir" -e CURL_CA_BUNDLE=/run-dir/tls/tls.crt \
    -e WAYSTATION_URL="$URL" -e HARNESS_ADMIN_PASSWORD="$DT_ADMIN_PASSWORD" "$CURL_IMAGE" sh /deploy-client.sh "$1"
}

wait_healthy() {  # wait_healthy <container>...
  local c
  for c in "$@"; do
    for _ in $(seq 1 90); do
      [ "$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null)" = healthy ] && continue 2
      sleep 2
    done
    fail "$c did not become healthy"
  done
}

if [ -z "${IMAGE_TAG:-}" ]; then
  step "build the images ($WAYSTATION_IMAGE_PREFIX/waystation-<role>:$WAYSTATION_IMAGE_TAG)"
  make -C "$ROOT" --no-print-directory images IMAGE_PREFIX="$WAYSTATION_IMAGE_PREFIX" IMAGE_TAG="$WAYSTATION_IMAGE_TAG"
fi

step 'the web container refuses half a TLS configuration'
out=$(docker run --rm -e WAYSTATION_TLS_CERT=/etc/hostname "$WAYSTATION_IMAGE_PREFIX/waystation-web:$WAYSTATION_IMAGE_TAG" 2>&1) \
  && fail 'web started with a certificate and no key'
echo "$out" | grep -q 'set both WAYSTATION_TLS_CERT and WAYSTATION_TLS_KEY' || fail "unexpected output: $out"
out=$(docker run --rm -e WAYSTATION_SERVER_ADDR='a:1;b' "$WAYSTATION_IMAGE_PREFIX/waystation-web:$WAYSTATION_IMAGE_TAG" 2>&1) \
  && fail 'web started with an invalid server address'
echo "$out" | grep -q 'invalid WAYSTATION_SERVER_ADDR' || fail "unexpected output: $out"

step 'shared blob directory, throwaway certificate, env file'
rm -rf "$RUN"
mkdir -p "$RUN/data" "$RUN/tls" "$RUN/www"
printf 'proxied content from the origin\n' > "$RUN/www/proxied.txt"
chmod 755 "$RUN" "$RUN/www"
chmod 644 "$RUN/www/proxied.txt"
docker run --rm --user "$(id -u):$(id -g)" -v "$RUN/tls:/out" "$OPENSSL_IMAGE" req -x509 -newkey rsa:2048 -nodes -days 2 \
  -subj "/CN=$HOST" -addext "subjectAltName=DNS:$HOST" -keyout /out/tls.key -out /out/tls.crt >/dev/null 2>&1
chmod 0755 "$RUN/tls"
chmod 0644 "$RUN/tls/tls.crt" "$RUN/tls/tls.key"  # the web container's uid reads them (a real host: group + 0640)
DT_ADMIN_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
export DT_ADMIN_PASSWORD
umask 077
{
  echo "WAYSTATION_BASE_URL=$URL"
  echo 'WAYSTATION_LOG_FORMAT=console'
  echo 'WAYSTATION_MONGO_URI=mongodb://mongodb.example.com:27017/?replicaSet=rs0'
  echo 'WAYSTATION_VALKEY_URL=redis://valkey.example.com:6379/0'
  echo "WAYSTATION_SECRET_KEYS=$(head -c 32 /dev/urandom | base64)"
  echo "WAYSTATION_SESSION_SECRET=$(head -c 32 /dev/urandom | base64)"
  echo 'WAYSTATION_DATA_MOUNT_TYPE=bind'
  echo "WAYSTATION_DATA_SOURCE=$RUN/data"
  echo 'WAYSTATION_SERVER_BIND=127.0.0.1'
  echo 'WAYSTATION_SERVER_ADDR=server-a.example.com:8000,server-b.example.com:8000'
  echo "WAYSTATION_TLS_DIR=$RUN/tls"
} > "$RUN/stack.env"
umask 022

step 'external MongoDB (replica set), Valkey and the origin on the LAN'
docker network create "$DT_LAN" >/dev/null
docker run -d --name "$ID-mongodb" --network "$DT_LAN" --network-alias mongodb.example.com mongo:8 \
  mongod --replSet rs0 --bind_ip_all --quiet >/dev/null
docker run -d --name "$ID-valkey" --network "$DT_LAN" --network-alias valkey.example.com valkey/valkey:8-alpine \
  valkey-server --appendonly yes --maxmemory-policy noeviction >/dev/null
docker run -d --name "$ID-origin" --network "$DT_LAN" --network-alias origin.example.com --user 65534:65534 \
  -v "$RUN/www:/www:ro" busybox:1.37 httpd -f -p 8080 -h /www >/dev/null
for _ in $(seq 1 60); do
  docker exec "$ID-mongodb" mongosh --quiet --norc --eval 'db.adminCommand("ping").ok' >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$ID-mongodb" mongosh --quiet --norc --eval \
  'rs.initiate({_id: "rs0", members: [{_id: 0, host: "mongodb.example.com:27017"}]})' >/dev/null
for _ in $(seq 1 60); do
  [ "$(docker exec "$ID-mongodb" mongosh --quiet --norc --eval 'db.hello().isWritablePrimary')" = true ] && break
  sleep 1
done

step 'two server hosts (init on both at once), a worker host, a web host with TLS'
server_host server-a create
server_host server-b create
server_host server-a up -d &
pid_a=$!
server_host server-b up -d
wait "$pid_a" || fail 'server host a did not start'
[ "$(docker inspect -f '{{.State.ExitCode}}' "$ID-server-a-init")" = 0 ] || fail 'init on server host a failed'
[ "$(docker inspect -f '{{.State.ExitCode}}' "$ID-server-b-init")" = 0 ] || fail 'init on server host b failed'
worker_host up -d
web_host up -d
wait_healthy "$ID-server-a-server" "$ID-server-b-server" "$ID-worker" "$ID-web"

client tls
client seed
client fetch

step 'the web tier balanced over both server hosts (its access log names the upstream)'
for h in server-a server-b; do
  ip=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$DT_LAN\").IPAddress}}" "$ID-$h-server")
  docker logs "$ID-web" 2>/dev/null | grep -q "\"upstream\":\"$ip:8000\"" || fail "$h ($ip) received no requests"
done

step 'a worker job on the shared blob store (blob verify finds every file)'
out=$(docker exec "$ID-worker" python -m waystation_worker.run_task blob.verify --wait 2>&1) || fail "blob.verify: $out"
echo "$out" | grep -q '"status": "succeeded"' || fail "blob.verify: $out"
echo "$out" | grep -q '"hosted_lost": 0' || fail "blob.verify reported losses: $out"

step 'server host a stops: the web tier serves everything from server host b'
server_host server-a stop server
client fetch

step 'PASS'
