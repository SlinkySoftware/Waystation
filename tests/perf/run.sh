#!/usr/bin/env bash
# Waystation perf suite runner (tests/perf/README.md). Called by `make perf [PROFILE=ci|full] [STRICT=1]`.
#   tests/perf/run.sh [ci|full]
# Host requirements: bash, docker and flock. Everything else runs in containers.
#
# Holds the machine-wide integration-test lock (TEST_LOCK, the one `make test-integration` takes) for the whole
# run, starts a throwaway stack as its own compose project (production images built from this checkout + a synthetic
# nginx upstream), prepares and seeds it, runs each k6 scenario and the live-event probe, compares the results with
# Build/ci/budgets.json and always tears the stack down. Results: tests/perf/.results/ (report.md, measured.json,
# raw k6/probe output, resource samples).
#
# Env: any PERF_* variable of the profile overrides it (e.g. PERF_LARGE_MIB=10240); PERF_SCENARIOS limits the
# scenarios; STRICT=1 fails on every budget over its limit (warn-only until M2 otherwise); KEEP=1 leaves the stack
# running for inspection (tear it down with `docker compose -p <project> down -v`).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PERF="$ROOT/tests/perf"
PROFILE=${1:-ci}
PROFILE_FILE="$PERF/profiles/$PROFILE.env"
[ -f "$PROFILE_FILE" ] || { echo "perf: no profile $PROFILE ($PROFILE_FILE)" >&2; exit 2; }

# The profile, without overriding variables already set in the environment.
while IFS='=' read -r key value; do
  case "$key" in PERF_*) [ -n "${!key:-}" ] || export "$key=$value" ;; esac
done < <(grep -E '^PERF_[A-Z0-9_]+=' "$PROFILE_FILE")

ALL_SCENARIOS="hit_small hit_range hit_large metadata docker miss_small miss_large sse api concurrent"
SCENARIOS=${PERF_SCENARIOS:-$ALL_SCENARIOS}
DEV_IMAGE=${DEV_IMAGE:-waystation-dev:local}
CHECKOUT_ID=$(printf '%s' "$ROOT" | md5sum | cut -c1-8)
export PERF_PROJECT="waystation-perf-$CHECKOUT_ID"
export PERF_DIR="$PERF" PERF_RUN_DIR="$PERF/.run" PERF_RESULTS="$PERF/.results"
export PERF_UID PERF_GID
PERF_UID=$(id -u)
PERF_GID=$(id -g)
export WAYSTATION_IMAGE_PREFIX=waystation WAYSTATION_IMAGE_TAG=perf
NETWORK="${PERF_PROJECT}_frontend"

# ---- one heavy stack per machine: queue on the integration-test lock (same default as the Makefile)
TEST_LOCK=${TEST_LOCK-/tmp/waystation-test-integration.lock}
if [ -n "$TEST_LOCK" ] && command -v flock >/dev/null; then
  exec 9>>"$TEST_LOCK"
  flock -n 9 || { echo "perf: another run holds $TEST_LOCK; waiting"; flock 9; }
fi

rm -rf "$PERF_RUN_DIR" "$PERF_RESULTS"
mkdir -p "$PERF_RUN_DIR/origin" "$PERF_RESULTS"
export PERF_ADMIN_PASSWORD
PERF_ADMIN_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
umask 077
{
  echo 'WAYSTATION_BASE_URL=http://waystation-web:8080'
  echo 'WAYSTATION_LOG_FORMAT=json'
  echo 'WAYSTATION_LOG_LEVEL=WARNING'
  echo "WAYSTATION_SECRET_KEYS=$(head -c 32 /dev/urandom | base64)"
  echo "WAYSTATION_SESSION_SECRET=$(head -c 32 /dev/urandom | base64)"
} > "$PERF_RUN_DIR/stack.env"
umask 022

compose=(docker compose -p "$PERF_PROJECT" --env-file "$PERF_RUN_DIR/stack.env"
  -f "$ROOT/Build/Containers/compose.yaml" -f "$PERF/compose.perf.yaml")
dev_run() {  # dev_run <args...>: a stdlib Python script from the checkout, on the perf stack's network
  docker run --rm -i --network "$NETWORK" --user "$PERF_UID:$PERF_GID" -v "$ROOT:/src" -w /src \
    -e PERF_ADMIN_PASSWORD "$DEV_IMAGE" "$@"
}

SAMPLER=
cleanup() {
  local rc=$?
  [ -z "$SAMPLER" ] || kill "$SAMPLER" 2>/dev/null || true
  if [ "${KEEP:-0}" = 1 ]; then
    echo "KEEP=1: left project $PERF_PROJECT running (docker compose -p $PERF_PROJECT down -v)"
  else
    "${compose[@]}" --profile tools down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$PERF_RUN_DIR"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

started=$(date +%s)
echo "==> perf: profile=$PROFILE project=$PERF_PROJECT scenarios: $SCENARIOS"

echo "==> generate the synthetic upstream content"
docker run --rm --user "$PERF_UID:$PERF_GID" -v "$ROOT:/src" -w /src "$DEV_IMAGE" python tests/perf/origin/generate.py \
  --out tests/perf/.run/origin \
  --small-files "$PERF_SMALL_FILES" --medium-files "$PERF_MEDIUM_FILES" --large-mib "$PERF_LARGE_MIB" \
  --miss-small "$PERF_MISS_SMALL" --miss-large "$PERF_MISS_LARGE" --miss-large-mib "$PERF_MISS_LARGE_MIB" \
  --slow-files "$PERF_SLOW_FILES" --slow-mib "$PERF_SLOW_MIB" --origin-rate "$PERF_ORIGIN_RATE" \
  --packages "$PERF_PACKAGES" --versions "$PERF_VERSIONS" --apt-stanzas "$PERF_APT_STANZAS"

echo "==> build and start the stack (production images, tag perf)"
quiet_build=()
if docker compose up --help 2>/dev/null | grep -q -- '--quiet-build'; then quiet_build=(--quiet-build); fi
"${compose[@]}" up -d --build "${quiet_build[@]}" --wait --wait-timeout 300 --quiet-pull perf-origin web server worker \
  || { "${compose[@]}" logs --no-color --tail 60 >&2; exit 1; }

echo "==> create repositories and warm the cache"
dev_run python tests/perf/prepare.py --catalog tests/perf/.run/origin/catalog.json --out tests/perf/.run/targets.json

if [[ " $SCENARIOS " == *" api "* ]]; then
  echo "==> seed $PERF_BULK_ASSETS assets for the API scenario"
  "${compose[@]}" run --rm --no-deps -v "$PERF:/perf:ro" server python /perf/seed_assets.py \
    --repo perf-bulk --count "$PERF_BULK_ASSETS"
fi

sample() {  # sample <file>: docker stats of the stack every ~2 s until killed
  local names=()
  for s in web server worker mongodb valkey origin; do names+=("$PERF_PROJECT-$s"); done
  while :; do
    docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' "${names[@]}" >> "$1" 2>/dev/null || true
  done
}

k6_env=()
for var in $(compgen -e | grep -E '^PERF_'); do k6_env+=(-e "$var"); done
failed=()
for scenario in $SCENARIOS; do
  echo "==> scenario $scenario"
  sample "$PERF_RESULTS/stats-$scenario.csv" &
  SAMPLER=$!
  if [ "$scenario" = sse ]; then
    dev_run python tests/perf/sse_probe.py --targets tests/perf/.run/targets.json --out tests/perf/.results/sse.json \
      || failed+=("$scenario")
  else
    "${compose[@]}" --profile tools run --rm --no-deps --quiet-pull "${k6_env[@]}" -e SCENARIO="$scenario" k6 \
      run --quiet --no-color /perf/k6/main.js || failed+=("$scenario")
  fi
  kill "$SAMPLER" 2>/dev/null || true
  wait "$SAMPLER" 2>/dev/null || true
  SAMPLER=
done

echo "==> results"
dev_run python tests/perf/collect.py --results tests/perf/.results --project "$PERF_PROJECT" >/dev/null
strict=()
[ "${STRICT:-0}" = 1 ] && strict=(--strict)
set +e
dev_run python Build/ci/budgets.py --markdown "${strict[@]}" > "$PERF_RESULTS/budgets.md" 2>/dev/null
dev_run python Build/ci/budgets.py "${strict[@]}"
budget_rc=$?
set -e
{
  echo "# Waystation perf report"
  echo
  echo "- Profile: \`$PROFILE\` (tests/perf/profiles/$PROFILE.env), $(date -u +%Y-%m-%dT%H:%MZ), commit $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "- Stack: server ${PERF_SERVER_CPUS} CPUs / ${PERF_SERVER_WORKERS} processes, web ${PERF_WEB_CPUS} CPUs, MongoDB ${PERF_MONGO_CPUS} CPUs"
  echo "- Content: ${PERF_LARGE_MIB} MiB large file, ${PERF_MISS_LARGE} x ${PERF_MISS_LARGE_MIB} MiB misses at origin rate ${PERF_ORIGIN_RATE}, ${PERF_BULK_ASSETS} seeded assets (API), ${PERF_CONC_VUS} concurrent clients"
  echo "- Enforcement: $([ "${STRICT:-0}" = 1 ] && echo 'strict (every budget fails)' || echo "budgets.json 'enforce' (warn until M2)")"
  [ ${#failed[@]} -eq 0 ] || echo "- **Failed scenarios: ${failed[*]}**"
  echo "- Duration: $(( $(date +%s) - started )) s"
  echo
  sed 's/^### Performance budgets/## Budgets/' "$PERF_RESULTS/budgets.md"
  echo
  sed 's/^### /## /' "$PERF_RESULTS/details.md"
} > "$PERF_RESULTS/report.md"
echo "perf: report in tests/perf/.results/report.md"

if [ ${#failed[@]} -gt 0 ]; then
  echo "perf: FAILED scenarios: ${failed[*]}" >&2
  exit 1
fi
exit "$budget_rc"
