#!/bin/sh
# OCI distribution-spec conformance suite (v1.1.1, built into client-conformance) against the docker-hosted
# repository created by setup.sh: pull, push (monolithic, chunked, mounts), content discovery (tags, referrers) and
# content management (deletes), with an API token of the per-run bootstrap admin limited to docker-hosted.
set -eu
. /harness/lib.sh

step 'OCI distribution-spec conformance: pull, push, content discovery, content management'
# An API token as the password: the suite asks the token service for a token before nearly every request, and
# account passwords are rate limited per client address (token use is not).
ws_login
ws_token '["write","delete"]' '["docker-hosted"]'
report=/tmp/oci-report
mkdir -p "$report"
export OCI_ROOT_URL="$WAYSTATION_URL"
export OCI_NAMESPACE=docker-hosted/conformance
export OCI_CROSSMOUNT_NAMESPACE=docker-hosted/conformance-mount
export OCI_USERNAME=conformance
export OCI_PASSWORD="$WS_TOKEN"
export OCI_TEST_PULL=1 OCI_TEST_PUSH=1 OCI_TEST_CONTENT_DISCOVERY=1 OCI_TEST_CONTENT_MANAGEMENT=1
export OCI_HIDE_SKIPPED_WORKFLOWS=0 OCI_DEBUG=0 OCI_DELETE_MANIFEST_BEFORE_BLOBS=1
export OCI_REPORT_DIR="$report"
cd "$report"
if ! conformance.test > "$report/run.log" 2>&1; then
  grep -E '^\s*\[FAIL\]|FAIL!|Fail|Expected|to equal|to be' "$report/run.log" | head -60 >&2 || true
  tail -40 "$report/run.log" >&2
  fail 'the OCI conformance suite failed'
fi
grep -E 'Ran [0-9]+ of [0-9]+ Specs|Passed|SUCCESS' "$report/run.log" | tail -3

pass
