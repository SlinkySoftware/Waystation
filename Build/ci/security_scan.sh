#!/usr/bin/env bash
# `make security-scan` (T-R01, Documents/Security/DEPENDENCIES.md): known vulnerabilities in what Waystation ships.
#   1. pip-audit over uv.lock (every workspace package's locked dependencies, dev tools included);
#   2. npm audit --omit=dev over Application/Web/package-lock.json (what the SPA bundle contains);
#   3. Trivy over the server, worker and web images (HIGH and CRITICAL, OS packages and language packages).
# Builds the images first unless SKIP_IMAGES=1 (IMAGE_PREFIX/IMAGE_TAG name them, as `make images` does).
# Exit status: non-zero when pip-audit or npm audit report anything, or when Trivy reports a HIGH/CRITICAL
# finding that has a fix (`--ignore-unfixed`: findings without a fixed package are listed in DEPENDENCIES.md).
# Host: bash + docker. Needs network access to the vulnerability databases.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DEV_IMAGE=${DEV_IMAGE:-waystation-dev:local}
TRIVY_IMAGE=${TRIVY_IMAGE:-aquasec/trivy:0.76.2}
IMAGE_PREFIX=${IMAGE_PREFIX:-waystation}
IMAGE_TAG=${IMAGE_TAG:-$(cat "$ROOT/VERSION")}
CACHE=${TRIVY_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/waystation-trivy}
rc=0

step() { printf '\n==> [security-scan] %s\n' "$*"; }

step 'pip-audit (uv.lock)'
docker run --rm -i --user "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv -v "$ROOT:/src:ro" -w /src \
  "$DEV_IMAGE" bash -c 'uv export --frozen --no-hashes --all-packages --no-emit-workspace -o /tmp/req.txt >/dev/null \
    && uvx pip-audit --no-deps --disable-pip --progress-spinner off -r /tmp/req.txt' || rc=1

step 'npm audit --omit=dev (Application/Web)'
docker run --rm -i --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$ROOT/Application/Web:/web:ro" -w /tmp "$DEV_IMAGE" \
  bash -c 'cp /web/package.json /web/package-lock.json . && npm audit --omit=dev' || rc=1

if [ "${SKIP_IMAGES:-0}" != 1 ]; then
  step "build images ($IMAGE_PREFIX/waystation-*:$IMAGE_TAG)"
  make -C "$ROOT" images IMAGE_PREFIX="$IMAGE_PREFIX" IMAGE_TAG="$IMAGE_TAG"
fi
mkdir -p "$CACHE"
for role in server worker web; do
  image="$IMAGE_PREFIX/waystation-$role:$IMAGE_TAG"
  step "trivy $image (HIGH, CRITICAL)"
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$CACHE:/root/.cache" "$TRIVY_IMAGE" image \
    --quiet --scanners vuln --severity HIGH,CRITICAL --format table "$image"
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$CACHE:/root/.cache" "$TRIVY_IMAGE" image \
    --quiet --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 --format table "$image" \
    >/dev/null || { echo "security-scan: $image has fixable HIGH/CRITICAL findings"; rc=1; }
done

[ "$rc" = 0 ] && printf '\nPASS [security-scan]\n' || printf '\nFAIL [security-scan]\n'
exit "$rc"
