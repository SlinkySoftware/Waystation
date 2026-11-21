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
# tf.example.com: module and provider addresses of the hosted repository need a host name with a dot
grep -q ' tf.example.com$' /etc/hosts || echo '127.0.0.1 tf.example.com' >> /etc/hosts
TLSFRONT_NAMES=tf.example.com tlsfront 127.0.0.1:443 "$W" "$work/front.crt" &
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

MODULE_VERSION=1.0.0
module_config() {  # module_config <file>: module lookups for both public registries go to the module proxies
  cat > "$1" <<EOF
host "registry.terraform.io" {
  services = {
    "modules.v1" = "https://localhost/repository/tf-mirror/v1/modules/"
  }
}
host "registry.opentofu.org" {
  services = {
    "modules.v1" = "https://localhost/repository/tf-tofu/v1/modules/"
  }
}
EOF
}

module_project() {  # module_project <dir>: a configuration using hashicorp/subnets/cidr (a GitHub-hosted module)
  rm -rf "$1"
  mkdir -p "$1"
  cat > "$1/main.tf" <<EOF
module "subnets" {
  source  = "hashicorp/subnets/cidr"
  version = "$MODULE_VERSION"

  base_cidr_block = "10.0.0.0/8"
  networks = [{ name = "harness", new_bits = 8 }]
}

output "harness" {
  value = module.subnets.network_cidr_blocks["harness"]
}
EOF
}

module_config "$work/modules.rc"
step "terraform init + apply with hashicorp/subnets/cidr $MODULE_VERSION through the module proxy (registry.terraform.io)"
module_project "$work/tf-module"
run terraform "$work/tf-module" "$work/modules.rc" 'module proxy'
grep -q '10.0.0.0/16' "$work/apply.log" || fail "terraform: the module's output is missing"
download=$(curl -sS -o /dev/null -D - "$W/repository/tf-mirror/v1/modules/hashicorp/subnets/cidr/$MODULE_VERSION/download")
printf '%s' "$download" | grep -qi '^x-terraform-get: ./archive.tar.gz//terraform-cidr-subnets-' \
  || fail "the module download does not point at the cached GitHub archive: $download"

step "tofu init + apply with hashicorp/subnets/cidr $MODULE_VERSION through the module proxy (registry.opentofu.org)"
module_project "$work/tofu-module"
run tofu "$work/tofu-module" "$work/modules.rc" 'module proxy'
grep -q '10.0.0.0/16' "$work/apply.log" || fail "tofu: the module's output is missing"

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

step 'publish a module and a private provider to the hosted repository tf-hosted'
hosted="$W/repository/tf-hosted"
ws_login
ws_token '["read","write","delete"]' '["tf-*"]'
PUBLISH=$WS_TOKEN
mkdir -p "$work/net"
printf 'variable "name" {\n  default = "harness-net"\n}\n\noutput "name" {\n  value = var.name\n}\n' > "$work/net/main.tf"
tar -czf "$work/net.tar.gz" -C "$work/net" .
curl -fsS -o /dev/null -H "Authorization: Bearer $PUBLISH" --upload-file "$work/net.tar.gz" \
  "$hosted/v1/modules/acme/net/aws/1.0.0" || fail 'module upload'
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PUBLISH" --upload-file "$work/net.tar.gz" \
  "$hosted/v1/modules/acme/net/aws/1.0.0")
[ "$code" = 409 ] || fail "a module version was replaced: $code"
pkg="$work/pkg"
mkdir -p "$pkg"
zip_name=terraform-provider-null_${VERSION}_linux_amd64.zip
sums_name=terraform-provider-null_${VERSION}_SHA256SUMS
curl -fsS -o "$pkg/$zip_name" "$repo/registry.terraform.io/hashicorp/null/$VERSION/linux/amd64/$zip_name" \
  || fail 'the provider package from the mirror'
(cd "$pkg" && sha256sum "$zip_name" > "$sums_name")
export GNUPGHOME="$work/gnupg"
mkdir -p -m 700 "$GNUPGHOME"
gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
  --quick-generate-key 'Harness Signer <signer@example.com>' ed25519 sign 1d 2>/dev/null || fail 'gpg key'
gpg --batch --quiet --pinentry-mode loopback --passphrase '' --detach-sign "$pkg/$sums_name" || fail 'gpg sign'
gpg --armor --export signer@example.com > "$pkg/key.asc"
curl -fsS -o "$work/release.json" -H "Authorization: Bearer $PUBLISH" -F "archive=@$pkg/$zip_name" \
  -F "shasums=@$pkg/$sums_name" -F "signature=@$pkg/$sums_name.sig" -F "signing_key=@$pkg/key.asc" \
  -F protocols=5.0 "$hosted/v1/providers/acme/null/$VERSION" || fail 'provider upload'
grep -q '"platforms":\["linux_amd64"\]' "$work/release.json" || fail "provider upload: $(cat "$work/release.json")"

ws_token '["read"]' '["tf-*"]'
READER=$WS_TOKEN
curl -fsS -H "Authorization: Bearer $READER" "$W/.well-known/terraform.json" \
  | grep -q '"modules.v1":"/repository/tf-hosted/v1/modules/"' || fail 'service discovery'

hosted_project() {  # hosted_project <dir>: the private provider and module
  rm -rf "$1"
  mkdir -p "$1"
  cat > "$1/main.tf" <<EOF
terraform {
  required_providers {
    null = {
      source  = "tf.example.com/acme/null"
      version = "$VERSION"
    }
  }
}

module "net" {
  source  = "tf.example.com/acme/net/aws"
  version = "1.0.0"
}

resource "null_resource" "harness" {}

output "net" {
  value = module.net.name
}
EOF
}
printf 'credentials "tf.example.com" {\n  token = "%s"\n}\n' "$READER" > "$work/hosted.rc"
for client in terraform tofu; do
  step "$client init + apply with a private module and provider from tf-hosted (bearer token, signed downloads)"
  hosted_project "$work/$client-hosted"
  run "$client" "$work/$client-hosted" "$work/hosted.rc" hosted
  grep -q 'provider "tf.example.com/acme/null"' "$work/$client-hosted/.terraform.lock.hcl" \
    || fail "$client lock file names no private provider"
  grep -q 'harness-net' "$work/apply.log" || fail "$client: the private module's output is missing"
done

step 'the hosted repository refuses an invalid token and a token for other repositories'
printf 'credentials "tf.example.com" {\n  token = "wst_harness-not-a-token"\n}\n' > "$work/hosted-bad.rc"
hosted_project "$work/hosted-bad"
if (cd "$work/hosted-bad" && TF_CLI_CONFIG_FILE="$work/hosted-bad.rc" terraform init -no-color) >/dev/null 2>&1; then
  fail 'terraform init succeeded with an invalid token'
fi
ws_token '["read"]' '["docker-*"]'
printf 'credentials "tf.example.com" {\n  token = "%s"\n}\n' "$WS_TOKEN" > "$work/hosted-narrow.rc"
hosted_project "$work/hosted-narrow"
if (cd "$work/hosted-narrow" && TF_CLI_CONFIG_FILE="$work/hosted-narrow.rc" tofu init -no-color) >/dev/null 2>&1; then
  fail 'a token limited to docker-* could install from the hosted repository'
fi
code=$(curl -sS -o /dev/null -w '%{http_code}' "$hosted/v1/modules/acme/net/aws/1.0.0/archive.tar.gz")
[ "$code" = 401 ] || fail "an unsigned anonymous archive download: $code"

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
