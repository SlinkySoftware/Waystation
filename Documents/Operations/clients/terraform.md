# Terraform and OpenTofu clients

How to point Terraform and OpenTofu at Waystation **Terraform** repositories: a proxy repository is a provider
network mirror and a module registry proxy; the instance's one **hosted** Terraform repository is a private module
and provider registry. The repository's **Set me up** tab shows the same snippets with your real URL filled in.
Examples below use `https://waystation.example.com`, a proxy called `terraform-mirror` and the hosted repository
`terraform`.

Status: provider network mirror (T-P5-TFP), module registry proxy and the hosted repository (T-P5-TFM). Verified by
the real-client harness (`tests/clients/terraform/`) with terraform 1.17 and tofu 1.13: `init` and `apply` of a
configuration using `hashicorp/null` through the mirror, from registry.terraform.io (archives on HashiCorp's release
site) and from registry.opentofu.org (archives on GitHub release assets, behind a redirect), anonymously and with an
API token in a `credentials` block; `hashicorp/subnets/cidr` (a GitHub tag archive) through module proxies of both
registries; a module and a private provider published to the hosted repository and installed with an API token.

## What the mirror does

One repository serves providers of every **origin registry** it allows: option **origin registries**, by default
`registry.terraform.io` and `registry.opentofu.org`, plus the host of the repository's upstream URL. A provider is
addressed as `<hostname>/<namespace>/<type>` (Terraform's `registry.terraform.io/hashicorp/null`), and the mirror
answers the clients' network mirror protocol:

- `.../repository/terraform-mirror/<hostname>/<namespace>/<type>/index.json`: the versions the origin lists.
- `.../<version>.json`: the version's archive per platform with its hashes: `zh:` (the archive's sha256 from the
  version's signed `SHA256SUMS`) for every platform, and `h1:` (the hash the lock file uses) for archives this
  repository has cached.
- `.../<version>/<os>/<arch>/<file>.zip`: the archive, cached on first download.

Behind it, Waystation talks to the origin as Terraform would: service discovery (`/.well-known/terraform.json`),
the provider registry protocol (version list, download document per platform), then `SHA256SUMS`, its signature and
the archive from wherever the registry points. **Before anything of a version is served**, the signature of its
`SHA256SUMS` must verify with a signing key the registry publishes for that provider (option **verify signatures**,
on by default), and the list must name the registry's archive with the same sha256. Every archive is checked against
that list before it is cached; a mismatch is never cached and the client gets an error.

Caching: discovery for a day; version lists are metadata, revalidated after the repository's metadata max age
(default 30 minutes), so new provider releases show up; download documents, `SHA256SUMS` and archives are kept
(a release never changes) until a cleanup policy removes them (newest versions per provider in semantic version
order).

## CLI configuration

Terraform and OpenTofu accept only `https://` mirror URLs: Waystation must be reached over TLS (SPECIFICATION §8.4).

`~/.terraformrc` (Linux, macOS), `%APPDATA%\terraform.rc` (Windows) for Terraform; `~/.tofurc` or
`%APPDATA%\tofu.rc` for OpenTofu; or any file named by `TF_CLI_CONFIG_FILE` (both read it):

```hcl
provider_installation {
  network_mirror {
    url = "https://waystation.example.com/repository/terraform-mirror/"
  }
}
```

With only `network_mirror`, every provider is installed through Waystation and the clients never contact a
registry. To keep some providers direct, add `include`/`exclude` patterns and a `direct` block:

```hcl
provider_installation {
  network_mirror {
    url     = "https://waystation.example.com/repository/terraform-mirror/"
    include = ["registry.terraform.io/*/*", "registry.opentofu.org/*/*"]
  }
  direct {
    exclude = ["registry.terraform.io/*/*", "registry.opentofu.org/*/*"]
  }
}
```

Then `terraform init` / `tofu init` as usual. Provider sources keep their usual addresses: `hashicorp/null` means
`registry.terraform.io/hashicorp/null` for Terraform and `registry.opentofu.org/hashicorp/null` for OpenTofu, and the
mirror serves both.

### Lock files

A mirror's hashes are checked, not signed, so `terraform init` records only the `h1:` of the package it installed
for the current platform in `.terraform.lock.hcl`. To record every platform your team uses (through the mirror):

```sh
terraform providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=windows_amd64
```

The `zh:` hashes the mirror lists are the same as the registry's, so lock files created against the registry
directly keep working through the mirror and the other way round.

## Authentication

Anonymous users can read proxy repositories by default. When they cannot, give the client an **API token** (your
profile → API tokens; scope `read`, optionally limited to the repository) as a bearer token:

```hcl
credentials "waystation.example.com" {
  token = "<api token>"
}
```

or in the environment (host names without a port only; dots become `_`, dashes `__`):

```sh
export TF_TOKEN_waystation_example_com=<api token>
```

The clients send the token only to that host. Account passwords are not accepted here.

## Repository options

- **Origin registries**: host names whose providers the mirror serves. Add a private registry's host to mirror it;
  its upstream credentials (repository **Upstream authentication**, bearer) are sent only to the host of the
  repository's upstream URL, so point the upstream URL at that registry. The upstream URL's own host is reached with
  its scheme, so a registry in a lab without TLS works as the upstream.
- **Verify signatures**: on by default. Turn off only for an origin whose providers are not signed with a key type
  Waystation supports (version 4 RSA ≥ 2048 bits, ECDSA P-256/384/521, Ed25519/Ed448).
- **Module archive hosts** (proxies): hosts module archives are fetched and cached from (default `github.com`); the
  upstream's own host is always allowed. Sources elsewhere are passed to the client.
- The SSRF guard applies to every request, including the redirects to archive hosts: archives on private addresses
  need **allow private upstream**.
- The hosted repository has no format options; its write policy `read_only` refuses uploads and deletes.

## Modules through a proxy

A proxy repository also answers the **module registry protocol** of its upstream registry (`.../repository/
terraform-mirror/v1/modules/`). Module sources such as `terraform-aws-modules/vpc/aws` are looked up on the public
registry's host name, and that host's service discovery cannot be changed, so a `host` block in the CLI configuration
sends the module lookups for that host to Waystation (both clients support it). Module sources stay unchanged:

```hcl
host "registry.terraform.io" {
  services = {
    "modules.v1" = "https://waystation.example.com/repository/terraform-mirror/v1/modules/"
  }
}
```

For OpenTofu's registry create a second proxy with the upstream `https://registry.opentofu.org` and a block for
`registry.opentofu.org`. Combine the block with the `network_mirror` above: the `host` block names only the module
service, so providers must come through the mirror. When anonymous reads are not allowed, the token goes in a
`credentials` block **for the public registry's host name** (Terraform sends it to the URLs that host's services
point at, here Waystation):

```hcl
credentials "registry.terraform.io" {
  token = "<api token>"
}
```

What is cached: the registry's version list (revalidated after the metadata max age) and each version's download
answer; then the module itself when Waystation can fetch it as **one archive**: a GitHub repository at a tag or commit
(`git::https://github.com/<owner>/<repo>?ref=...`, what the public registries answer for most modules: fetched as
GitHub's tag archive) or an HTTP(S)/S3 archive (`.zip`, `.tar.gz`), from a host in the option **module archive
hosts** (default `github.com`) or the upstream's own host. The archive's sha256 is recorded at the first fetch and
pinned: if a cleanup removed it and the source later serves other bytes, Waystation refuses them. Other sources
(other git hosts, `ssh://`, a branch without a ref) are handed to the client unchanged: the client fetches them
itself, as without Waystation.

## The hosted repository: private modules and providers

An instance has **one** hosted Terraform repository (creating a second is refused), because Terraform finds a
registry through its host name: `https://waystation.example.com/.well-known/terraform.json` names that repository's
module and provider registries. Module sources are `<host>/<namespace>/<name>/<system>`, provider sources
`<host>/<namespace>/<type>`. The host name must contain a dot, and Waystation must be served over https.

```hcl
terraform {
  required_providers {
    example = {
      source  = "waystation.example.com/my-namespace/example"
      version = "1.0.0"
    }
  }
}

module "network" {
  source  = "waystation.example.com/my-namespace/network/aws"
  version = "1.0.0"
}
```

Credentials: a `credentials "waystation.example.com"` block (or `TF_TOKEN_waystation_example_com`, or
`terraform login waystation.example.com` with an API token) with an API token holding `read` on the repository.
Terraform and OpenTofu send it to the registry API, discovery included (a caller who cannot see the repository gets
an empty discovery document: "host does not provide a module registry"). They do **not** send it when they download
the files the registry points at, so for a signed-in caller Waystation adds a short-lived signed grant to each file
URL (`?ws_grant=...`, valid five minutes for that one file; adr/0010). Nothing else to configure; no `.netrc`.

### Publish a module

The archive holds the module directory's **contents** (its `.tf` files at the root of the archive), as a `.tar.gz`
of at most 64 MiB:

```sh
tar -czf network.tar.gz -C ./network .
curl --fail -H "Authorization: Bearer <api token>" --upload-file network.tar.gz \
  https://waystation.example.com/repository/terraform/v1/modules/my-namespace/network/aws/1.0.0
```

Publishing needs `write`. Waystation reads the archive without unpacking it and refuses links, absolute or `..`
names, more than 10 000 entries or 512 MiB unpacked, and an archive without a `.tf`/`.tf.json` file at its root (the
usual mistake: archiving the directory itself). A version cannot be replaced (`409`); delete it first:
`curl -X DELETE` on the same URL (`delete` permission).

### Publish a provider

A release is one request with every platform's zip, the `SHA256SUMS` listing them and its detached OpenPGP signature,
and the public key the signature verifies with (Terraform and OpenTofu check the same when they install it):

```sh
cd dist   # terraform-provider-example_1.0.0_<os>_<arch>.zip for each platform
sha256sum terraform-provider-example_1.0.0_*.zip > terraform-provider-example_1.0.0_SHA256SUMS
gpg --detach-sign terraform-provider-example_1.0.0_SHA256SUMS
gpg --armor --export my-signing-key > signing-key.asc
curl --fail -H "Authorization: Bearer <api token>" \
  -F archive=@terraform-provider-example_1.0.0_linux_amd64.zip \
  -F archive=@terraform-provider-example_1.0.0_darwin_arm64.zip \
  -F shasums=@terraform-provider-example_1.0.0_SHA256SUMS \
  -F signature=@terraform-provider-example_1.0.0_SHA256SUMS.sig \
  -F signing_key=@signing-key.asc -F protocols=5.0 \
  https://waystation.example.com/repository/terraform/v1/providers/my-namespace/example/1.0.0
```

The archive names must be `terraform-provider-<type>_<version>_<os>_<arch>.zip` for the address and version in the
URL, every archive must be listed in `SHA256SUMS` with its sha256, and the signature must verify with the key
(version 4 RSA ≥ 2048 bits, ECDSA, Ed25519). `protocols` is the plugin protocol (`5.0`, `6.0`; default `5.0`). A
release cannot be replaced; `curl -X DELETE` on the release URL removes it and its files. GoReleaser's usual
Terraform provider configuration produces exactly these files.

## Troubleshooting

- `invalid module registry hostname: must contain at least one dot`: hosted module sources need a host name with a
  dot (`localhost` does not work); use the name Waystation is served under.
- `... does not provide a module registry` / `... does not offer a provider registry`: discovery answered `{}`: no
  hosted Terraform repository exists, it is offline, or your token cannot see it (or no token was sent and anonymous
  users cannot read it).
- A module installs from the original git host instead of Waystation: its source is not one Waystation can cache
  (see "Modules through a proxy"); add the archive host to **module archive hosts** if it serves archives.
- `401` on an archive or provider file: the signed grant expired (five minutes) or the URL was edited; run `init`
  again.
- Upload `400`: the message names the problem (archive layout, `SHA256SUMS` entry, signature). `409`: the version
  exists.
- `Invalid provider_installation source block ... requires an absolute "https" URL`: the mirror URL is `http://`.
  Put Waystation behind TLS.
- `Failed to query available provider packages ... 404`: the provider's host is not one of the repository's origin
  registries, the provider does not exist on the origin, or (with a token limited to other repositories) the
  repository is hidden from you.
- `502 ... SHA256SUMS signature`: the version's signature did not verify with the registry's keys (or the registry
  publishes none). Nothing of that version is cached. Check the provider's release; as a last resort turn off
  **verify signatures** for the repository.
- `502 ... does not list ... with the registry's shasum`: the registry's download document and the release's
  `SHA256SUMS` disagree (a re-published release). Nothing is cached.
- `the cached package ... does not match any of the checksums recorded in the dependency lock file`: the lock file
  was made for another build of the provider (OpenTofu's registry serves its own builds of many providers: a
  Terraform lock file does not fit OpenTofu and vice versa).
- Browse → the repository lists the providers, their versions and the archives held with their `zh:` and `h1:`
  hashes (hosted: the releases with their signing key ID and publisher), and under **Modules** the modules, their
  versions and the archive held (proxy: its pinned sha256 and the source the registry gave).
