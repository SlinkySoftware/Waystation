# Terraform and OpenTofu clients

How to point Terraform and OpenTofu at a Waystation **Terraform** repository (a provider network mirror). The
repository's **Set me up** tab shows the same snippets with your real URL filled in. Examples below use
`https://waystation.example.com` and a repository called `terraform-mirror`.

Status (T-P5-TFP): provider network mirror (proxy) repositories. Verified by the real-client harness
(`tests/clients/terraform/`) with terraform 1.17 and tofu 1.13: `init` and `apply` of a configuration using
`hashicorp/null` through the mirror, from registry.terraform.io (archives on HashiCorp's release site) and from
registry.opentofu.org (archives on GitHub release assets, behind a redirect), anonymously and with an API token in a
`credentials` block. Modules (the module registry protocol) and the hosted Terraform repository (private providers
and modules) come with T-P5-TFM.

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
- The SSRF guard applies to every request, including the redirects to archive hosts: archives on private addresses
  need **allow private upstream**.

## Troubleshooting

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
  hashes.
