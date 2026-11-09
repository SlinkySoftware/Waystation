#!/bin/sh
# Create the provider network mirror the test uses: a Terraform proxy repository (origins registry.terraform.io and
# registry.opentofu.org, the default; signature verification on, the default). Idempotent.
set -eu
. /harness/lib.sh

ws_login
if ! ws_api GET /repositories/tf-mirror >/dev/null 2>&1; then
  step 'create the Terraform provider mirror tf-mirror'
  ws_api POST /repositories '{"name": "tf-mirror", "format": "terraform", "type": "proxy", "online": true,
    "description": "Provider network mirror (client harness)",
    "proxy": {"upstream_url": "https://registry.terraform.io/"}}' >/dev/null \
    || fail 'could not create the repository tf-mirror'
fi
