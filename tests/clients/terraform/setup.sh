#!/bin/sh
# Create the repositories the test uses (idempotent): the provider network mirror and module registry proxy tf-mirror
# (registry.terraform.io; origins registry.terraform.io and registry.opentofu.org, the default; signature
# verification on, the default), the module registry proxy tf-tofu (registry.opentofu.org) and the instance's hosted
# Terraform repository tf-hosted (T-P5-TFM).
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
if ! ws_api GET /repositories/tf-tofu >/dev/null 2>&1; then
  step 'create the module registry proxy tf-tofu (registry.opentofu.org)'
  ws_api POST /repositories '{"name": "tf-tofu", "format": "terraform", "type": "proxy", "online": true,
    "description": "OpenTofu registry modules (client harness)",
    "proxy": {"upstream_url": "https://registry.opentofu.org/"}}' >/dev/null \
    || fail 'could not create the repository tf-tofu'
fi
if ! ws_api GET /repositories/tf-hosted >/dev/null 2>&1; then
  step 'create the hosted Terraform repository tf-hosted'
  ws_api POST /repositories '{"name": "tf-hosted", "format": "terraform", "type": "hosted", "online": true,
    "description": "Private modules and providers (client harness)"}' >/dev/null \
    || fail 'could not create the repository tf-hosted'
fi
