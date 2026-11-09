#!/bin/sh
# Create the raw repositories the test uses: a proxy of the Debian archive root (its trace files treated as
# mutable metadata, revalidated on every request), a hosted repository for uploads and a group of both.
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

step 'create raw hosted repository raw-uploads and group raw-all (hosted first, then the proxy)'
ws_api POST /repositories '{
  "name": "raw-uploads",
  "format": "raw",
  "type": "hosted",
  "online": true,
  "description": "Uploads (client harness)",
  "hosted": {"write_policy": "allow_redeploy"},
  "format_options": {"content_disposition": "attachment"}
}' >/dev/null || fail 'could not create the hosted repository'
ws_api POST /repositories '{
  "name": "raw-all",
  "format": "raw",
  "type": "group",
  "online": true,
  "description": "Uploads and the Debian archive (client harness)",
  "group": {"members": ["raw-uploads", "debian-files"], "writable_member": "raw-uploads"}
}' >/dev/null || fail 'could not create the group repository'
