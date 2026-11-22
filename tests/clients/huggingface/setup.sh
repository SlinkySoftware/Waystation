#!/bin/sh
# Create the Hugging Face repository the test uses: a proxy of the Hub (anonymous reads, as every proxy). Idempotent.
set -eu
. /harness/lib.sh

ws_login
if ! ws_api GET /repositories/huggingface >/dev/null 2>&1; then
  step 'create Hugging Face repository huggingface'
  ws_api POST /repositories '{"name": "huggingface", "format": "huggingface", "type": "proxy", "online": true,
    "description": "Hugging Face Hub proxy (client harness)", "proxy": {"upstream_url": "https://huggingface.co"}}' \
    >/dev/null || fail 'could not create the repository huggingface'
fi
