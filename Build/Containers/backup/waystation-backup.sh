#!/usr/bin/env bash
# Waystation backup and restore (Documents/Operations/BACKUP.md, SPECIFICATION §8.5).
#
#   waystation-backup.sh backup  --target DIR [--hosted-only] [--keep N] [--no-hardlinks] [options]
#   waystation-backup.sh restore --from DIR [--force] [--keep-valkey] [options]
#   waystation-backup.sh verify  [--timeout SECONDS] [options]
#   waystation-backup.sh list    --target DIR
#
# Options: --prefix NAME (container name prefix, default waystation: waystation-worker, waystation-mongodb, ...),
#          --image IMAGE (tools image with mongodump/mongorestore/mongosh, default mongo:8),
#          --db NAME (MongoDB database; default: the stack's WAYSTATION_MONGO_DB, else waystation),
#          --user UID:GID (backup only: run as this user instead of root, e.g. 10001:10001 for NAS targets that
#          squash root; it must be able to read the data directory and write the target).
#
# Host requirements: bash and docker. The work runs in a throwaway container of the tools image that mounts the
# stack's data volume (--volumes-from its worker, server or init container) and joins that container's networks, so
# it reaches MongoDB the way the application does. The MongoDB URI (WAYSTATION_MONGO_URI or WAYSTATION_MONGO_URI_FILE)
# and database come from that container's environment; a URI is never put on a command line.
#
# backup: mongodump --archive --gzip of the database FIRST, then the blob files (blobs are immutable and blob GC keeps
#   an unreferenced blob for 24 h, so every blob the dump references is still there when it is copied), keys/ and a
#   manifest.json, into DIR/waystation-<UTC timestamp>/ (written as DIR/.waystation-<ts>.partial and renamed when
#   complete). Blob files already in the previous backup are hard-linked from it (incremental snapshots; falls back
#   to copies where the target filesystem has no hard links). --hosted-only copies only the blobs that hosted
#   repositories' assets reference (cached proxy content is re-fetched from upstreams after a restore). --keep N
#   deletes the oldest complete backups beyond N.
# restore: checks the archive against the manifest, replaces the database with the dump, copies the blob files and
#   keys back (existing files are kept), fixes their ownership and empties Valkey. Server, worker and web must be
#   stopped (`docker compose stop web server worker`); MongoDB must be running. Then start the stack and run verify.
# verify: runs the blob.verify task in the worker and waits for its result (missing blobs of proxied assets are
#   marked for re-fetch, hosted losses are reported).
# shellcheck disable=SC2016  # the mongosh JavaScript is single-quoted on purpose ($match, $group)
set -euo pipefail

SELF=$(readlink -f "${BASH_SOURCE[0]}")
DEFAULT_URI='mongodb://waystation-mongodb:27017/?replicaSet=rs0'

die() { echo "waystation-backup: $*" >&2; exit 1; }
say() { echo "==> $*"; }

usage() {
  sed -n '2,15p' "$SELF" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# ============================================================================================ inside the container
# Environment: WAYSTATION_MONGO_URI / WAYSTATION_MONGO_URI_FILE, WS_DB, WS_DATA (data dir), WS_APP_IMAGE.

mongo_config() {  # a mongodump/mongorestore --config file holding the URI (0600, tmpfs)
  local uri
  uri=$(mongo_uri)
  (umask 077 && printf "uri: '%s'\n" "${uri//\'/\'\'}" > /tmp/mongo.yaml)
}

mongo_uri() {
  if [ -n "${WAYSTATION_MONGO_URI_FILE:-}" ]; then
    [ -r "$WAYSTATION_MONGO_URI_FILE" ] || die "cannot read WAYSTATION_MONGO_URI_FILE ($WAYSTATION_MONGO_URI_FILE)"
    tr -d '\r\n' < "$WAYSTATION_MONGO_URI_FILE"
  else
    printf '%s' "${WAYSTATION_MONGO_URI:-$DEFAULT_URI}"
  fi
}

js() {  # js <script>: run JavaScript in mongosh with `database` connected (URI from the environment, not argv)
  WS_URI=$(mongo_uri) mongosh --quiet --norc --nodb --eval \
    "const database = new Mongo(process.env.WS_URI).getDB(process.env.WS_DB); $1"
}

inside_backup() {
  local mode=$1 keep=$2 hardlinks=$3
  local data=$WS_DATA target=/backup
  [ -d "$data/blobs" ] || die "no blobs/ in the data directory $data (wrong container?)"
  exec 9>>"$target/.lock"
  flock -n 9 || die "another backup into this target is running"
  rm -rf "$target"/.waystation-*.partial
  local started name work prev=''
  started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  name="waystation-$(date -u +%Y%m%dT%H%M%SZ)"
  work="$target/.$name.partial"
  mkdir -p "$work"
  prev=$(latest "$target")

  say "dumping MongoDB database $WS_DB"
  mongo_config
  mongodump --config=/tmp/mongo.yaml --db="$WS_DB" --archive="$work/mongodb.archive.gz" --gzip --quiet

  say "listing blobs ($mode)"
  local list="$work/.blobs" hosted_missing=0
  if [ "$mode" = hosted-only ]; then
    js '
      const hosted = database.repositories.find({type: "hosted"}, {_id: 1}).toArray().map(r => r._id);
      database.assets.aggregate([{$match: {repo_id: {$in: hosted}}}, {$group: {_id: "$blob_sha256"}}])
        .forEach(d => { if (/^[0-9a-f]{64}$/.test(d._id)) print(d._id) });' \
      | awk '{ print "blobs/sha256/" substr($0, 1, 2) "/" substr($0, 3, 2) "/" $0 }' | sort -u > "$list.all"
    : > "$list"
    while IFS= read -r rel; do
      if [ -f "$data/$rel" ]; then echo "$rel" >> "$list"; else hosted_missing=$((hosted_missing + 1)); fi
    done < "$list.all"
    rm -f "$list.all"
    [ "$hosted_missing" = 0 ] || echo "warning: $hosted_missing hosted blobs are missing from the blob store" >&2
  else
    (cd "$data" && find blobs/sha256 -type f -regextype posix-extended -regex '.*/[0-9a-f]{64}' | sort) > "$list"
  fi

  local linked=0 copied=0
  if [ -n "$prev" ] && [ "$hardlinks" = 1 ] && ln "$target/$prev/manifest.json" "$work/.probe" 2>/dev/null; then
    rm -f "$work/.probe"
    (cd "$target/$prev" && while IFS= read -r rel; do [ -f "$rel" ] && echo "$rel"; done < "$list") > "$list.link"
    comm -23 "$list" "$list.link" > "$list.copy"
    if [ -s "$list.link" ]; then
      (cd "$target/$prev" && xargs -r -d '\n' cp -l --parents -t "$work" < "$list.link")
    fi
    linked=$(wc -l < "$list.link")
  else
    cp "$list" "$list.copy"
    : > "$list.link"
  fi
  if [ -s "$list.copy" ]; then
    say "copying $(wc -l < "$list.copy") blob files"
    (cd "$data" && xargs -r -d '\n' cp -p --parents -t "$work" < "$list.copy")
  fi
  copied=$(wc -l < "$list.copy")
  mkdir -p "$work/blobs"
  local key_files=0
  if [ -d "$data/keys" ]; then
    cp -a "$data/keys" "$work/keys"
    key_files=$(find "$work/keys" -type f | wc -l)
  fi
  local blob_files blob_bytes archive_bytes archive_sha
  blob_files=$(find "$work/blobs" -type f | wc -l)
  blob_bytes=$(find "$work/blobs" -type f -printf '%s\n' | awk '{ s += $1 } END { printf "%d", s }')
  archive_bytes=$(stat -c %s "$work/mongodb.archive.gz")
  archive_sha=$(sha256sum "$work/mongodb.archive.gz" | cut -d' ' -f1)
  rm -f "$list" "$list.link" "$list.copy"

  say "writing the manifest"
  WS_MANIFEST="$work/manifest.json" WS_NAME=$name WS_MODE=$mode WS_STARTED=$started WS_PREV=$prev \
    WS_LINKED=$linked WS_COPIED=$copied WS_BLOB_FILES=$blob_files WS_BLOB_BYTES=$blob_bytes \
    WS_HOSTED_MISSING=$hosted_missing WS_KEY_FILES=$key_files WS_ARCHIVE_BYTES=$archive_bytes \
    WS_ARCHIVE_SHA=$archive_sha js '
      const e = process.env, n = k => Number(e[k]);
      const collections = {};
      database.getCollectionNames().sort().forEach(c => { collections[c] = database.getCollection(c).estimatedDocumentCount() });
      const manifest = {
        format: 1,
        name: e.WS_NAME,
        mode: e.WS_MODE,
        started_at: e.WS_STARTED,
        finished_at: new Date().toISOString(),
        waystation: {image: e.WS_APP_IMAGE || null},
        mongodb: {
          server_version: database.version(),
          database: e.WS_DB,
          archive: "mongodb.archive.gz",
          archive_bytes: n("WS_ARCHIVE_BYTES"),
          archive_sha256: e.WS_ARCHIVE_SHA,
          migrations: database.migrations.find({}, {_id: 1}).sort({_id: 1}).toArray().map(m => m._id),
          collections: collections,
        },
        blobs: {
          files: n("WS_BLOB_FILES"),
          bytes: n("WS_BLOB_BYTES"),
          linked_from_previous: n("WS_LINKED"),
          copied: n("WS_COPIED"),
          previous: e.WS_PREV || null,
          hosted_missing: n("WS_HOSTED_MISSING"),
        },
        keys: {files: n("WS_KEY_FILES")},
      };
      require("fs").writeFileSync(e.WS_MANIFEST, JSON.stringify(manifest, null, 2) + "\n");'
  # The backup belongs to whoever owns the target directory (not possible on some NAS mounts: then left as is).
  if [ "$(id -u)" = 0 ]; then
    chown -R "$(stat -c %u:%g "$target")" "$work" 2>/dev/null || echo "note: could not change the backup's owner" >&2
  fi
  mv "$work" "$target/$name"
  ln -sfn "$name" "$target/latest" 2>/dev/null || true
  chown -h "$(stat -c %u:%g "$target")" "$target/latest" "$target/.lock" 2>/dev/null || true
  say "backup complete: $name ($blob_files blobs, $blob_bytes bytes; $linked linked, $copied copied; archive $archive_bytes bytes)"

  if [ "$keep" -gt 0 ]; then
    local old
    complete "$target" | head -n "-$keep" | while IFS= read -r old; do
      say "retention: removing $old"
      rm -rf "${target:?}/$old"
    done
  fi
}

complete() {  # complete backups in a target, oldest first
  local d
  for d in "$1"/waystation-*/; do
    [ -f "$d/manifest.json" ] && basename "$d"
  done | sort
}

latest() { complete "$1" | tail -n 1; }

inside_list() {
  local name
  complete /backup | while IFS= read -r name; do
    WS_FILE="/backup/$name/manifest.json" mongosh --quiet --norc --nodb --eval '
      const m = JSON.parse(require("fs").readFileSync(process.env.WS_FILE, "utf8"));
      print([m.name, m.mode, m.finished_at, m.blobs.files + " blobs", m.blobs.bytes + " bytes",
             m.mongodb.archive_bytes + " archive bytes"].join("  "))'
  done
}

inside_restore() {
  local data=$WS_DATA from=/backup
  [ -f "$from/manifest.json" ] || die "no manifest.json in the backup (incomplete backup?)"
  [ -d "$data" ] || die "no data directory $data"
  local src_db sha mode
  read -r src_db sha mode < <(WS_FILE="$from/manifest.json" mongosh --quiet --norc --nodb --eval '
    const m = JSON.parse(require("fs").readFileSync(process.env.WS_FILE, "utf8"));
    print([m.mongodb.database, m.mongodb.archive_sha256, m.mode].join(" "))')
  say "checking the archive"
  [ "$(sha256sum "$from/mongodb.archive.gz" | cut -d' ' -f1)" = "$sha" ] || die "archive sha256 differs from the manifest"

  say "restoring MongoDB database $WS_DB (from $src_db)"
  mongo_config
  js 'const r = database.dropDatabase(); if (!r.ok) throw new Error("dropDatabase failed")' >/dev/null
  mongorestore --config=/tmp/mongo.yaml --archive="$from/mongodb.archive.gz" --gzip --quiet --drop \
    --nsInclude="$src_db.*" --nsFrom="$src_db.*" --nsTo="$WS_DB.*"

  say "restoring blob files ($mode backup)"
  local owner list=/tmp/restore.list
  owner=$(stat -c %u:%g "$data")
  (cd "$from" && find blobs -type f) > "$list"
  if [ -s "$list" ]; then
    (cd "$from" && xargs -r -d '\n' cp -p --update=none --parents -t "$data" < "$list")
  fi
  if [ -d "$from/keys" ]; then
    mkdir -p "$data/keys"
    cp -a --update=none "$from/keys/." "$data/keys/"
  fi
  mkdir -p "$data/blobs" "$data/uploads" "$data/keys"
  find "$data/blobs" "$data/keys" \( ! -user "${owner%:*}" -o ! -group "${owner#*:}" \) -exec chown -h "$owner" {} +
  find "$data/blobs" -type d ! -perm 2750 -exec chmod 2750 {} +
  find "$data/blobs" -type f ! -perm 0640 -exec chmod 0640 {} +
  find "$data/keys" -type d ! -perm 0700 -exec chmod 0700 {} +
  find "$data/keys" -type f ! -perm 0600 -exec chmod 0600 {} +
  say "restored $(wc -l < "$list") blob files"
}

if [ "${1:-}" = __inside ]; then
  shift
  export LC_ALL=C
  umask 027
  case "$1" in
    backup) inside_backup "$2" "$3" "$4" ;;
    restore) inside_restore ;;
    list) inside_list ;;
  esac
  exit 0
fi

# ============================================================================================ on the host
command=${1:-}
[ -n "$command" ] || usage
shift
prefix=waystation image=mongo:8 db='' target='' from='' mode=full keep=0 hardlinks=1 force=0 keep_valkey=0
timeout=3600 user=''
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) prefix=$2; shift ;;
    --image) image=$2; shift ;;
    --db) db=$2; shift ;;
    --target) target=$2; shift ;;
    --from) from=$2; shift ;;
    --hosted-only) mode=hosted-only ;;
    --keep) keep=$2; shift ;;
    --no-hardlinks) hardlinks=0 ;;
    --force) force=1 ;;
    --keep-valkey) keep_valkey=1 ;;
    --timeout) timeout=$2; shift ;;
    --user) user=$2; shift ;;
    -h|--help) usage ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done
[[ "$keep" =~ ^[0-9]+$ ]] || die "--keep needs a number"

exists() { docker container inspect "$1" >/dev/null 2>&1; }
running() { [ "$(docker container inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }

app_container() {  # the container whose data mount, networks and environment the tools container borrows
  local c
  for c in "$prefix-worker" "$prefix-server" "$prefix-init"; do
    if exists "$c"; then echo "$c"; return; fi
  done
  die "no container $prefix-worker, $prefix-server or $prefix-init (create the stack first: docker compose up -d init)"
}

app_env() {  # app_env <container> <NAME>: a variable from the container's configuration (empty when unset)
  docker container inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" | sed -n "s/^$2=//p" | head -n 1
}

run_tools() {  # run_tools <ro|rw> <host dir for /backup> <backup dir mode ro|rw> <args...>
  local access=$1 dir=$2 dir_mode=$3
  shift 3
  local app envfile cid nets first n
  app=$(app_container)
  envfile=$(mktemp)
  chmod 600 "$envfile"
  {
    local uri uri_file
    uri=$(app_env "$app" WAYSTATION_MONGO_URI)
    uri_file=$(app_env "$app" WAYSTATION_MONGO_URI_FILE)
    [ -z "$uri" ] || echo "WAYSTATION_MONGO_URI=$uri"
    [ -z "$uri_file" ] || echo "WAYSTATION_MONGO_URI_FILE=$uri_file"
    echo "WS_DB=${db:-$(app_env "$app" WAYSTATION_MONGO_DB)}"
    echo "WS_DATA=$(app_env "$app" WAYSTATION_DATA_DIR)"
    echo "WS_APP_IMAGE=$(docker container inspect -f '{{.Config.Image}}' "$app")"
  } > "$envfile"
  sed -i -e 's/^WS_DB=$/WS_DB=waystation/' -e 's|^WS_DATA=$|WS_DATA=/var/lib/waystation|' "$envfile"
  read -r -a nets <<< "$(docker container inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$app")"
  [ ${#nets[@]} -gt 0 ] || die "$app is not attached to any network"
  first=${nets[0]}
  local as_user=()
  [ -z "$user" ] || as_user=(--user "$user")
  cid=$(docker create "${as_user[@]}" -e HOME=/tmp --env-file "$envfile" --network "$first" --volumes-from "$app:$access" \
    -v "$dir:/backup:$dir_mode" -v "$SELF:/waystation-backup.sh:ro" --tmpfs /tmp --entrypoint /bin/bash \
    --security-opt no-new-privileges:true --label waystation.backup-tool=1 "$image" /waystation-backup.sh __inside "$@")
  rm -f "$envfile"
  # shellcheck disable=SC2064
  trap "docker rm -f $cid >/dev/null 2>&1 || true" EXIT INT TERM
  for n in "${nets[@]:1}"; do docker network connect "$n" "$cid"; done
  docker start -a "$cid" || true
  local rc
  rc=$(docker container inspect -f '{{.State.ExitCode}}' "$cid")
  docker rm -f "$cid" >/dev/null 2>&1 || true
  trap - EXIT INT TERM
  return "$rc"
}

case "$command" in
  backup)
    [ -n "$target" ] || die "backup needs --target DIR"
    mkdir -p "$target"
    target=$(readlink -f "$target")
    run_tools ro "$target" rw backup "$mode" "$keep" "$hardlinks"
    ;;
  list)
    [ -n "$target" ] && [ -d "$target" ] || die "list needs --target DIR"
    docker run --rm -v "$(readlink -f "$target"):/backup:ro" -v "$SELF:/waystation-backup.sh:ro" \
      --entrypoint /bin/bash "$image" /waystation-backup.sh __inside list
    ;;
  restore)
    [ -n "$from" ] && [ -f "$from/manifest.json" ] || die "restore needs --from <backup directory with manifest.json>"
    from=$(readlink -f "$from")
    if [ "$force" != 1 ]; then
      for c in "$prefix-server" "$prefix-worker" "$prefix-web"; do
        ! running "$c" || die "$c is running: stop web, server and worker first (docker compose stop web server worker)"
      done
    fi
    user=""  # restore runs as root: it sets the owner of the restored files
    run_tools rw "$from" ro restore
    if [ "$keep_valkey" != 1 ]; then
      if running "$prefix-valkey"; then
        say "emptying Valkey (live state of the old database: job streams, caches, counters)"
        docker exec "$prefix-valkey" valkey-cli FLUSHALL >/dev/null
      else
        echo "note: $prefix-valkey is not running; empty Valkey before starting the stack (FLUSHALL)" >&2
      fi
    fi
    say "restore complete. Next: docker compose up -d, then: $0 verify --prefix $prefix"
    ;;
  verify)
    running "$prefix-worker" || die "$prefix-worker is not running (start the stack first)"
    docker exec "$prefix-worker" python -m waystation_worker.run_task blob.verify --wait --timeout "$timeout"
    ;;
  *) usage ;;
esac
