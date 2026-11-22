# Cargo client guide

How to point cargo (Rust) at a Waystation **Cargo** repository and publish your own crates. The repository's **Set
me up** tab shows the same snippets with your real URL filled in. Examples below use `https://waystation.example.com`
and repositories called `crates-io` (a proxy of crates.io), `crates-internal` (hosted) and `crates` (a group of both,
with `crates-internal` as its writable member).

## Overview

- Waystation serves Cargo's **sparse** protocol. Every repository's index is at
  `sparse+https://waystation.example.com/repository/<name>/index/` (the `sparse+` prefix is required; the git index
  protocol is not served).
- Proxy, hosted and group repositories; `cargo publish`, `cargo yank`, `cargo owner --list` and `cargo search`.
- Tested with cargo 1.99 (`tests/clients/cargo/`): a program with crates.io dependencies built through a proxy that
  replaces crates.io, `cargo login`, `cargo publish`, `cargo yank` and `cargo yank --undo` on a hosted repository,
  an anonymous build refused, and crates.io and hosted crates resolved together through a group that needs
  credentials.

## Repository types

- **Proxy**: caches a sparse index (default `https://index.crates.io/`). Index files are metadata, revalidated with
  the upstream's `ETag`/`Last-Modified` after the repository's metadata max age, and refused (502) unless every line
  is a valid entry for that crate. `.crate` files are downloaded from where the upstream's `config.json` says
  (crates.io: `static.crates.io`), checked against the index's `cksum` (sha256) before they are kept, and kept for
  good: a published version never changes.
- **Hosted**: your own crates, published with `cargo publish`. Index files are generated from what was published.
- **Group**: one registry for several Cargo repositories (proxy and hosted, in order). Index files are merged (the
  first member listing a version wins it); every `.crate` comes from the first member that has it. Publishing to the
  group goes to its writable member.

### Prefetch (proxy repositories)

The repository's warm list takes `crate@version` (an exact version, e.g. `serde@1.0.228`) or `crate` (its highest
non-yanked release); warming caches the index file and the `.crate`. "Keep popular metadata fresh" revalidates the
index files clients use most.

## Client setup

### cargo: replace crates.io

`~/.cargo/config.toml` (Windows: `%USERPROFILE%\.cargo\config.toml`), or `.cargo/config.toml` in a project:

```toml
[registries.waystation]
index = "sparse+https://waystation.example.com/repository/crates/index/"

[source.crates-io]
replace-with = "waystation"
```

Every crates.io dependency now comes through Waystation; `Cargo.lock` keeps recording them as crates.io packages,
with the same checksums, so lock files work with and without Waystation.

### cargo: named registry (private crates)

```toml
[registries.waystation]
index = "sparse+https://waystation.example.com/repository/crates/index/"
```

and in `Cargo.toml`:

```toml
[dependencies]
my-crate = { version = "1", registry = "waystation" }
```

The two can be combined: with the group above as both the replacement for crates.io and the registry of private
crates, one registry serves everything.

## Authentication

Cargo only sends tokens to registries that ask for credentials when a credential provider is configured, once per
machine in `~/.cargo/config.toml`:

```toml
[registry]
global-credential-providers = ["cargo:token"]
```

(`cargo:token` keeps tokens in `~/.cargo/credentials.toml`; `cargo:libsecret`, `cargo:macos-keychain` and
`cargo:wincred` use the system keyring.) The token is a Waystation **API token** (profile → API tokens; read scope to
build, write scope to publish):

```sh
cargo login --registry waystation        # paste the token
# or, for one shell or a CI job:
export CARGO_REGISTRIES_WAYSTATION_TOKEN=<api-token>
```

Cargo sends the token as the whole `Authorization` header; Waystation accepts it there (as well as
`Bearer <token>`). When anonymous users may read the repository (proxies, by default), cargo needs no token to build.
Otherwise Waystation answers cargo's first request (`config.json`) with `401`; cargo then sends the registry's token,
and the `config.json` it gets says `"auth-required": true`, so cargo sends the token on every index, download and API
request from then on. Without a token cargo stops with "no token found for `waystation`".

Use HTTPS in production: cargo sends the token on every request of a registry that needs credentials.

## Publishing

```toml
# Cargo.toml: keep the crate off crates.io
[package]
publish = ["waystation"]
```

```sh
cargo publish --registry waystation
cargo yank --registry waystation --version 1.0.0 my-crate
cargo yank --undo --registry waystation --version 1.0.0 my-crate
```

- Publishing needs `write` on the hosted repository (or on the group and its writable member). The crate file is
  checked before it is kept: a gzipped tar of `<name>-<version>/...` with a `Cargo.toml` for that name and version.
- Crate names follow crates.io: ASCII letters, digits, `-` and `_`, starting with a letter, at most 64 characters.
  Names that differ only in case or in `-` versus `_` are the same crate (the second spelling is refused).
- A published version is replaced by a new publish of the same version only if the repository's write policy allows
  redeploying (the default); with **disable redeploy** it is refused, with **read only** nothing is accepted. Prefer
  publishing a new version: lock files that recorded the old checksum fail to verify a replaced crate.
- Yanking keeps the file downloadable (existing lock files keep working); new resolutions skip the version. Yank and
  un-yank need `write`.
- `cargo search --registry waystation <term>` searches the crates the repository knows (published crates, and for a
  proxy the crates it has cached), not all of crates.io.

## Limitations

- Sparse protocol only: the git index protocol is not served.
- `cargo owner` can list owners (the accounts that published the crate); adding or removing owners is refused:
  access is managed with Waystation roles.
- `cargo search` covers only crates the repository knows, not the whole upstream.

## Troubleshooting

- **`authenticated registries require a credential-provider to be configured`**: add
  `global-credential-providers` (see Authentication).
- **`failed to get successful HTTP response from .../config.json`, status 401**: the repository needs credentials and
  cargo has no token for that registry name: `cargo login --registry <name>` or `CARGO_REGISTRIES_<NAME>_TOKEN`.
- **403 on publish or yank**: the token lacks the write scope or your account lacks `write` on the repository (for a
  group: on the group and on its writable member).
- **502 for an index file**: the upstream answered something that is not a valid index file for that crate (or could
  not be reached and nothing is cached yet). The server log names the reason.
- **Checksum mismatches after a re-publish**: a version was replaced (write policy allows redeploy); delete the
  affected `Cargo.lock` entries or publish a new version.
- **`the source waystation is not a registry`**: the index URL needs the `sparse+` prefix.
