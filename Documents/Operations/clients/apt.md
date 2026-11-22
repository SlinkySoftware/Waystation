# apt (Debian, Ubuntu) client guide

How to point apt at Waystation apt repositories, and how to publish your own `.deb` packages to a hosted repository.
The repository's **Set me up** tab shows the same snippets with your real URL filled in. Examples use
`https://waystation.example.com` as the Waystation base URL, proxy repositories named `debian`, `debian-security`,
`ubuntu` and `ubuntu-security`, and a hosted repository named `debs`.

## Overview

An apt proxy repository caches a Debian-style archive (`deb.debian.org/debian`, `deb.debian.org/debian-security`,
`archive.ubuntu.com/ubuntu`, `security.ubuntu.com/ubuntu`, a vendor repository). A hosted apt repository publishes
your own `.deb` packages, signed with its own key. Protocol notes: [FORMATS.md](../../Architecture/FORMATS.md),
section "apt".

Every repository answers at `https://waystation.example.com/repository/<name>/`, with the archive layout below it
(`dists/<suite>/...`, `pool/...`).

Tested with apt on Debian 13 and Ubuntu 26.04 against proxies, and with dpkg-deb, curl uploads and apt on Debian 13
against a hosted repository. A Raspberry Pi OS arm64 client (Debian 13 arm64 plus `archive.raspberrypi.com/debian`,
`trixie`) runs in the same test suite where arm64 containers can run.

### What Waystation checks (proxy)

- `InRelease`/`Release` are served as the upstream signed them; clients verify the signature.
- Index files (`Packages`, `Sources`, `Translation-*`, ...) are always the ones the `Release` your client received
  lists, checked against its SHA256. apt's `by-hash` requests (`Acquire-By-Hash`) are cached forever and verified
  against the hash in their path. If the upstream changed an index under its `Release`, Waystation fetches the
  `Release` again once, then fails rather than serve a mismatched set.
- Package files under `pool/` are checked against the SHA256 and size of their entry in a cached `Packages` file.
  A package fetched before any `Packages` file that lists it was cached is served and recorded as *unverified*; when
  a listing arrives and disagrees, the cached copy is dropped and fetched again on the next request.

## Repository types

### Proxy

One repository per upstream archive, not per suite: every suite of the archive (`trixie`, `trixie-updates`,
`trixie-backports`) goes through the same repository. Security archives live at a different upstream URL, so they
get their own repository.

| Repository | Upstream URL | Distribution |
|---|---|---|
| `debian` | `https://deb.debian.org/debian/` | `trixie` |
| `debian-security` | `https://deb.debian.org/debian-security/` | `trixie-security` |
| `ubuntu` | `https://archive.ubuntu.com/ubuntu/` | `resolute` |
| `ubuntu-security` | `https://security.ubuntu.com/ubuntu/` | `resolute-security` |

*Distribution* is informational (the "Set me up" snippets and the upstream probe use it); it does not limit the
suites clients can use. Set *flat* only for flat repositories (`deb <url> ./`, no `dists/`). The metadata max age
(default 30 minutes) is how long a cached `InRelease` is served before Waystation asks the upstream again.

### Hosted

A hosted apt repository holds the `.deb` packages you upload and publishes one distribution for them, signed with a
key Waystation generates for that repository (RSA 3072, stored encrypted under the data directory's `keys/`; never
shown through the API). Create it with type *Hosted* and set:

| Option | Meaning |
|---|---|
| Distribution | The suite clients use, e.g. `stable` or `trixie` (`dists/<distribution>/`; one name, no `/`). |
| Components | Where uploads may go (default `main`); uploads use the first unless `?component=` names another. |
| Architectures | Always published (`binary-<arch>`, default `amd64 arm64`). Packages with `Architecture: all` are listed under each; a package of another architecture adds its architecture. |
| Write policy | `allow_redeploy` replaces a package version, `disable_redeploy` refuses an existing one (409), `read_only` refuses uploads and deletes (403). |

Browse lists the published packages under the distribution, like a proxy's suites.

### Group

Group repositories are not supported for apt.

## Client setup

### apt (proxy repositories)

Point the `URIs` (or the URL of a one-line entry) at the repository and keep everything else, **including
`Signed-By`**: apt still verifies the distribution's own signature, because Waystation passes `InRelease`,
`Release` and `Release.gpg` through byte for byte. There is no Waystation key to install for a proxy.

deb822 (Debian 12+, Ubuntu 24.04+), e.g. `/etc/apt/sources.list.d/debian.sources` replacing the stock file:

```text
Types: deb
URIs: https://waystation.example.com/repository/debian/
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: https://waystation.example.com/repository/debian-security/
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
```

Ubuntu (`/etc/apt/sources.list.d/ubuntu.sources`):

```text
Types: deb
URIs: https://waystation.example.com/repository/ubuntu/
Suites: resolute resolute-updates resolute-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: https://waystation.example.com/repository/ubuntu-security/
Suites: resolute-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
```

One-line format (`/etc/apt/sources.list.d/*.list`, older releases):

```text
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://waystation.example.com/repository/debian/ trixie main
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://waystation.example.com/repository/debian-security/ trixie-security main
```

Then `sudo apt update`. Remove or comment out the stock entries, otherwise apt also talks to the upstream directly.

Over plain `http://` (inside a trusted network) apt needs nothing else; for `https://` on Debian before 13 and
Ubuntu before 24.04 the image needs `ca-certificates` (and `apt-transport-https` on very old releases).

### apt (hosted repositories)

Install the repository's public key, then point apt at it with `Signed-By`:

```shell
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL -u <username>:<your-api-token> https://waystation.example.com/repository/debs/public.asc \
  | sudo tee /etc/apt/keyrings/debs.asc > /dev/null
gpg --show-keys /etc/apt/keyrings/debs.asc   # compare the fingerprint with the one the index job logged
```

`/etc/apt/sources.list.d/debs.sources`:

```text
Types: deb
URIs: https://waystation.example.com/repository/debs/
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/debs.asc
```

apt reads armored keys from files ending in `.asc` (apt 2.4 and later); `public.gpg` is the same key as a binary
keyring for older clients. Credentials go into `auth.conf` (see "Authentication").

## Authentication

Proxy repositories are readable anonymously by default, so no client configuration is needed. Hosted repositories,
and proxies whose anonymous access an administrator removed, need credentials: apt authenticates with HTTP basic
auth, any username and an API token as the password. Create an API token on your profile page (avatar menu →
Profile → API tokens; shown once, copy it then) or, for CI, on a service account. Give it only the scopes it needs
(`read` to install; `write` / `delete` to upload and delete) and, ideally, restrict it to the repositories the client
needs: a token only ever narrows what its owner may do. A revoked or expired token is refused at once (401); it
never falls back to anonymous access.

Keep the credentials out of the sources file, in `/etc/apt/auth.conf.d/waystation.conf` (`chmod 600`, owned by
root):

```text
machine waystation.example.com/repository/
login <username>
password <your-api-token>
```

apt applies an entry without a scheme to `https://` sources only. If Waystation is reached over plain HTTP (not
recommended: the token travels in clear), write the scheme: `machine http://waystation.example.com/repository/`.

Account passwords (local or LDAP) are refused here (401): apt, like every format client except `docker login` and
`npm login`, uses API tokens. Tokens of LDAP and OIDC users work the same as local users' tokens.

## Publishing

Publish to a hosted repository. Upload and delete with curl (an API token with the `write` / `delete` scope):

```shell
curl -fS -u <username>:<your-api-token> --upload-file mytool_1.2-1_amd64.deb https://waystation.example.com/repository/debs/
curl -fS -u <username>:<your-api-token> --upload-file mytool_1.2-1_amd64.deb 'https://waystation.example.com/repository/debs/?component=contrib'
curl -fS -u <username>:<your-api-token> -X DELETE https://waystation.example.com/repository/debs/pool/main/m/mytool/mytool_1.2-1_amd64.deb
```

`POST` to the repository URL with the package as the body (`--data-binary @file.deb`, as Nexus documents it) or as a
form file (`-F package=@file.deb`) works too. Whatever the URL, the package is stored at its pool path
`pool/<component>/<prefix>/<source>/<name>_<version>_<arch>.deb` (the answer's `Location`). Waystation reads the
control file (one paragraph with valid `Package`, `Version` and `Architecture`; the archive layout, sizes and
compression are checked, nothing is unpacked) and refuses anything else with 400. The same package version and
architecture can be stored once per repository (another component answers 409).

### Index rebuild and signing

Within seconds of each upload or delete a worker rebuilds the distribution: `Packages`, `Packages.gz` and
`Packages.xz` per component and architecture (also at their `by-hash/SHA256/` paths, `Acquire-By-Hash: yes`),
`Release`, a clearsigned `InRelease` and a detached `Release.gpg`, switched together so clients never see a mix; the
previous generation's `by-hash` files stay for clients in the middle of an update. Until the first upload
`dists/<distribution>/InRelease` answers 404.

## Limitations

- Group repositories are not supported for apt.
- Hosted repositories: one distribution per repository (create one repository per distribution); binary packages
  only, no source packages (`.dsc`); the signing key is generated by Waystation and cannot be imported or rotated.
- Proxies never re-sign: clients verify the upstream's signature with the upstream's keyring.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `NO_PUBKEY` / `The following signatures couldn't be verified` | `Signed-By` points at the wrong keyring (Debian vs Ubuntu vs vendor key) or the keyring package is old. Waystation never re-signs proxied archives; use the keyring you used against the upstream. |
| `404  Not Found [IP: ...]` for `dists/<suite>/InRelease` | The suite does not exist under this repository's upstream URL (e.g. `trixie-security` requested from the `debian` repository: it lives in `debian-security`), or the upstream URL's base path is wrong. |
| `404` for an index file (`.../Packages.xz`) | The `Release` the suite is served with does not list that file (wrong component or architecture, or a compression the archive does not publish). Waystation only serves indexes it can verify. |
| `File has unexpected size` / `Hash Sum mismatch` | Should not happen through Waystation (index sets always match the served `Release`). If it does, the upstream changed files mid-update: run `apt update` again; persistent failures are a bug worth reporting with the repository's activity log. |
| `502 Bad Gateway` on `apt update` | The upstream answered an error, an index failed verification twice (the upstream's `Release` and files disagree), or a `Release` file was malformed. The repository's upstream health card shows the last error. |
| `504 Gateway Timeout` | The upstream did not answer in time. A stale cached `InRelease` is served instead when one exists and *stale on error* is on. |
| Updates appear late | The cached `InRelease` is served until it is older than the metadata max age (default 30 minutes). Lower it for security repositories if needed. |
| `401` | Anonymous access was removed for this repository: add credentials (see "Authentication"). |
| Hosted: `NO_PUBKEY` / `The following signatures couldn't be verified` | `Signed-By` does not point at the repository's own key (`public.asc`). Download it again; the key never changes for a repository. |
| Hosted: `404` for `dists/<distribution>/InRelease` | Nothing was uploaded yet, the distribution in the sources differs from the repository's, or the index job has not run (check Tasks for `index.apt`). |
| Hosted upload answers `400` | The file is not a Debian binary package Waystation accepts (the message says why), or `?component=` names a component the repository does not have. |
| Hosted upload answers `409` | `disable_redeploy` and the version exists, or the same version and architecture is stored in another component. |
