#!/bin/sh
# Terraform and OpenTofu through a Waystation provider network mirror (client-terraform container). Both clients
# accept only https:// mirrors, so they reach the plain-HTTP harness stack through a TLS front (go/tlsfront.go, built
# into the image). terraform installs registry.terraform.io/hashicorp/null and tofu registry.opentofu.org/
# hashicorp/null (3.1.0) and apply a configuration that uses it; then the same from the cache with an API token in a
# credentials block, and a token limited to other repositories is refused. The clients never reach a registry: the
# fake upstream fails on any request it does not expect.
set -eu
. /harness/lib.sh

W=$WAYSTATION_URL
work=/tmp/tf-harness
rm -rf "$work"
mkdir -p "$work"
export CHECKPOINT_DISABLE=1 TF_IN_AUTOMATION=1 TF_INPUT=0
VERSION=3.1.0
repo="$W/repository/tf-mirror"

step 'start a TLS front (terraform and tofu accept only https:// network mirrors)'
tlsfront 127.0.0.1:443 "$W" "$work/front.crt" &
front_pid=$!
trap 'kill $front_pid 2>/dev/null || true' EXIT
i=0
until [ -s "$work/front.crt" ] && curl -fsS --cacert "$work/front.crt" -o /dev/null https://localhost/healthz; do
  i=$((i + 1)); [ "$i" -lt 30 ] || fail 'tlsfront did not start'; sleep 0.5
done
export SSL_CERT_FILE="$work/front.crt"

cli_config() {  # cli_config <file> [token]: the network mirror (and a credentials block when a token is given)
  cat > "$1" <<EOF
provider_installation {
  network_mirror {
    url = "https://localhost/repository/tf-mirror/"
  }
}
EOF
  if [ -n "${2:-}" ]; then
    printf 'credentials "localhost" {\n  token = "%s"\n}\n' "$2" >> "$1"
  fi
}

project() {  # project <dir> <provider source>: a configuration with one null_resource
  rm -rf "$1"
  mkdir -p "$1"
  cat > "$1/main.tf" <<EOF
terraform {
  required_providers {
    null = {
      source  = "$2"
      version = "$VERSION"
    }
  }
}

resource "null_resource" "harness" {}
EOF
}

run() {  # run <client> <dir> <cli config> <label>: init and apply
  (cd "$2" && TF_CLI_CONFIG_FILE="$3" "$1" init -no-color >"$work/init.log" 2>&1) \
    || { grep -v -i token "$work/init.log" | tail -n 30; fail "$1 init ($4)"; }
  (cd "$2" && TF_CLI_CONFIG_FILE="$3" "$1" apply -no-color -auto-approve >"$work/apply.log" 2>&1) \
    || { tail -n 30 "$work/apply.log"; fail "$1 apply ($4)"; }
}

step 'the mirror serves only the allowed origin registries'
code=$(curl -sS -o /dev/null -w '%{http_code}' "$repo/registry.example.com/acme/thing/index.json")
[ "$code" = 404 ] || fail "a provider of another registry host: $code"

step "index.json and the $VERSION version document (zh: hashes from the signed SHA256SUMS)"
curl -fsS "$repo/registry.terraform.io/hashicorp/null/index.json" | grep -q "\"$VERSION\":{}" \
  || fail "index.json does not list $VERSION"
doc=$(curl -fsS "$repo/registry.terraform.io/hashicorp/null/$VERSION.json") || fail 'version document'
printf '%s' "$doc" | grep -q "\"linux_amd64\":{\"url\":\"$VERSION/linux/amd64/terraform-provider-null_${VERSION}_linux_amd64.zip\"" \
  || fail "version document: no linux_amd64 archive: $doc"
printf '%s' "$doc" | grep -q '"darwin_arm64"' || fail "version document: only one platform: $doc"
printf '%s' "$doc" | grep -q '"zh:[0-9a-f]\{64\}"' || fail "version document: no zh: hash: $doc"

cli_config "$work/anonymous.rc"

step "terraform init + apply through the mirror (registry.terraform.io/hashicorp/null $VERSION)"
project "$work/tf" hashicorp/null
run terraform "$work/tf" "$work/anonymous.rc" anonymous
grep -q 'provider "registry.terraform.io/hashicorp/null"' "$work/tf/.terraform.lock.hcl" \
  || fail 'terraform lock file names no provider'
grep -q '"h1:' "$work/tf/.terraform.lock.hcl" || fail 'terraform lock file has no h1: hash'
# (a mirror's hashes are checked, not signed: terraform records only the h1: of the package it installed)

step 'the version document now carries the h1: hash of the cached archive'
i=0
until curl -fsS "$repo/registry.terraform.io/hashicorp/null/$VERSION.json" | grep -q '"hashes":\["h1:'; do
  i=$((i + 1)); [ "$i" -lt 40 ] || fail 'no h1: hash after caching the archive'; sleep 0.5
done
h1=$(curl -fsS "$repo/registry.terraform.io/hashicorp/null/$VERSION.json" \
  | sed -n 's/.*"linux_amd64":{[^}]*"hashes":\["\(h1:[^"]*\)".*/\1/p')
grep -q "\"$h1\"" "$work/tf/.terraform.lock.hcl" || fail "the mirror's h1: $h1 is not the one terraform computed"

step "tofu init + apply through the mirror (registry.opentofu.org/hashicorp/null $VERSION, GitHub release assets)"
project "$work/tofu" hashicorp/null
run tofu "$work/tofu" "$work/anonymous.rc" anonymous
grep -q 'provider "registry.opentofu.org/hashicorp/null"' "$work/tofu/.terraform.lock.hcl" \
  || fail 'tofu lock file names no provider'

step 'an invalid API token in the credentials block is refused'
cli_config "$work/bad.rc" wst_harness-not-a-token
project "$work/bad" hashicorp/null
if (cd "$work/bad" && TF_CLI_CONFIG_FILE="$work/bad.rc" terraform init -no-color) >/dev/null 2>&1; then
  fail 'terraform init succeeded with an invalid token'
fi

step 'terraform and tofu with an API token in the credentials block (served from the cache)'
ws_login
ws_token '["read"]' '["tf-*"]'
cli_config "$work/token.rc" "$WS_TOKEN"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $WS_TOKEN" \
  "$repo/registry.terraform.io/hashicorp/null/index.json")
[ "$code" = 200 ] || fail "bearer token read: $code"
project "$work/tf-token" hashicorp/null
run terraform "$work/tf-token" "$work/token.rc" token
project "$work/tofu-token" hashicorp/null
run tofu "$work/tofu-token" "$work/token.rc" token

step 'a token limited to other repositories cannot use the mirror'
ws_token '["read"]' '["docker-*"]'
cli_config "$work/narrow.rc" "$WS_TOKEN"
project "$work/narrow" hashicorp/null
if (cd "$work/narrow" && TF_CLI_CONFIG_FILE="$work/narrow.rc" tofu init -no-color) >/dev/null 2>&1; then
  fail 'a token limited to docker-* could install from the mirror'
fi

step 'browse API: cached providers and versions'
i=0
until curl -fsS "$W/api/v1/repositories/tf-mirror/terraform/providers?q=null" \
  | grep -q '"provider":"registry.opentofu.org/hashicorp/null"'; do
  i=$((i + 1)); [ "$i" -lt 40 ] || fail 'browse: the providers are not listed'; sleep 0.5
done
page=$(curl -fsS "$W/api/v1/repositories/tf-mirror/terraform/versions?provider=registry.terraform.io/hashicorp/null") \
  || fail 'browse: versions'
printf '%s' "$page" | grep -q "\"version\":\"$VERSION\"" || fail "browse: $VERSION missing: $page"
printf '%s' "$page" | grep -q '"filename":"terraform-provider-null_3.1.0_linux_amd64.zip"' \
  || fail "browse: no cached archive: $page"

pass
