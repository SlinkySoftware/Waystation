#!/bin/sh
# conda channels through Waystation (client-micromamba container). micromamba creates an environment with tzdata
# (a tiny noarch package) from a proxy of conda-forge (repodata streamed and indexed, the package verified against
# its sha256); builds two packages (a .conda and a .tar.bz2), uploads them to a hosted channel with an API token,
# waits for the worker's repodata and installs them with the token in the channel URL; an anonymous install from the
# hosted channel is refused. micromamba never talks to the upstream: the fake upstream fails on anything unexpected.
set -eu
. /harness/lib.sh

W=$WAYSTATION_URL
work=/tmp/conda-harness
rm -rf "$work"
mkdir -p "$work"
export MAMBA_ROOT_PREFIX="$work/root" CONDA_PKGS_DIRS="$work/pkgs"

mm() {  # mm <label> <micromamba args...>: run micromamba, show its log on failure
  _label=$1
  shift
  micromamba "$@" > "$work/mm.log" 2>&1 || { grep -v -i token "$work/mm.log" | tail -n 30; fail "micromamba: $_label"; }
}

# ---------------------------------------------------------------------------- proxy
step 'micromamba creates an environment through the conda-forge proxy'
mm proxy create -y -p "$work/env-proxy" --override-channels -c "$W/repository/conda-forge" 'tzdata=2025b'
ls "$work"/env-proxy/conda-meta/tzdata-2025b-*.json >/dev/null 2>&1 || fail 'tzdata is not installed'
[ -d "$work/env-proxy/share/zoneinfo" ] || fail 'tzdata files are missing'

step 'the cached package is verified against the indexed repodata and browsable'
# micromamba may fetch the package before the background scan of the repodata has listed it: it is then served
# unverified and confirmed by the listing moments later.
tries=0
while :; do
  files=$(curl -fsS "$W/api/v1/repositories/conda-forge/conda/files?package=tzdata&subdir=noarch") || files=''
  if printf '%s' "$files" | grep -q '"cached":true' && ! printf '%s' "$files" | grep -q '"verified":false'; then
    break
  fi
  tries=$((tries + 1))
  [ "$tries" -lt 180 ] || fail "the cached tzdata file was not verified: $files"
  sleep 1
done
printf '%s' "$files" | grep -q '"version":"2025b"' || fail "tzdata 2025b is not listed: $files"
code=$(curl -s -o /dev/null -w '%{http_code}' "$W/repository/conda-forge/noarch/repodata.jlap")
[ "$code" = 404 ] || fail "repodata.jlap answered $code"

# ---------------------------------------------------------------------------- hosted
ws_login
ws_token '["read", "write", "delete"]'
writer=$WS_TOKEN
ws_token '["read"]'
reader=$WS_TOKEN

build() {  # build <name> <version> <format: conda|tar.bz2>: a noarch package with one file, in $work/out
  _dir="$work/build/$1-$2"
  rm -rf "$_dir"
  mkdir -p "$_dir/info" "$_dir/share/ws-harness" "$work/out"
  printf '%s %s\n' "$1" "$2" > "$_dir/share/ws-harness/$1.txt"
  printf '{"name": "%s", "version": "%s", "build": "0", "build_number": 0, "subdir": "noarch", "noarch": "generic", "depends": [], "license": "MIT", "timestamp": 1700000000000}\n' \
    "$1" "$2" > "$_dir/info/index.json"
  printf 'share/ws-harness/%s.txt\n' "$1" > "$_dir/info/files"
  _file="$_dir/share/ws-harness/$1.txt"
  printf '{"paths": [{"_path": "share/ws-harness/%s.txt", "path_type": "hardlink", "sha256": "%s", "size_in_bytes": %s}], "paths_version": 1}\n' \
    "$1" "$(sha256sum "$_file" | cut -d' ' -f1)" "$(wc -c < "$_file" | tr -d ' ')" > "$_dir/info/paths.json"
  _stem="$1-$2-0"
  if [ "$3" = conda ]; then
    (cd "$_dir" && tar -cf "$work/info.tar" info && tar -cf "$work/pkg.tar" share)
    zstd -q -f "$work/info.tar" -o "$work/build/info-$_stem.tar.zst"
    zstd -q -f "$work/pkg.tar" -o "$work/build/pkg-$_stem.tar.zst"
    printf '{"conda_pkg_format_version": 2}' > "$work/build/metadata.json"
    rm -f "$work/out/$_stem.conda"
    (cd "$work/build" && zip -q -0 "$work/out/$_stem.conda" metadata.json "pkg-$_stem.tar.zst" "info-$_stem.tar.zst")
  else
    (cd "$_dir" && tar -cjf "$work/out/$_stem.tar.bz2" info share)
  fi
}

step 'hosted: upload a .conda and a .tar.bz2 package with an API token'
build ws-harness-hello 1.0 conda
build ws-harness-legacy 0.9 tar.bz2
for f in ws-harness-hello-1.0-0.conda ws-harness-legacy-0.9-0.tar.bz2; do
  code=$(curl -s -o "$work/put.out" -w '%{http_code}' -u "admin:$writer" --upload-file "$work/out/$f" \
    "$W/repository/conda-hosted/noarch/")
  [ "$code" = 201 ] || fail "upload $f answered $code: $(cat "$work/put.out")"
done
code=$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$reader" --upload-file "$work/out/ws-harness-hello-1.0-0.conda" \
  "$W/repository/conda-hosted/noarch/")
[ "$code" = 403 ] || fail "an upload with a read-only token answered $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$writer" --upload-file "$work/out/ws-harness-hello-1.0-0.conda" \
  "$W/repository/conda-hosted/linux-64/")
[ "$code" = 400 ] || fail "an upload to the wrong subdir answered $code"

step 'hosted: the worker publishes the repodata'
tries=0
until curl -fsS -u "admin:$reader" "$W/repository/conda-hosted/noarch/repodata.json" 2>/dev/null \
  | grep -q 'ws-harness-legacy-0.9-0.tar.bz2'; do
  tries=$((tries + 1))
  [ "$tries" -lt 60 ] || fail 'the hosted repodata does not list the uploads'
  sleep 1
done
curl -fsS -u "admin:$reader" "$W/repository/conda-hosted/noarch/repodata.json" | grep -q 'ws-harness-hello-1.0-0.conda' \
  || fail 'the .conda upload is not listed'

step 'hosted: micromamba installs both packages with a read-only token in the channel URL'
authed=$(printf '%s' "$W" | sed "s#://#://admin:$reader@#")
mm hosted create -y -p "$work/env-hosted" --override-channels -c "$authed/repository/conda-hosted" \
  'ws-harness-hello=1.0' 'ws-harness-legacy=0.9'
[ "$(cat "$work/env-hosted/share/ws-harness/ws-harness-hello.txt")" = 'ws-harness-hello 1.0' ] \
  || fail 'ws-harness-hello is not installed'
[ "$(cat "$work/env-hosted/share/ws-harness/ws-harness-legacy.txt")" = 'ws-harness-legacy 0.9' ] \
  || fail 'ws-harness-legacy is not installed'

step 'hosted: without credentials the channel is refused'
rm -rf "$work/pkgs"
micromamba create -y -p "$work/env-anon" --override-channels -c "$W/repository/conda-hosted" 'ws-harness-hello' \
  > "$work/mm.log" 2>&1 && fail 'an anonymous install succeeded'

step 'hosted: delete a package; the repodata drops it'
code=$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$writer" -X DELETE \
  "$W/repository/conda-hosted/noarch/ws-harness-legacy-0.9-0.tar.bz2")
[ "$code" = 204 ] || fail "delete answered $code"
tries=0
while curl -fsS -u "admin:$reader" "$W/repository/conda-hosted/noarch/repodata.json" | grep -q 'ws-harness-legacy'; do
  tries=$((tries + 1))
  [ "$tries" -lt 60 ] || fail 'the hosted repodata still lists the deleted package'
  sleep 1
done
pass
