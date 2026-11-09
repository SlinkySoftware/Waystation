#!/bin/sh
# OCI distribution-spec conformance suite (v1.1.1, built into client-conformance) against the docker-hosted
# repository created by setup.sh: pull, push (monolithic, chunked, mounts), content discovery (tags, referrers) and
# content management (deletes), with an API token of the per-run bootstrap admin limited to docker-hosted and
# docker-group; then the same suite through the group docker-group (reads from its members, writes to docker-hosted).
set -eu
. /harness/lib.sh

step 'OCI distribution-spec conformance: pull, push, content discovery, content management'
# An API token as the password: the suite asks the token service for a token before nearly every request, and
# account passwords are rate limited per client address (token use is not).
ws_login
ws_token '["write","delete"]' '["docker-hosted","docker-group"]'
export OCI_ROOT_URL="$WAYSTATION_URL"
export OCI_USERNAME=conformance
export OCI_PASSWORD="$WS_TOKEN"
export OCI_TEST_PULL=1 OCI_TEST_PUSH=1 OCI_TEST_CONTENT_DISCOVERY=1 OCI_TEST_CONTENT_MANAGEMENT=1
export OCI_HIDE_SKIPPED_WORKFLOWS=0 OCI_DEBUG=0 OCI_DELETE_MANIFEST_BEFORE_BLOBS=1

conformance() {  # conformance <namespace>: run the whole suite against one repository
  report=/tmp/oci-report-${1%%/*}
  mkdir -p "$report"
  export OCI_NAMESPACE="$1" OCI_CROSSMOUNT_NAMESPACE="$1-mount" OCI_REPORT_DIR="$report"
  if ! (cd "$report" && conformance.test > "$report/run.log" 2>&1); then
    grep -E '^\s*\[FAIL\]|FAIL!|Fail|Expected|to equal|to be' "$report/run.log" | head -60 >&2 || true
    tail -40 "$report/run.log" >&2
    fail "the OCI conformance suite failed against $1"
  fi
  grep -E 'Ran [0-9]+ of [0-9]+ Specs|Passed|SUCCESS' "$report/run.log" | tail -3
}

conformance docker-hosted/conformance

step 'OCI distribution-spec conformance through the group docker-group (pushes land in docker-hosted)'
conformance docker-group/conformance

pass
