#!/bin/sh
# Create the raw proxy repository the test uses: the Debian archive root, with the archive's trace files treated as
# mutable metadata (revalidated on every request: metadata_max_age_s 0).
set -eu
. /harness/lib.sh

step 'create raw proxy repository debian-files'
ws_login
ws_api POST /repositories '{
  "name": "debian-files",
  "format": "raw",
  "type": "proxy",
  "online": true,
  "description": "Debian archive top level (client harness)",
  "proxy": {"upstream_url": "https://deb.debian.org/debian/", "metadata_max_age_s": 0},
  "format_options": {"metadata_path_regex": "^/project/trace/", "content_disposition": "attachment"}
}' >/dev/null || fail 'could not create the repository'
