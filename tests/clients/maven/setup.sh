#!/bin/sh
# Create the Maven repositories the test uses (idempotent): a Maven Central proxy, release and SNAPSHOT hosted
# repositories and a group over the three.
set -eu
. /harness/lib.sh

create() {  # create <name> <json body without the name>
  if ws_api GET "/repositories/$1" >/dev/null 2>&1; then
    return 0
  fi
  step "create maven repository $1"
  ws_api POST /repositories "{\"name\": \"$1\", \"format\": \"maven\", \"online\": true, $2}" >/dev/null \
    || fail "could not create the repository $1"
}

ws_login
create maven-central '"type": "proxy", "proxy": {"upstream_url": "https://repo.maven.apache.org/maven2/"},
  "format_options": {"version_policy": "release"}'
create maven-releases '"type": "hosted", "hosted": {"write_policy": "disable_redeploy"},
  "format_options": {"version_policy": "release"}'
create maven-snapshots '"type": "hosted", "format_options": {"version_policy": "snapshot"}'
create maven-public '"type": "group", "group": {"members": ["maven-releases", "maven-snapshots", "maven-central"]}'
