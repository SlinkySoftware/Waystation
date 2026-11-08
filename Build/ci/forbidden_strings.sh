#!/usr/bin/env bash
# Repository-wide forbidden-strings check (CLAUDE.md "No environment details in this repo"; make forbidden-strings,
# part of make check and CI). Fails on host names not in Build/ci/allowed_hosts.txt (plus a format's own list for
# tests/clients/<format>/ and tests/fixtures/upstreams/<format>/), internal-only suffixes, IPs outside RFC 5737/3849
# and loopback, private keys and token shapes. Same scanner as `make clients-check`
# (tests/clients/harness/check_fixtures.py), run in the dev image (no host Python).
#
# Scans every tracked and untracked-but-not-ignored file (git ls-files), so a file is checked before it is committed.
# Usage:
#   Build/ci/forbidden_strings.sh            # the whole repository
#   Build/ci/forbidden_strings.sh PATH...    # only these files/directories
# Optional local deny list (never committed), e.g. your real domains: WAYSTATION_FORBIDDEN_FILE=/path/to/strings.txt
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
deny=()
if [ -n "${WAYSTATION_FORBIDDEN_FILE:-}" ]; then
  deny=(-e WAYSTATION_FORBIDDEN_FILE=/run/forbidden.txt -v "$WAYSTATION_FORBIDDEN_FILE:/run/forbidden.txt:ro")
fi
run() {
  docker run --rm -i --user "$(id -u):$(id -g)" -v "$ROOT:/src:ro" -w /src "${deny[@]}" \
    "${DEV_IMAGE:-waystation-dev:local}" python tests/clients/harness/check_fixtures.py "$@"
}
if [ $# -gt 0 ]; then
  args=()
  for a in "$@"; do args+=("${a#"$ROOT"/}"); done
  run "${args[@]}"
else
  git ls-files -z --cached --others --exclude-standard | run --files-from -
fi
