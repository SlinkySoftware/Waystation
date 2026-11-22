# Alpine apk: client guide

An apk proxy repository caches an Alpine mirror tree (`https://dl-cdn.alpinelinux.org/alpine/`). The mirror's
layout is kept below the repository, so one Waystation repository serves every branch (`v3.22`, `edge`,
`latest-stable`), repository (`main`, `community`, `testing`) and architecture. A hosted repository holds your own
packages under `<branch>/<repository>/<arch>/` and publishes an `APKINDEX.tar.gz` signed with its own key. Groups are
not supported for apk (signed indexes cannot be merged). Tested with apk-tools on Alpine 3.22 (`tests/clients/apk/`).
Protocol notes: FORMATS.md "Alpine apk".

Examples use `https://registry.example.com` as the Waystation base URL.

## Create the repositories

| Repository | Type | Upstream URL |
|---|---|---|
| `alpine` | proxy | `https://dl-cdn.alpinelinux.org/alpine/` |
| `apk-hosted` | hosted | - |

Options (all optional; they only shape "Set me up", "test upstream" and prefetch items without a path):

| Option | Default | Meaning |
|---|---|---|
| `branch` | `latest-stable` | Branch of the `/etc/apk/repositories` lines (`v3.22`, `edge`...) |
| `repositories` | `main`, `community` | Repositories below the branch |
| `architectures` | `x86_64` | Architectures that prefetch items without a path warm |

## Client configuration (proxy)

Replace the mirror lines of `/etc/apk/repositories` (keep your Alpine release's branch: `cat /etc/alpine-release`):

```text
https://registry.example.com/repository/alpine/v3.22/main
https://registry.example.com/repository/alpine/v3.22/community
```

```sh
apk update && apk add curl
```

apk picks the architecture itself (`<line>/<arch>/APKINDEX.tar.gz`). The `APKINDEX.tar.gz` files are passed through
untouched: apk keeps verifying their signatures with the Alpine keys in `/etc/apk/keys` (package `alpine-keys`), and
each package against the index.

### What Waystation checks

- `APKINDEX.tar.gz` must be an apk index (gzip streams holding a tar with `APKINDEX`; at most 64 MiB, 512 MiB
  unpacked), else the upstream answer is refused (502). Its signature is left to apk.
- A package's index entry gives `C:` (`Q1` + base64 SHA-1 of the package's **control segment**, not of the file) and
  `S:` (the file size). Before a fetched `.apk` is stored Waystation checks the size, finds the control segment (the
  gzip stream after the optional signature stream), compares its SHA-1 with `C:`, then checks the data segment
  against the SHA-256 `datahash` that the verified `.PKGINFO` names: the whole file is verified. A mismatch
  revalidates the index once, then fails (502).
- A package requested before its directory's index was cached makes Waystation fetch and index that index first. A
  package no index lists (an older release removed upstream, a direct download) is served and recorded `unverified`
  with its computed `C:`; when a later index lists it with another checksum the cached copy is dropped.

## Hosted repositories

Upload with `PUT` to `<branch>/<repository>/<arch>/` (the write permission). The package is stored as
`<pkgname>-<pkgver>.apk` whatever the file name in the URL; its architecture must be the directory's, or `noarch`
(listed with the directory's architecture, as `apk index --rewrite-arch` does):

```sh
curl -fS -u ci-bot:<token> --upload-file mypackage-1.0-r0.apk \
  https://registry.example.com/repository/apk-hosted/v1/main/x86_64/mypackage-1.0-r0.apk
# Delete (the delete permission)
curl -fS -u ci-bot:<token> -X DELETE \
  https://registry.example.com/repository/apk-hosted/v1/main/x86_64/mypackage-1.0-r0.apk
```

Within seconds a worker rebuilds the `APKINDEX.tar.gz` of every directory and signs it with the repository's RSA key
(`.SIGN.RSA256.<key name>`: RSA PKCS#1 v1.5 over SHA-256; apk-tools 2.12 or newer, every supported Alpine release).
Packages themselves need no signature: apk trusts them through the signed index (`C:`). Write policies apply
(`disable_redeploy`: an existing version is refused with 409; `read_only`: 403).

Trust the repository's key once, then use it like any repository. The key name is
`<repository name>-<last 8 characters of its id>.rsa.pub` ("Set me up" shows it); the key is published after the first
upload:

```sh
wget -O /etc/apk/keys/apk-hosted-01abcdef.rsa.pub \
  https://registry.example.com/repository/apk-hosted/keys/apk-hosted-01abcdef.rsa.pub
echo https://registry.example.com/repository/apk-hosted/v1/main >> /etc/apk/repositories
apk update && apk add mypackage
```

Compare the key with the one the UI shows before you trust it. Until the key is in `/etc/apk/keys`, `apk update`
reports the index as `UNTRUSTED signature`.

## Browse and search

The browse view lists packages (newest version, architectures) and each package's files (version, branch,
repository, architecture, `C:` checksum, verified or not, cached, license, origin, dependencies). A proxy lists what
its cached indexes list. Global search finds packages by name. Cleanup policies order versions with apk's rules
(`1.0_rc1 < 1.0 < 1.0-r1 < 1.0_p1 < 1.0a < 1.0.1`).

## Prefetch

Warm items, one per line: `name` (the version the index lists), `name=version`, each looked up in the default branch
for every configured architecture (the first configured repository that lists it), or
`<branch>/<repository>/<arch>/name[=version]`. Refresh re-reads popular `APKINDEX.tar.gz` files.

## Authentication

Anonymous reads (when the repository allows them) need nothing. Otherwise put the credentials in the URL; apk sends
them as HTTP basic authentication. An API token is the password; keep the file chmod 600:

```text
https://ci-bot:<token>@registry.example.com/repository/alpine/v3.22/main
```

## Troubleshooting

- `UNTRUSTED signature` on a proxy: the image lacks the Alpine key of that branch (`apk add alpine-keys` from a
  trusted source), or the index was not from Alpine. On a hosted repository: the repository key is not in
  `/etc/apk/keys`, or its file name differs from the key name in the signature.
- `BAD signature` / checksum errors on `apk add`: the upstream changed a package under the same version; Waystation
  answers 502 after revalidating the index. Run `apk update` and retry.
- A package just uploaded is not offered: `apk update` reuses an index it fetched less than a minute ago; run
  `apk update --force-refresh` (CI jobs that upload and install right away).
- 404 on `APKINDEX.tar.gz` of a hosted repository: nothing was uploaded to that `<branch>/<repository>/<arch>` yet.
- 400 on upload: not an apk v2 package (apk v3 `.adb` packages are not supported), an architecture that is not the
  directory's, a path that is not `<branch>/<repository>/<arch>/<file>.apk`, or a `.PKGINFO` without a valid
  `pkgname`/`pkgver`.
