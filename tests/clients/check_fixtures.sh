#!/usr/bin/env bash
# Forbidden-strings + size check for upstream fixtures and the harness (tests/clients/harness/check_fixtures.py).
# Runs in the dev image (no host Python). Usage:
#   tests/clients/check_fixtures.sh                       # tests/clients + tests/fixtures/upstreams
#   tests/clients/check_fixtures.sh [--fixtures-root R] PATH...
# Optional local deny list (never committed): WAYSTATION_FORBIDDEN_FILE=/path/to/strings.txt
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
args=()
for a in "$@"; do args+=("${a#"$ROOT"/}"); done
deny=()
if [ -n "${WAYSTATION_FORBIDDEN_FILE:-}" ]; then
  deny=(-e WAYSTATION_FORBIDDEN_FILE=/run/forbidden.txt -v "$WAYSTATION_FORBIDDEN_FILE:/run/forbidden.txt:ro")
fi
exec docker run --rm -i --user "$(id -u):$(id -g)" -v "$ROOT:/src" -w /src "${deny[@]}" "${DEV_IMAGE:-waystation-dev:local}" \
  python tests/clients/harness/check_fixtures.py "${args[@]}"
