#!/usr/bin/env bash
# Backup/restore end to end (T-O07, Documents/Operations/BACKUP.md), `make backup-test`. Host: bash + docker.
#
# 1. Start a throwaway stack (own compose project and container names), seed it with real clients: a raw hosted
#    repository with two uploads and a raw proxy repository with one cached file from a local origin.
# 2. Full backup twice (the second hard-links every blob from the first), then a --hosted-only backup with --keep 2.
# 3. Wipe the stack (down -v), create an empty one, restore the hosted-only backup, start it, run blob verify:
#    the proxied asset is marked for re-fetch (its blob was not backed up), nothing hosted is lost.
# 4. Clients: hosted files intact, the proxied file is fetched from the origin again and the mark is gone.
# 5. Delete a hosted blob file and verify again: reported as a hosted loss.
# KEEP=1 leaves the stack and tests/backup/.run in place.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DIR="$ROOT/tests/backup"
ID=$(printf '%s' "$ROOT" | md5sum | cut -c1-8)
export BT_PROJECT="waystation-backuptest-$ID" BT_RUN_DIR="$DIR/.run"
export WAYSTATION_IMAGE_PREFIX=waystation WAYSTATION_IMAGE_TAG=backuptest
TOOL="$ROOT/Build/Containers/backup/waystation-backup.sh"
BACKUPS="$BT_RUN_DIR/backups"

step() { printf '\n==> [backup-test] %s\n' "$*"; }
fail() { printf 'FAIL [backup-test]: %s\n' "$*" >&2; exit 1; }

rm -rf "$BT_RUN_DIR"
mkdir -p "$BT_RUN_DIR/www" "$BACKUPS"
chmod 755 "$BT_RUN_DIR/www"
printf 'proxied content from the origin\n' > "$BT_RUN_DIR/www/proxied.txt"
chmod 644 "$BT_RUN_DIR/www/proxied.txt"
BT_ADMIN_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
export BT_ADMIN_PASSWORD
umask 077
{
  echo 'WAYSTATION_BASE_URL=http://waystation-web:8080'
  echo 'WAYSTATION_LOG_FORMAT=console'
  echo "WAYSTATION_SECRET_KEYS=$(head -c 32 /dev/urandom | base64)"
  echo "WAYSTATION_SESSION_SECRET=$(head -c 32 /dev/urandom | base64)"
} > "$BT_RUN_DIR/stack.env"
umask 022

compose=(docker compose -p "$BT_PROJECT" --env-file "$BT_RUN_DIR/stack.env"
  -f "$ROOT/Build/Containers/compose.yaml" -f "$ROOT/Build/Containers/compose.dev.yaml" -f "$DIR/compose.backuptest.yaml")

cleanup() {
  local rc=$?
  if [ "${KEEP:-0}" = 1 ]; then
    echo "KEEP=1: left project $BT_PROJECT and $BT_RUN_DIR in place"
  else
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$BT_RUN_DIR"
  fi
  exit "$rc"
}
trap cleanup EXIT

client() {
  docker run --rm --network "${BT_PROJECT}_frontend" -v "$ROOT/tests/clients:/harness:ro" \
    -v "$DIR/client.sh:/backup-client.sh:ro" -e WAYSTATION_URL=http://waystation-web:8080 \
    -e HARNESS_ADMIN_PASSWORD="$BT_ADMIN_PASSWORD" curlimages/curl:8.17.0 sh /backup-client.sh "$1"
}

mongo_eval() {  # mongo_eval <js>: evaluate in the stack's database, print the result
  docker exec "$BT_PROJECT-mongodb" mongosh --quiet --norc waystation --eval "$1"
}

manifest() {  # manifest <backup name> <expression on m>
  docker run --rm -v "$BACKUPS/$1/manifest.json:/m.json:ro" --entrypoint mongosh mongo:8 --quiet --norc --nodb \
    --eval "const m = JSON.parse(require('fs').readFileSync('/m.json', 'utf8')); print($2)"
}

backups() {  # complete and partial backup directories, oldest first
  find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d -name 'waystation-*' -printf '%f\n' | sort
}

quiet_build=()
if docker compose up --help 2>/dev/null | grep -q -- '--quiet-build'; then quiet_build=(--quiet-build); fi

step "start the stack ($BT_PROJECT)"
"${compose[@]}" up -d --build "${quiet_build[@]}" --wait --wait-timeout 300 --quiet-pull web server worker origin
client seed

step 'full backup, twice (the second links every blob from the first)'
"$TOOL" backup --prefix "$BT_PROJECT" --target "$BACKUPS"
"$TOOL" backup --prefix "$BT_PROJECT" --target "$BACKUPS"
mapfile -t names < <(backups)
[ "${#names[@]}" = 2 ] || fail "expected 2 backups, found ${names[*]}"
[ "$(manifest "${names[0]}" 'm.mode + " " + m.blobs.files')" = 'full 3' ] || fail "first backup: $(manifest "${names[0]}" 'JSON.stringify(m.blobs)')"
[ "$(manifest "${names[1]}" 'm.blobs.linked_from_previous + " " + m.blobs.copied')" = '3 0' ] \
  || fail "second backup was not incremental: $(manifest "${names[1]}" 'JSON.stringify(m.blobs)')"
[ "$(manifest "${names[1]}" 'm.mongodb.migrations.length > 0 && m.mongodb.collections.assets')" = 3 ] \
  || fail "manifest: $(manifest "${names[1]}" 'JSON.stringify(m.mongodb)')"
blob=$(cd "$BACKUPS/${names[1]}" && find blobs -type f | head -n 1)
[ "$(stat -c %h "$BACKUPS/${names[1]}/$blob")" = 2 ] || fail "blob $blob is not hard-linked between backups"
[ "$(sha256sum "$BACKUPS/${names[1]}/mongodb.archive.gz" | cut -d' ' -f1)" = \
  "$(manifest "${names[1]}" 'm.mongodb.archive_sha256')" ] || fail 'archive sha256 differs from the manifest'

step 'hosted-only backup with --keep 2 (the oldest full backup goes)'
"$TOOL" backup --prefix "$BT_PROJECT" --target "$BACKUPS" --hosted-only --keep 2
mapfile -t names < <(backups)
[ "${#names[@]}" = 2 ] || fail "retention: expected 2 backups, found ${names[*]}"
hosted_only=${names[1]}
[ "$(manifest "$hosted_only" 'm.mode + " " + m.blobs.files + " " + m.blobs.hosted_missing')" = 'hosted-only 2 0' ] \
  || fail "hosted-only backup: $(manifest "$hosted_only" 'JSON.stringify(m.blobs)')"
[ "$(readlink "$BACKUPS/latest")" = "$hosted_only" ] || fail 'latest does not point at the newest backup'
"$TOOL" list --target "$BACKUPS"

step 'wipe the stack (down -v) and create an empty one'
"${compose[@]}" down -v --remove-orphans
"${compose[@]}" up -d --wait --quiet-pull mongodb valkey
"${compose[@]}" up -d --no-deps init
[ "$(docker wait "$BT_PROJECT-init")" = 0 ] || fail 'init failed on the empty stack'
"${compose[@]}" create worker
[ "$(mongo_eval 'db.assets.countDocuments()')" = 0 ] || fail 'the new stack is not empty'

step 'restore the hosted-only backup'
"$TOOL" restore --prefix "$BT_PROJECT" --from "$BACKUPS/$hosted_only"
[ "$(mongo_eval 'db.assets.countDocuments()')" = 3 ] || fail 'assets were not restored'
"${compose[@]}" up -d --wait --wait-timeout 300 web server worker origin

step 'blob verify: the proxied asset is marked for re-fetch, nothing hosted is lost'
out=$("$TOOL" verify --prefix "$BT_PROJECT" --timeout 300)
echo "$out"
echo "$out" | grep -q '"status": "succeeded"' || fail 'verify did not succeed'
echo "$out" | grep -q '"proxy_marked": 1' || fail 'the proxied asset was not marked for re-fetch'
echo "$out" | grep -q '"hosted_lost": 0' || fail 'verify reported hosted losses after the restore'
[ "$(mongo_eval 'db.assets.findOne({path: "proxied.txt"}).blob_missing.reason')" = missing ] \
  || fail 'no blob_missing mark on the proxied asset'

client check
# shellcheck disable=SC2016  # $exists is MongoDB syntax
[ "$(mongo_eval 'db.assets.countDocuments({blob_missing: {$exists: true}})')" = 0 ] \
  || fail 'the re-fetch did not clear the mark'

step 'a hosted blob file disappears: verify reports the loss'
sha=$(mongo_eval 'db.assets.findOne({path: "releases/hosted.txt"}).blob_sha256')
docker exec "$BT_PROJECT-worker" rm "/var/lib/waystation/blobs/sha256/${sha:0:2}/${sha:2:2}/$sha"
out=$("$TOOL" verify --prefix "$BT_PROJECT" --timeout 300)
echo "$out" | grep -q '"hosted_lost": 1' || fail "the hosted loss was not reported: $out"
echo "$out" | grep -q '"files-hosted": 1' || fail "lost repository not named: $out"
[ "$(mongo_eval 'db.assets.findOne({path: "releases/hosted.txt"}).blob_missing.hosted')" = true ] \
  || fail 'the lost hosted asset is not marked'

step 'PASS'
